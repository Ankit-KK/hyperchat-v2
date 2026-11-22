"""PAY-15 (D17) operator commands: `python -m app.payments.operator <command>`.

Stage 6a-2 implements only `verify` (read-only, plan revision 10 §9 / D-6):

    HYPERCHAT_READONLY_DSN='host=... dbname=... user=hyperchat_readonly' \
        python -m app.payments.operator verify --max-seconds N [--creator UUID | --donation UUID] [--output FILE]

It connects as hyperchat_readonly, prints the canonical JSON report (ids, enums and amounts only) and exits with the
D-7 code: 0 clean, 1 violation, 2 refused, 3 attention. A scoped run (--creator / --donation) has every gate false.
`--max-seconds` is required (revision 7 §2.7.1: no production default). The scope is validated before any connection.
`--output` publishes without ever overwriting (§2.7.4): an exclusive temporary file, fsync, then a hard link.
The writing commands (state, reconcile, resolve, revert) belong to stage 6a-3 and are refused here (exit 2).
"""

import argparse
import errno
import hashlib
import os
import re
import secrets
import stat
import sys

import psycopg

from app.payments import verify as verifier

LATER = ("state", "reconcile", "resolve", "revert")  # stage 6a-3
# PROVISIONAL (owner item 5: the allow-list format and location are pending): a root-owned file outside the
# database and outside the invocation, one role name per line, '#' comments. A missing file means no approved
# administrator (fail closed: every other superuser is unexpected_superuser).
ADMIN_ALLOWLIST = "/etc/hyperchat/verify-administrators"
TRUSTED_UID = 0  # PROVISIONAL (owner item 5): the only owner whose allow-list authorizes administrators
MAX_ALLOWLIST_BYTES = 65_536
_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_$]{0,62}")
_GROUP_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH
# POSIX guarantees both checks rely on: no-follow opens and directory-relative operations
_POSIX_SECURE = os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and \
    {os.open, os.stat, os.link, os.unlink} <= os.supports_dir_fd


def _trusted_ancestors(path: str, trusted_uids: set, what: str = "administrator allow-list"):
    """Every ancestor directory of `path` is a real directory (not a symlink), owned by a trusted uid, and not
    writable by group / others unless sticky (then others cannot rename or remove entries they do not own). So only
    a trusted principal can replace what `path` names after it was opened."""
    d = os.path.dirname(os.path.abspath(path))
    while True:
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"{what} path component is not a directory: {d}")
        if st.st_uid not in trusted_uids:
            raise ValueError(f"{what} directory has an untrusted owner: {d}")
        if st.st_mode & _GROUP_OTHER_WRITE and not st.st_mode & stat.S_ISVTX:
            raise ValueError(f"{what} directory is writable by group or others: {d}")
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def load_admin_allowlist(path: str = ADMIN_ALLOWLIST, trusted_uid: int = TRUSTED_UID) -> tuple[frozenset, str | None]:
    """The approved administrators and the file's SHA-256 (printed in the report; a digest is evidence of WHICH file
    was read, never of its authorization). Missing: none approved. On POSIX the file is opened without following a
    symlink and validated and read through that one descriptor (so a later pathname swap cannot change what was
    read): a regular file, owned by `trusted_uid`, not group / other writable, under trusted directories. Anything
    else refuses (ValueError). Without these guarantees (Windows) an existing allow-list refuses: it is never
    trusted on an inadequate check."""
    if not _POSIX_SECURE:
        if os.path.lexists(path):
            raise ValueError("administrator allow-list authorization is not supported on this platform")
        return frozenset(), None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return frozenset(), None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("administrator allow-list is a symlink") from None
        raise ValueError(f"administrator allow-list unreadable: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("administrator allow-list is not a regular file")
        if st.st_uid != trusted_uid:
            raise ValueError("administrator allow-list is not owned by the trusted owner")
        if st.st_mode & _GROUP_OTHER_WRITE:
            raise ValueError("administrator allow-list is writable by group or others")
        _trusted_ancestors(path, {0, trusted_uid})
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ALLOWLIST_BYTES:
                raise ValueError("administrator allow-list is too large")
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    names = set()
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise ValueError("administrator allow-list is not ASCII") from None
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not _ROLE_NAME.fullmatch(line):
            raise ValueError("administrator allow-list is malformed")
        names.add(line)
    return frozenset(names), hashlib.sha256(data).hexdigest()


class _Substituted(Exception):
    """The temporary pathname no longer names the inode this call created."""


def _ident(st) -> tuple:
    return st.st_dev, st.st_ino


def publish(text: str, dest: str, trusted_uids=None) -> str | None:
    """§2.7.4 inside a trust boundary: never overwrite, never publish or delete content this call did not write.
    Returns None once published, else the failed step (`dest` untouched).

    The directory is opened once (O_DIRECTORY | O_NOFOLLOW) and must be owned by a trusted uid (root or this
    process's effective uid) and not writable by group / others; every later operation is relative to that open
    directory, so a directory symlink or parent-path swap cannot redirect it. The temporary file is created with
    O_EXCL | O_NOFOLLOW; after fsync the temporary name must still identify the inode written (else `substituted`:
    nothing is published and the foreign file is left alone). The commit point links the OPEN inode itself
    (/proc/self/fd), never a name. Only a temporary name still bound to this call's inode is ever unlinked. The
    directory's ancestors must be trusted too (root / this uid, not group- / other-writable unless sticky). Without
    these primitives (Windows, or POSIX without /proc/self/fd): refused, never a weaker scheme."""
    if not _POSIX_SECURE or not os.path.isdir("/proc/self/fd"):  # no way to link the open inode itself
        return "unsupported_platform"
    trusted = {0, os.geteuid()} if trusted_uids is None else set(trusted_uids)
    directory, base = os.path.split(os.path.abspath(dest))
    try:  # the directory's own ancestors: trusted, so nobody else can swap the directory out from under the open
        _trusted_ancestors(directory, trusted, "output")
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except (OSError, ValueError):
        return "directory"
    fd = None
    try:
        st = os.fstat(dfd)
        if st.st_uid not in trusted or st.st_mode & _GROUP_OTHER_WRITE:
            return "directory"
        tmp = f"{base}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                         0o600, dir_fd=dfd)
        except OSError:  # EEXIST: the colliding file belongs to someone else and is never touched
            return "create"
        mine = _ident(os.fstat(fd))
        step = "write"
        try:
            view = memoryview((text + "\n").encode("ascii"))
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short write")
                view = view[n:]
            step = "fsync"
            os.fsync(fd)
            step = "verify"
            try:
                if _ident(os.stat(tmp, dir_fd=dfd, follow_symlinks=False)) != mine:
                    raise _Substituted
            except FileNotFoundError:
                raise _Substituted from None
            step = "link"
            try:  # the open inode itself: a pathname swap after the check cannot change what is linked
                os.link(f"/proc/self/fd/{fd}", base, dst_dir_fd=dfd, follow_symlinks=True)
            except FileNotFoundError:  # our inode lost its last name: it was swapped out after the check
                raise _Substituted from None
        except _Substituted:
            return "substituted"
        except OSError:
            _unlink_if_mine(tmp, dfd, mine)
            return step
        try:  # after the commit point: published whatever happens to the cleanup
            os.fsync(dfd)
        except OSError:
            pass
        if not _unlink_if_mine(tmp, dfd, mine):
            sys.stderr.write(f"warning: the report was published but its temporary file remains: {tmp}\n")
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(dfd)


def _unlink_if_mine(name: str, dfd: int, mine: tuple) -> bool:
    """Remove `name` only while it still names this call's inode (never a substituted file)."""
    try:
        if _ident(os.stat(name, dir_fd=dfd, follow_symlinks=False)) != mine:
            return False
        os.unlink(name, dir_fd=dfd)
        return True
    except OSError:
        return False


def _emit(report: dict, output: str | None) -> int:
    text = verifier.canonical(report)
    if output:
        failed = publish(text, output)
        if failed is not None:
            report = verifier.refused_report(f"output_not_published: {failed}", report.get("scope"))
            text = verifier.canonical(report)
    sys.stdout.write(text + "\n")
    return report["exit_code"]


def run_verify(args) -> int:
    try:  # F7, before any connection: an invalid scope never reaches the database
        kind, _ = verifier.scope_args(args.creator, args.donation)
    except verifier.Refused as exc:
        both = args.creator is not None and args.donation is not None
        scope = {} if both else {"kind": "creator" if args.creator is not None else "donation"}
        return _emit(verifier.refused_report(str(exc), scope), args.output)
    scope = {"kind": kind}
    try:
        max_seconds = float(args.max_seconds) if args.max_seconds is not None else None
        verifier.Budget(max_seconds)  # validated here too, so a bad value never opens a connection
    except (ValueError, verifier.Refused):
        return _emit(verifier.refused_report("--max-seconds must be a finite number > 0 (required)", scope),
                     args.output)
    try:
        admins, admins_sha = load_admin_allowlist()
    except ValueError as exc:
        return _emit(verifier.refused_report(str(exc), scope), args.output)
    dsn = os.environ.get("HYPERCHAT_READONLY_DSN", "")
    if not dsn:
        return _emit(verifier.refused_report("HYPERCHAT_READONLY_DSN is not set", scope), args.output)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                             application_name="hyperchat-accounting-verify") as conn:
            report = verifier.verify(conn, creator=args.creator, donation=args.donation, max_seconds=max_seconds,
                                     admins=admins, admin_list_sha256=admins_sha)
    except psycopg.Error as exc:
        report = verifier.refused_report(f"cannot connect: {type(exc).__name__}", scope)
    except Exception as exc:  # noqa: BLE001 - fail closed: an internal error is never a clean report
        report = verifier.refused_report(f"internal error: {type(exc).__name__}", scope)
    return _emit(report, args.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.payments.operator")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="read-only accounting verification (hyperchat_readonly)")
    group = v.add_mutually_exclusive_group()
    group.add_argument("--creator", help="limit to one creator id (every gate false)")
    group.add_argument("--donation", help="limit to one donation id (every gate false)")
    v.add_argument("--output", help="also publish the report to this new file (never overwritten)")
    v.add_argument("--max-seconds", dest="max_seconds", help="the verification budget in seconds (required)")
    for name in LATER:
        sub.add_parser(name, add_help=False)
    args, extra = parser.parse_known_args(argv)
    if args.command != "verify":
        sys.stdout.write(verifier.canonical({"status": "refused", "exit_code": verifier.EXIT_CODES["refused"],
                                             "reason": f"'{args.command}' is not available in stage 6a-2"}) + "\n")
        return verifier.EXIT_CODES["refused"]
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return run_verify(args)


if __name__ == "__main__":
    sys.exit(main())
