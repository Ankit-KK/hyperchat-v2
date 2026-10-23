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
    compared; COMPLETE.json (hashes, sizes, no secrets) is written last: an off-site set without it is incomplete;
  * a rerun after a partial upload resumes: objects already there with the expected hash are kept, nothing replaced.
Exit: 0 ok, 2 refused (disabled / invalid configuration / wrong set), 1 failed.
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
        "GPG_RECIPIENT_FILE", "LOCAL_DIR", "DATABASE")


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
    return Config(env, endpoint.rstrip("/"), values["BUCKET"], env, values["ACCESS_KEY_ID"],
                  values["SECRET_ACCESS_KEY"], recipient, local_dir, database)


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


def _put_verified(client, cfg: Config, key: str, path: Path, sha: str) -> str:
    """PUT once (never over another object), then read it back and compare. 'kept' when it was already there."""
    existing = _exists(client, cfg.bucket, key)
    if existing is not None:
        if (existing.get("Metadata") or {}).get("sha256") != sha:
            raise Refused("object_exists_with_other_content")
        outcome = "kept"
    else:
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
        outcome = "uploaded"
    try:
        got = _sha256_stream(client.get_object(Bucket=cfg.bucket, Key=key)["Body"])
    except Exception as exc:
        raise Failed("readback_failed") from exc
    if got != sha:
        raise Failed("readback_checksum_mismatch")
    return outcome


def upload(cfg: Config, bset: BackupSet, client, encrypt=gpg_encrypt) -> dict:
    if bset.environment != cfg.environment or bset.database != cfg.database:
        raise Refused("set_of_another_environment")
    work = Path(tempfile.mkdtemp(prefix="hc-offsite-"))
    try:
        os.chmod(work, 0o700)
        home = work / "gnupg"
        home.mkdir(mode=0o700)
        objects = []
        for path in bset.files:
            enc = work / (path.name + ".gpg")
            encrypt(path, enc, cfg.recipient_file, home)
            sha, plain_sha = sha256_file(enc), sha256_file(path)
            if sha == plain_sha:
                raise Failed("encryption_did_not_change_the_data")
            key = f"{cfg.prefix}/{bset.base}/{enc.name}"
            outcome = _put_verified(client, cfg, key, enc, sha)
            objects.append({"key": key, "sha256": sha, "bytes": enc.stat().st_size, "outcome": outcome,
                            "plain_sha256": plain_sha, "plain_name": path.name})
            enc.unlink()
        complete = {"format": 1, "environment": bset.environment, "database": bset.database, "set": bset.base,
                    "created_at": bset.manifest["created_at"], "alembic_version": bset.manifest.get("alembic_version"),
                    "encryption": "gpg-public-key", "objects": [{k: o[k] for k in ("key", "sha256", "bytes",
                                                                                    "plain_sha256", "plain_name")}
                                                                 for o in objects]}
        marker = work / "COMPLETE.json"
        marker.write_text(json.dumps(complete, sort_keys=True), encoding="utf-8")
        _put_verified(client, cfg, f"{cfg.prefix}/{bset.base}/COMPLETE.json", marker, sha256_file(marker))
        return {"ok": True, "set": bset.base, "objects": [{k: o[k] for k in ("key", "outcome", "bytes")} for o in objects]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
