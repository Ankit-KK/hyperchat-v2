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
    bytes) and reported as such; nothing is uploaded again;
  * every set is SIGNED (security audit run-4, NV4-4): encryption to a public key proves nothing about who uploaded,
    and the bucket's token can create new keys (bucket locks stop deletion and overwrite, not new objects), so a token
    holder could otherwise plant a self-consistent "newest" set. The exact COMPLETE.json bytes are signed with this
    environment's ed25519 key (`ssh-keygen -Y sign`, namespace hyperchat-offsite, principal hyperchat-offsite-<env>)
    into COMPLETE.json.sig, uploaded before the marker. verify, fetch, list and a re-run of upload accept a set only
    when that signature verifies against the ALLOWED-SIGNERS file, which the owner keeps a copy of off the server;
    unsigned or forged sets are refused. A set stamped after its provider upload time (a future-stamped plant) is
    refused too, and verify reports the upload time. Key paths are fixed per environment (no setting to change):
    /etc/hyperchat-backup/offsite-sign-<env>.key (private, root 0600, upload only) and .allowed (public).
Exit: 0 ok, 2 refused (disabled / invalid configuration / wrong set / unverifiable or unsigned remote state), 1 failed.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backup.sets import BASE, BackupSet, SetError, complete_sets, load, sha256_file

LOCAL_DIRS = {"prod": "/var/backups/hyperchat", "dev": "/var/backups/hyperchat-dev"}
DATABASES = {"prod": "hyperchat", "dev": "hyperchat_dev"}
KEYS = ("ENABLED", "ENVIRONMENT", "ENDPOINT", "BUCKET", "PREFIX", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY",
        "GPG_RECIPIENT_FILE", "LOCAL_DIR", "DATABASE", "STATE_DIR", "SIGNING_KEY_FILE", "ALLOWED_SIGNERS_FILE")
STATE_ROOT = "/var/lib/hyperchat-offsite"  # systemd StateDirectory=hyperchat-offsite/<env>, root 0700
KEY_ROOT = "/etc/hyperchat-backup"  # prod/dev signing paths are derived, so the owner can stage them before the code
SIGN_NAMESPACE = "hyperchat-offsite"
CLOCK_SKEW = timedelta(minutes=10)  # a set's stamp may not be later than its provider upload time beyond this


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
    signing_key: Path
    allowed_signers: Path

    @property
    def principal(self) -> str:
        return f"hyperchat-offsite-{self.environment}"

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
    if env == "test":  # tests name their own key files; prod and dev use the fixed paths
        signing_key, allowed = Path(values.get("SIGNING_KEY_FILE", "")), Path(values.get("ALLOWED_SIGNERS_FILE", ""))
        if not signing_key.is_absolute() or not allowed.is_absolute() or str(signing_key).startswith(KEY_ROOT) \
                or str(allowed).startswith(KEY_ROOT):
            raise Refused("config_test_scope")
    elif "SIGNING_KEY_FILE" in values or "ALLOWED_SIGNERS_FILE" in values:
        raise Refused("config_signing_paths_are_fixed")
    else:
        signing_key, allowed = Path(f"{KEY_ROOT}/offsite-sign-{env}.key"), Path(f"{KEY_ROOT}/offsite-sign-{env}.allowed")
    return Config(env, endpoint.rstrip("/"), values["BUCKET"], env, values["ACCESS_KEY_ID"],
                  values["SECRET_ACCESS_KEY"], recipient, local_dir, database, state_dir, signing_key, allowed)


def _own_file(path: Path, private: bool, reason: str) -> Path:
    """A plain file of ours (never a symlink): private (0600) for the signing key, not writable by others for the
    allowed-signers file, which decides which sets are trusted."""
    try:
        st = path.lstat()
    except OSError as exc:
        raise Refused(reason) from exc
    mask = 0o077 if private else 0o022
    if not stat.S_ISREG(st.st_mode) or st.st_mode & mask or (hasattr(os, "geteuid") and st.st_uid != os.geteuid()):
        raise Refused(reason)
    return path


def sign(cfg: Config, data: bytes) -> bytes:
    """This environment's signature over `data` (ssh-keygen -Y sign: ed25519, no agent, the key file read only)."""
    key = _own_file(cfg.signing_key, True, "signing_key_missing_or_not_private")
    try:
        sig = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", SIGN_NAMESPACE], input=data,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise Failed("signing_failed") from exc
    if not sig.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        raise Failed("signing_failed")
    check_signature(cfg, data, sig)  # a signature the allowed-signers file would refuse is never uploaded
    return sig


def check_signature(cfg: Config, data: bytes, sig: bytes) -> None:
    """Refused unless `sig` is a valid signature over exactly `data` by this environment's allowed signer."""
    allowed = _own_file(cfg.allowed_signers, False, "allowed_signers_missing_or_writable")
    with tempfile.TemporaryDirectory(prefix="hc-offsite-sig-") as tmp:
        sig_file = Path(tmp) / "COMPLETE.json.sig"
        sig_file.write_bytes(sig)
        try:
            r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", cfg.principal,
                                "-n", SIGN_NAMESPACE, "-s", str(sig_file)], input=data, capture_output=True)
        except OSError as exc:
            raise Failed("signature_check_unavailable") from exc
    expected = f'Good "{SIGN_NAMESPACE}" signature for {cfg.principal} with '.encode()
    if r.returncode != 0 or not r.stdout.startswith(expected):
        raise Refused("offsite_set_signature_invalid")


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


def _get(client, cfg: Config, key: str) -> bytes | None:
    """An object's bytes, or None when it does not exist (any other error is a failure, never 'absent')."""
    try:
        return client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()
    except Exception as exc:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise Failed("get_failed") from exc


def authentic(cfg: Config, base: str, client) -> tuple[bytes, bytes, dict, datetime]:
    """The off-site set `base` only when it is a set of this environment's database, its COMPLETE.json is signed by
    this environment's allowed signer, and its stamp is not later than its provider upload time. Returns the marker's
    exact bytes, its signature, the parsed marker and the upload time. Every expected hash and name used afterwards
    comes from this signed marker, never from unsigned remote state."""
    match = BASE.match(base or "")
    if not match or match["db"] != cfg.database:
        raise Refused("not_a_set_of_this_environment")
    key = f"{cfg.prefix}/{base}/COMPLETE.json"
    raw = _get(client, cfg, key)
    if raw is None:
        raise Failed("not_complete_offsite")
    sig = _get(client, cfg, key + ".sig")
    if sig is None:
        raise Refused("offsite_set_unsigned")
    check_signature(cfg, raw, sig)
    try:
        marker = json.loads(raw)
    except ValueError as exc:
        raise Refused("marker_unreadable") from exc
    if (marker.get("environment"), marker.get("database"), marker.get("set")) != (cfg.environment, cfg.database, base):
        raise Refused("marker_of_another_environment_or_set")
    head = _exists(client, cfg.bucket, key) or {}
    uploaded = head.get("LastModified")
    if not isinstance(uploaded, datetime):
        raise Failed("upload_time_unknown")
    stamp = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if stamp > uploaded + CLOCK_SKEW:
        raise Refused("offsite_set_stamped_after_its_upload")
    return raw, sig, marker, uploaded


def _already_complete(cfg: Config, bset: BackupSet, client, recipient_sha: str) -> dict:
    """The off-site set exists: accept it only when it is exactly this local set, signed by this environment's key
    (every object's bytes checked). An unsigned or foreign marker under this set's name is refused, never trusted."""
    _, _, marker, _ = authentic(cfg, bset.base, client)
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


def _signature_of(cfg: Config, marker: Path, state: Path) -> Path:
    """The marker's signature, made once and kept with the state, so a retried upload offers the same bytes."""
    sig = state / "COMPLETE.json.sig"
    data = marker.read_bytes()
    if sig.is_symlink() or (sig.exists() and not sig.is_file()):
        raise Refused("local_state_unreadable")
    if sig.exists():
        try:
            check_signature(cfg, data, sig.read_bytes())
            return sig
        except Refused:
            sig.unlink()  # made by another key (rotated before the retry): sign again
    _write_atomically(sig, sign(cfg, data))
    return sig


def upload(cfg: Config, bset: BackupSet, client, encrypt=gpg_encrypt) -> dict:
    if bset.environment != cfg.environment or bset.database != cfg.database:
        raise Refused("set_of_another_environment")
    recipient_sha = sha256_file(cfg.recipient_file)
    if _exists(client, cfg.bucket, f"{cfg.prefix}/{bset.base}/COMPLETE.json") is not None:
        return _already_complete(cfg, bset, client, recipient_sha)
    _own_file(cfg.signing_key, True, "signing_key_missing_or_not_private")  # refuse before anything is uploaded
    _own_file(cfg.allowed_signers, False, "allowed_signers_missing_or_writable")
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
        sig = _signature_of(cfg, marker, state)  # the signature goes first: the marker still means "complete"
        _put_verified(client, cfg, f"{cfg.prefix}/{bset.base}/COMPLETE.json.sig", sig, sha256_file(sig))
        _put_verified(client, cfg, f"{cfg.prefix}/{bset.base}/COMPLETE.json", marker, sha256_file(marker))
        shutil.rmtree(state)  # local working copies only; the off-site set is complete and verified
        return {"ok": True, "set": bset.base, "objects": [{k: o[k] for k in ("key", "outcome", "bytes")} for o in objects]}
    finally:
        shutil.rmtree(home, ignore_errors=True)


def verify(cfg: Config, base: str, client) -> dict:
    """The signed COMPLETE.json, then every object it names; compare SHA-256. Decryption is the owner's (offline key)."""
    _, _, marker, uploaded = authentic(cfg, base, client)
    for obj in marker["objects"]:
        if not obj["key"].startswith(f"{cfg.prefix}/{base}/"):
            raise Refused("marker_key_outside_the_set")
        try:
            got = _sha256_stream(client.get_object(Bucket=cfg.bucket, Key=obj["key"])["Body"])
        except Exception as exc:
            raise Failed("object_missing") from exc
        if got != obj["sha256"]:
            raise Failed("object_checksum_mismatch")
    return {"ok": True, "set": base, "objects": len(marker["objects"]), "signed_by": cfg.principal,
            "uploaded_at": uploaded.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def fetch(cfg: Config, base: str, client, to: Path) -> dict:
    """Download one COMPLETE, SIGNED off-site set into a NEW private directory, verifying every object's SHA-256. The
    files stay encrypted: decrypting them is the owner's step, offline, with the private key (docs/backup-recovery.md).
    COMPLETE.json and COMPLETE.json.sig are kept, so the owner can check the signature again where they decrypt."""
    if to.exists():
        raise Refused("target_exists")
    marker_raw, sig, marker, _ = authentic(cfg, base, client)
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
    (to / "COMPLETE.json.sig").write_bytes(sig)
    return {"ok": True, "set": base, "objects": len(marker["objects"]), "to": str(to), "encrypted": True}


def list_complete(cfg: Config, client) -> tuple[list[str], dict[str, str]]:
    """Complete sets of this environment, newest first, that are SIGNED by its allowed signer; every other marker
    (an unsigned, forged or future-stamped set, or a name that is not a set of this database) is reported apart, so
    a planted set can never appear as the newest."""
    paginator = client.get_paginator("list_objects_v2")
    names = []
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=f"{cfg.prefix}/"):
        for obj in page.get("Contents", []):
            parts = obj["Key"].split("/")
            if len(parts) == 3 and parts[2] == "COMPLETE.json":
                names.append(parts[1])
    sets, refused = [], {}
    for name in sorted(names, reverse=True):
        try:
            authentic(cfg, name, client)
            sets.append(name)
        except (Refused, Failed) as exc:
            refused[name] = str(exc)
    return sets, refused


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
            sets, refused = list_complete(cfg, client)
            result = {"ok": True, "sets": sets, "refused": refused}
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
