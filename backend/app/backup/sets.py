"""A local backup SET written by deploy/backup/hyperchat-pgbackup.sh: <db>_<UTC>.{dump, dump.sha256, witness,
manifest.json}. A set is complete only when its manifest exists; it is VALID only when every file the manifest names
exists as a plain file and matches its SHA-256. Nothing here follows a symlink or accepts a path from a manifest:
file names are derived from the set's base name and compared with what the manifest says."""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE = re.compile(r"^(?P<db>[a-z_][a-z0-9_]{0,62})_(?P<stamp>\d{8}T\d{6}Z)$")
ENV_DATABASE = {"prod": "hyperchat", "dev": "hyperchat_dev"}  # test: hc_backuptest_* (disposable only)


class SetError(Exception):
    """A backup set that must not be trusted (message: a reason code, no paths or values)."""


def env_of(database: str) -> str:
    for env, db in ENV_DATABASE.items():
        if database == db:
            return env
    if database.startswith("hc_backuptest_"):
        return "test"
    raise SetError("unknown_database")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _plain(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SetError(f"not_a_plain_file:{path.name}")
    return path


@dataclass(frozen=True)
class BackupSet:
    directory: Path
    base: str
    manifest: dict

    @property
    def database(self) -> str:
        return self.manifest["database"]

    @property
    def environment(self) -> str:
        return self.manifest["environment"]

    @property
    def dump(self) -> Path:
        return self.directory / f"{self.base}.dump"

    @property
    def witness(self) -> Path | None:
        return self.directory / f"{self.base}.witness" if self.manifest.get("witness") else None

    @property
    def files(self) -> list[Path]:
        names = [f"{self.base}.dump", f"{self.base}.dump.sha256", f"{self.base}.manifest.json"]
        if self.witness is not None:
            names.insert(2, f"{self.base}.witness")
        return [self.directory / n for n in names]


def load(directory: str | os.PathLike, base: str, *, expect_env: str | None = None) -> BackupSet:
    """The verified set `base` in `directory`, or SetError. `expect_env` (prod / dev / test) refuses a set of another
    environment (a DEV set offered for a production recovery, or the reverse)."""
    match = BASE.match(base or "")
    if not match:
        raise SetError("bad_set_name")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise SetError("bad_directory")
    if not (directory / f"{base}.manifest.json").exists():
        raise SetError("incomplete_set")  # the manifest is written last: without it the set never completed
    try:
        manifest = json.loads(_plain(directory / f"{base}.manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetError("manifest_unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise SetError("manifest_format")
    if manifest.get("database") != match["db"] or manifest.get("created_at") != match["stamp"]:
        raise SetError("manifest_does_not_match_its_name")
    if manifest.get("environment") != env_of(match["db"]):
        raise SetError("environment_does_not_match_database")
    if expect_env is not None and manifest["environment"] != expect_env:
        raise SetError("wrong_environment")
    dump = manifest.get("dump") or {}
    if dump.get("file") != f"{base}.dump" or not re.fullmatch(r"[0-9a-f]{64}", str(dump.get("sha256", ""))):
        raise SetError("manifest_dump_entry")
    if sha256_file(_plain(directory / f"{base}.dump")) != dump["sha256"]:
        raise SetError("dump_checksum_mismatch")
    try:
        recorded = _plain(directory / f"{base}.dump.sha256").read_text(encoding="utf-8").split()
    except OSError as exc:
        raise SetError("checksum_file_unreadable") from exc
    if recorded != [dump["sha256"], f"{base}.dump"]:
        raise SetError("checksum_file_mismatch")
    witness = manifest.get("witness")
    if witness is not None:
        if not isinstance(witness, dict) or witness.get("file") != f"{base}.witness":
            raise SetError("manifest_witness_entry")
        if sha256_file(_plain(directory / f"{base}.witness")) != witness.get("sha256"):
            raise SetError("witness_checksum_mismatch")
    return BackupSet(directory, base, manifest)


def complete_sets(directory: str | os.PathLike, database: str) -> list[str]:
    """Base names of the complete (manifest present) sets of `database`, newest first. Not yet verified: load()."""
    pattern = re.compile(rf"^{re.escape(database)}_\d{{8}}T\d{{6}}Z\.manifest\.json$")
    names = [p.name[: -len(".manifest.json")] for p in Path(directory).iterdir()
             if pattern.match(p.name) and p.is_file() and not p.is_symlink()]
    return sorted(names, reverse=True)
