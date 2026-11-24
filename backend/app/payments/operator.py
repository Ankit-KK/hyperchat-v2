"""PAY-15 (D17) operator commands: `python -m app.payments.operator <command>`.

Stage 6a-2 implements only `verify` (read-only, plan revision 10 §9 / D-6):

    HYPERCHAT_READONLY_DSN='host=... dbname=... user=hyperchat_readonly' \
        python -m app.payments.operator verify --max-seconds N [--creator UUID | --donation UUID] [--output FILE]

It connects as hyperchat_readonly, prints the canonical JSON report (ids, enums and amounts only) and exits with the
D-7 code: 0 clean, 1 violation, 2 refused, 3 attention. A scoped run (--creator / --donation) has every gate false.
`--max-seconds` is required (revision 7 §2.7.1: no production default). The scope is validated before any connection.
`--output` publishes without ever overwriting (§2.7.4): an exclusive temporary file, fsync, then a hard link.
Stage 6a-3 adds the owner-only writing commands (app/payments/accounting_ops.py), report-only by default:

    HYPERCHAT_ACCOUNTING_OWNER_DSN='host=... dbname=... user=hyperchat_migrator' \
        python -m app.payments.operator {state [--to S] [--mode M] | reconcile | resolve keep|apply | revert}
        [--donation UUID ...] [--apply --confirm-database DB --operator ID --reason TEXT]

Exit codes as verify's (D-7): 0 nothing to do / all applied, 3 work planned, 1 a donation refused (or a reconcile's
second report not zero), 2 refused before anything was written.

Stage 6a-4 adds the read-only L4 deploy check, run by the deploy runbook before any code rollback:

    HYPERCHAT_READONLY_DSN='... user=hyperchat_readonly' \
        python -m app.payments.operator deploy-check --target-release RELEASE [--releases-root DIR]

Exit 0 when the rollback is allowed; 2 (refused) when the target release lacks the reversal-aware writer (migration
0032) while any applied reversal exists (revert first, L3), and on anything it cannot establish (fail closed).
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

from app.payments import accounting_ops as ops
from app.payments import switch
from app.payments import verify as verifier

WRITERS = ("state", "reconcile", "resolve", "revert")  # stage 6a-3 (accounting_ops)
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
    d = sub.add_parser("deploy-check", help="6a-4 L4: may the code be rolled back to this release? (read-only)")
    d.add_argument("--target-release", dest="target",
                   help="the release to roll back to: its 12-hex id as the host names it, or its 40-hex commit")
    d.add_argument("--releases-root", dest="root", default=switch.RELEASES_ROOT,
                   help=f"the directory holding the release trees (default {switch.RELEASES_ROOT})")
    for name in WRITERS:
        w = sub.add_parser(name, help=f"6a-3 {name} (owner; report-only unless --apply)")
        if name == "state":
            w.add_argument("--to", choices=("off", "active", "frozen"))
            w.add_argument("--mode", choices=("hold", "apply"))
        else:
            if name == "resolve":
                w.add_argument("action", nargs="?", choices=("keep", "apply"))
            w.add_argument("--donation", action="append", help="limit to this donation id (repeatable)")
        w.add_argument("--apply", action="store_true", help="write (default: report only)")
        w.add_argument("--confirm-database", dest="confirm", help="must equal the connected database (with --apply)")
        w.add_argument("--operator", help="the operator id recorded with the run / state change")
        w.add_argument("--reason", help="the reason recorded (10-500 characters)")
    args, extra = parser.parse_known_args(argv)
    if extra:
        if args.command != "verify":
            return _refuse(f"unrecognized arguments: {' '.join(extra)}")
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    if args.command == "deploy-check":
        return run_deploy_check(args)
    if args.command != "verify":
        return run_writer(args)
    return run_verify(args)


def run_deploy_check(args) -> int:
    """L4 (accounting plan): refuse a code rollback below the writer release while any applied reversal exists."""
    try:
        carries = switch.carries_writer_rule(args.root, args.target)
    except ValueError as exc:
        return _refuse(f"--target-release: {exc}")
    dsn = os.environ.get("HYPERCHAT_READONLY_DSN", "")
    if not dsn:
        return _refuse("HYPERCHAT_READONLY_DSN is not set")
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10,
                             application_name="hyperchat-accounting-deploy-check") as conn:
            applied = conn.execute(switch.APPLIED).fetchone()[0]
    except psycopg.Error as exc:
        return _refuse(f"cannot read the applied reversals: {type(exc).__name__}")
    out = {"command": "deploy-check", "target_release": args.target, "carries_writer_rule": carries,
           "writer_revision": switch.ACCOUNTING_WRITER_REVISION, "applied_reversals": applied}
    if not carries and applied:
        return _refuse(f"release {args.target} predates the reversal-aware writer ({switch.WRITER_MIGRATION}) and "
                       f"{applied} donation(s) have an applied reversal: revert them first (L3)", **out)
    sys.stdout.write(verifier.canonical({**out, "status": "clean", "exit_code": 0}) + "\n")
    return 0


def _refuse(reason: str, **extra) -> int:
    sys.stdout.write(verifier.canonical({**extra, "status": "refused", "exit_code": ops.EXIT["refused"],
                                         "reason": reason}) + "\n")
    return ops.EXIT["refused"]


def run_writer(args) -> int:
    """6a-3: validate everything before connecting; report-only unless --apply; canonical JSON out."""
    action = {"resolve": getattr(args, "action", None)}.get(args.command, args.command)
    if action is None:
        return _refuse("resolve needs an action: keep or apply")
    try:
        ids = ops.validate(apply=args.apply, confirm=args.confirm, operator=args.operator, reason=args.reason,
                           donations=getattr(args, "donation", None))
        conn = ops.connect(os.environ.get(ops.OWNER_DSN_ENV))
    except ops.Refusal as exc:
        return _refuse(str(exc))
    except psycopg.Error as exc:
        return _refuse(f"cannot connect as the owner: {type(exc).__name__}")
    try:
        with conn:
            if args.command == "state":
                out = ops.run_state(conn, to=args.to, mode=args.mode, apply=args.apply, confirm=args.confirm,
                                    operator=args.operator, reason=args.reason)
            else:
                out = ops.run_action(conn, action, apply=args.apply, confirm=args.confirm, operator=args.operator,
                                     reason=args.reason, ids=ids)
    except ops.Refusal as exc:
        return _refuse(str(exc))
    except psycopg.Error as exc:
        return _refuse(f"database error before writing: {type(exc).__name__}")
    code = ops.EXIT[out["status"]]
    sys.stdout.write(verifier.canonical({**out, "exit_code": code}) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
