"""Optional off-site copy of verified backup sets to S3-compatible storage (docs/backup-recovery.md). DISABLED until the
owner approves a destination and credentials; nothing in the repository names a bucket, endpoint or key.

    python -m app.backup.offsite upload --config /etc/hyperchat-backup/offsite-<env>.env (--latest | --set BASE)
    python -m app.backup.offsite verify --config ... --set BASE
    python -m app.backup.offsite list   --config ...
    python -m app.backup.offsite fetch  --config ... --set BASE --to NEW_DIR   (downloads, verifies, stays encrypted)

Guarantees:
  * only a locally VERIFIED set (app.backup.sets.load, for this environment's own directory) is uploaded;
  * every file is encrypted on this host with GPG to the owner's PUBLIC key before it leaves (the private key stays
    offline with the owner: this server, and anyone who compromises it, cannot decrypt the off-site copies);
  * objects go under <prefix>/<set>/ where the prefix IS the environment (prod / dev): one bucket or two, never mixed;
  * never overwrites (HEAD first, then a conditional If-None-Match PUT; an existing object with another hash is a
    refusal) and never deletes: the tool has no delete code path. Retention is the bucket's lifecycle rule and its
    object lock (owner configuration), so a stolen upload credential cannot erase history;
  * Content-MD5 on every PUT (integrity in transit, TLS endpoint only), then every object is READ BACK and its SHA-256
    compared; COMPLETE.json (hashes, sizes, recipient, no secrets) is written last: an off-site set without it is
    incomplete;
  * resumable without ever trusting the remote side: GPG encryption is randomized, so a retry must reuse the exact
    ciphertext it already produced. Each file's ciphertext and a binding record (environment, database, set, file,
    plaintext SHA-256, recipient key SHA-256, ciphertext SHA-256) are kept in a private local STATE directory until
    the set is complete. An existing remote object is accepted only when its DOWNLOADED bytes hash to the recorded
    ciphertext (object metadata is never trusted); anything else (another object, a corrupted one, one whose local
    record is gone, a changed plaintext or recipient) is a refusal, never an overwrite;
  * an already complete off-site set is verified against the local set (plaintext hashes, recipient, every object's
    bytes) and reported as such; nothing is uploaded again.
Exit: 0 ok, 2 refused (disabled / invalid configuration / wrong set / unverifiable remote state), 1 failed.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.backup.sets import BackupSet, SetError, complete_sets, load, sha256_file

LOCAL_DIRS = {"prod": "/var/backups/hyperchat", "dev": "/var/backups/hyperchat-dev"}
DATABASES = {"prod": "hyperchat", "dev": "hyperchat_dev"}
KEYS = ("ENABLED", "ENVIRONMENT", "ENDPOINT", "BUCKET", "PREFIX", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY",
        "GPG_RECIPIENT_FILE", "LOCAL_DIR", "DATABASE", "STATE_DIR")
STATE_ROOT = "/var/lib/hyperchat-offsite"  # systemd StateDirectory=hyperchat-offsite/<env>, root 0700


class Refused(Exception):
    """Configuration or input that must not be acted on (message: a reason code)."""


class Failed(Exception):
    """An upload or verification that did not complete (message: a reason code)."""


@dataclass(frozen=True)
class Config:
    environment: str
    endpoint: str
    bucket: str
    prefix: str
    access_key_id: str
    secret_access_key: str
    recipient_file: Path
    local_dir: Path
    database: str
    state_dir: Path

    def __repr__(self) -> str:  # never print credentials
        return f"Config(environment={self.environment!r}, bucket={self.bucket!r}, prefix={self.prefix!r})"


def read_config(path: str) -> Config:
    p = Path(path)
    try:
        st = p.lstat()
    except OSError as exc:
        raise Refused("config_missing") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise Refused("config_must_be_a_private_file")  # mode 0600: it holds the storage credential
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise Refused("config_owner")
    values: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.startswith("HYPERCHAT_OFFSITE_") or key[len("HYPERCHAT_OFFSITE_"):] not in KEYS:
            raise Refused("config_unknown_line")
        values[key[len("HYPERCHAT_OFFSITE_"):]] = value.strip()
    if values.get("ENABLED", "false").lower() != "true":
        raise Refused("offsite_disabled")
    env = values.get("ENVIRONMENT", "")
    if env not in ("prod", "dev", "test"):
        raise Refused("config_environment")
    if values.get("PREFIX") != env:
        raise Refused("prefix_must_be_the_environment")  # prod and dev never share a namespace
    endpoint = values.get("ENDPOINT", "")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/?", endpoint):
        raise Refused("endpoint_must_be_https")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", values.get("BUCKET", "")):
        raise Refused("config_bucket")
    if not values.get("ACCESS_KEY_ID") or not values.get("SECRET_ACCESS_KEY"):
        raise Refused("config_credentials")
    recipient = Path(values.get("GPG_RECIPIENT_FILE", ""))
    if not recipient.is_absolute() or not recipient.is_file():
        raise Refused("config_gpg_recipient_file")
    local_dir = Path(values.get("LOCAL_DIR") or LOCAL_DIRS.get(env, ""))
    database = values.get("DATABASE") or DATABASES.get(env, "")
    if env in LOCAL_DIRS and (str(local_dir) != LOCAL_DIRS[env] or database != DATABASES[env]):
        raise Refused("config_local_dir_or_database_of_another_environment")
    if env == "test" and (str(local_dir).startswith("/var/backups") or not database.startswith("hc_backuptest_")):
        raise Refused("config_test_scope")
    state_dir = Path(values.get("STATE_DIR") or f"{STATE_ROOT}/{env}")
    if not state_dir.is_absolute() or ".." in state_dir.parts or state_dir.name != env:
        raise Refused("config_state_dir")  # one state directory per environment, named after it
    if env == "test" and str(state_dir).startswith(STATE_ROOT):
        raise Refused("config_test_scope")
    return Config(env, endpoint.rstrip("/"), values["BUCKET"], env, values["ACCESS_KEY_ID"],
                  values["SECRET_ACCESS_KEY"], recipient, local_dir, database, state_dir)


def s3_client(cfg: Config):
    import boto3
    from botocore.config import Config as BotoConfig
    return boto3.client("s3", endpoint_url=cfg.endpoint, aws_access_key_id=cfg.access_key_id,
                        aws_secret_access_key=cfg.secret_access_key, region_name="auto",
                        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}))


def gpg_encrypt(src: Path, dst: Path, recipient_file: Path, home: Path) -> None:
    """Encrypt to the owner's public key only (no signing key, no passphrase on this host)."""
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--yes", "--no-tty", "--quiet", "--trust-model",
                    "always", "--recipient-file", str(recipient_file), "--output", str(dst), "--encrypt", str(src)],
                   capture_output=True, check=True)


def _sha256_stream(body) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: body.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def _exists(client, bucket: str, key: str) -> dict | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # botocore ClientError 404 / NoSuchKey
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise Failed("head_failed") from exc


def _remote_sha(client, cfg: Config, key: str) -> str:
    try:
        return _sha256_stream(client.get_object(Bucket=cfg.bucket, Key=key)["Body"])
    except Exception as exc:
        raise Failed("readback_failed") from exc


def _put_verified(client, cfg: Config, key: str, path: Path, sha: str) -> str:
    """PUT once (never over another object), then read it back and compare. An object already there is 'kept' only
    when its DOWNLOADED bytes hash to `sha` (this set's own recorded ciphertext); its metadata is never trusted."""
    if _exists(client, cfg.bucket, key) is not None:
        if _remote_sha(client, cfg, key) != sha:
            raise Refused("remote_object_differs_from_this_sets_ciphertext")
        return "kept"
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    md5 = base64.b64encode(digest.digest()).decode()
    with open(path, "rb") as body:
        try:
            client.put_object(Bucket=cfg.bucket, Key=key, Body=body, ContentMD5=md5, IfNoneMatch="*",
                              Metadata={"sha256": sha})
        except Exception as exc:
            raise Failed("put_failed") from exc
    if _remote_sha(client, cfg, key) != sha:
        raise Failed("readback_checksum_mismatch")
    return "uploaded"


def _private_dir(path: Path) -> Path:
    """Create (0700) or accept a directory only when it is ours, private and not a symlink."""
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077 or (hasattr(os, "geteuid") and st.st_uid != os.geteuid()):
        raise Refused("state_dir_not_private")
    return path


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ciphertext(cfg: Config, bset: BackupSet, path: Path, state: Path, recipient_sha: str, encrypt, home: Path) -> dict:
    """This file's ciphertext and its binding record: reused when recorded (a resume), created once otherwise."""
    record_path, enc = state / f"{path.name}.json", state / f"{path.name}.gpg"
    bound = {"environment": bset.environment, "database": bset.database, "set": bset.base, "file": path.name}
    plain_sha = sha256_file(path)
    if record_path.exists() or record_path.is_symlink():
        try:
            if record_path.is_symlink():
                raise ValueError
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refused("local_state_unreadable") from exc
        if {k: record.get(k) for k in bound} != bound:
            raise Refused("local_state_of_another_set")
        if record.get("plain_sha256") != plain_sha:
            raise Refused("local_plaintext_changed")  # sets never change once written
        if record.get("recipient_sha256") != recipient_sha:
            raise Refused("recipient_changed")  # objects already off-site were encrypted to another key
        if enc.is_symlink() or not enc.is_file() or sha256_file(enc) != record.get("ciphertext_sha256"):
            raise Refused("local_ciphertext_changed")
        return record
    tmp = state / f"{path.name}.gpg.tmp"
    tmp.unlink(missing_ok=True)
    encrypt(path, tmp, cfg.recipient_file, home)
    sha = sha256_file(tmp)
    if sha == plain_sha:
        raise Failed("encryption_did_not_change_the_data")
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, enc)
    record = {**bound, "plain_sha256": plain_sha, "recipient_sha256": recipient_sha, "ciphertext_sha256": sha,
              "bytes": enc.stat().st_size}
    _write_atomically(record_path, json.dumps(record, sort_keys=True).encode())  # only now may it be uploaded
    return record


def _already_complete(cfg: Config, bset: BackupSet, client, recipient_sha: str) -> dict:
    """The off-site set exists: accept it only when it is exactly this local set (every object's bytes checked)."""
    try:
        marker = json.loads(client.get_object(Bucket=cfg.bucket,
                                              Key=f"{cfg.prefix}/{bset.base}/COMPLETE.json")["Body"].read())
    except Exception as exc:
        raise Failed("marker_unreadable") from exc
    local = {p.name: sha256_file(p) for p in bset.files}
    offsite_plain = {o.get("plain_name"): o.get("plain_sha256") for o in marker.get("objects", [])}
    if (marker.get("environment"), marker.get("database"), marker.get("set")) != (cfg.environment, cfg.database,
                                                                                  bset.base):
        raise Refused("offsite_set_of_another_environment_or_set")
    if marker.get("recipient_sha256") != recipient_sha:
        raise Refused("offsite_set_encrypted_to_another_recipient")
    if offsite_plain != local:
        raise Refused("offsite_set_differs_from_local")
    verify(cfg, bset.base, client)
    return {"ok": True, "set": bset.base, "already_complete": True, "objects": []}


def upload(cfg: Config, bset: BackupSet, client, encrypt=gpg_encrypt) -> dict:
    if bset.environment != cfg.environment or bset.database != cfg.database:
        raise Refused("set_of_another_environment")
    recipient_sha = sha256_file(cfg.recipient_file)
    if _exists(client, cfg.bucket, f"{cfg.prefix}/{bset.base}/COMPLETE.json") is not None:
        return _already_complete(cfg, bset, client, recipient_sha)
    state = _private_dir(_private_dir(cfg.state_dir) / bset.base)
    home = Path(tempfile.mkdtemp(prefix="hc-offsite-gnupg-"))
    try:
        os.chmod(home, 0o700)
        objects = []
        for path in bset.files:
            record = _ciphertext(cfg, bset, path, state, recipient_sha, encrypt, home)
            key = f"{cfg.prefix}/{bset.base}/{path.name}.gpg"
            outcome = _put_verified(client, cfg, key, state / f"{path.name}.gpg", record["ciphertext_sha256"])
            objects.append({"key": key, "sha256": record["ciphertext_sha256"], "bytes": record["bytes"],
                            "outcome": outcome, "plain_sha256": record["plain_sha256"], "plain_name": path.name})
        complete = {"format": 1, "environment": bset.environment, "database": bset.database, "set": bset.base,
                    "created_at": bset.manifest["created_at"], "alembic_version": bset.manifest.get("alembic_version"),
                    "encryption": "gpg-public-key", "recipient_sha256": recipient_sha,
                    "objects": [{k: o[k] for k in ("key", "sha256", "bytes", "plain_sha256", "plain_name")}
                                for o in objects]}
        marker = state / "COMPLETE.json"  # deterministic from the records: a retried marker is the same bytes
        _write_atomically(marker, json.dumps(complete, sort_keys=True).encode())
        _put_verified(client, cfg, f"{cfg.prefix}/{bset.base}/COMPLETE.json", marker, sha256_file(marker))
        shutil.rmtree(state)  # local working copies only; the off-site set is complete and verified
        return {"ok": True, "set": bset.base, "objects": [{k: o[k] for k in ("key", "outcome", "bytes")} for o in objects]}
    finally:
        shutil.rmtree(home, ignore_errors=True)


def verify(cfg: Config, base: str, client) -> dict:
    """Download COMPLETE.json and every object it names; compare SHA-256. Decryption is the owner's (offline key)."""
    try:
        marker = json.loads(client.get_object(Bucket=cfg.bucket, Key=f"{cfg.prefix}/{base}/COMPLETE.json")["Body"].read())
    except Exception as exc:
        raise Failed("not_complete_offsite") from exc
    if marker.get("environment") != cfg.environment or marker.get("set") != base:
        raise Refused("marker_of_another_environment_or_set")
    for obj in marker["objects"]:
        if not obj["key"].startswith(f"{cfg.prefix}/{base}/"):
            raise Refused("marker_key_outside_the_set")
        try:
            got = _sha256_stream(client.get_object(Bucket=cfg.bucket, Key=obj["key"])["Body"])
        except Exception as exc:
            raise Failed("object_missing") from exc
        if got != obj["sha256"]:
            raise Failed("object_checksum_mismatch")
    return {"ok": True, "set": base, "objects": len(marker["objects"])}


def fetch(cfg: Config, base: str, client, to: Path) -> dict:
    """Download one COMPLETE off-site set into a NEW private directory, verifying every object's SHA-256. The files
    stay encrypted: decrypting them is the owner's step, offline, with the private key (docs/backup-recovery.md)."""
    if to.exists():
        raise Refused("target_exists")
    marker_raw = client.get_object(Bucket=cfg.bucket, Key=f"{cfg.prefix}/{base}/COMPLETE.json")["Body"].read()
    marker = json.loads(marker_raw)
    if marker.get("environment") != cfg.environment or marker.get("set") != base:
        raise Refused("marker_of_another_environment_or_set")
    to.mkdir(mode=0o700, parents=False)
    for obj in marker["objects"]:
        name = obj["key"].rsplit("/", 1)[-1]
        if obj["key"] != f"{cfg.prefix}/{base}/{name}" or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gpg", name):
            raise Refused("marker_key_outside_the_set")
        h = hashlib.sha256()
        body = client.get_object(Bucket=cfg.bucket, Key=obj["key"])["Body"]
        with open(to / name, "xb") as out:
            for chunk in iter(lambda: body.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != obj["sha256"]:
            raise Failed("object_checksum_mismatch")
    (to / "COMPLETE.json").write_bytes(marker_raw)
    return {"ok": True, "set": base, "objects": len(marker["objects"]), "to": str(to), "encrypted": True}


def list_complete(cfg: Config, client) -> list[str]:
    paginator = client.get_paginator("list_objects_v2")
    sets = []
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=f"{cfg.prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) == 3 and parts[2] == "COMPLETE.json":
                sets.append(parts[1])
    return sorted(sets, reverse=True)


def main(argv: list[str] | None = None, *, client_factory=s3_client, encrypt=gpg_encrypt) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backup.offsite", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("upload", "verify", "list", "fetch"):
        sub.add_parser(name).add_argument("--config", required=True)
    group = sub.choices["upload"].add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true")
    group.add_argument("--set")
    sub.choices["verify"].add_argument("--set", required=True)
    sub.choices["fetch"].add_argument("--set", required=True)
    sub.choices["fetch"].add_argument("--to", required=True, help="a NEW directory (created private)")
    args = parser.parse_args(argv)
    try:
        cfg = read_config(args.config)
        client = client_factory(cfg)
        if args.command == "list":
            result = {"ok": True, "sets": list_complete(cfg, client)}
        elif args.command == "verify":
            result = verify(cfg, args.set, client)
        elif args.command == "fetch":
            result = fetch(cfg, args.set, client, Path(args.to))
        else:
            base = args.set
            if args.latest:
                names = complete_sets(cfg.local_dir, cfg.database)
                if not names:
                    raise Refused("no_local_set")
                base = names[0]
            try:
                bset = load(cfg.local_dir, base, expect_env=cfg.environment)
            except SetError as exc:
                raise Refused(f"local_set_invalid:{exc}") from exc
            result = upload(cfg, bset, client, encrypt)
    except Refused as exc:
        print(json.dumps({"ok": False, "refused": str(exc)}))
        return 2
    except (Failed, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "failed": str(exc) or type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
