# DEV: legacy environment-file copies, retention and cleanup (plan)

**Status: a plan for review. Nothing here has been run.**
- Deleting any file needs the owner's explicit approval, separately for **batch A** and **batch B**.
- The inventory below comes from read-only checks on 2026-10-25. It records metadata, variable **names**, and yes/no comparisons with the active file only. No content, value or hash of a value was read into this document, a log or a report.
- **Scope:** DEV only. Production is excluded (see "Exclusions").

## 1. Inventory: the 11 copies
"Secret-bearing" means a variable whose name contains SECRET, KEY, TOKEN, PASSWORD or DSN. "Current" means equal to the active `/etc/hyperchat/api.env`.

| # | Path | Owner | Mode | Size | Modified (UTC) | Secret-bearing names in it | Value not current |
|---|---|---|---|---|---|---|---|
| 1 | `/etc/hyperchat/api.env.bak-phase1-74591d8b9d4f` | root:hyperchat | 640 | 474 | 2026-10-04 21:27 | none (11 variables; `DB_NAME=hyperchat_restore_test`) | – |
| 2 | `/etc/hyperchat/api.env.bak-pre-v2-f592286f4d9f` | root:hyperchat | 640 | 1143 | 2026-10-05 14:49 | `AUTH_SECRET` | none |
| 3 | `/etc/hyperchat/api.env.bak-pre-phase3-4093ec1d4f87` | root:hyperchat | 640 | 1127 | 2026-10-05 16:38 | `AUTH_SECRET` | none |
| 4 | `/etc/hyperchat/api.env.bak-pre-devsplit-20261006T180116Z` | root:hyperchat | 640 | 1471 | 2026-10-05 18:20 | `AUTH_SECRET` | none |
| 5 | `/etc/hyperchat/api.env.bak-pre-phase6-r2` | root:root | 640 | 1479 | 2026-10-06 21:31 | `AUTH_SECRET` | none |
| 6 | `/etc/hyperchat/api.env.bak-pre-phase7-razorpay` | root:root | 640 | 1997 | 2026-10-07 04:34 | `AUTH_SECRET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `UPLOAD_GRANT_SECRET` | none |
| 7 | `/etc/hyperchat/api.env.bak-20261014-000051` | root:root | 640 | 2953 | 2026-10-14 00:00 | 13 (Auth, R2, Pusher, Telegram, Discord, upload grant, Razorpay) | **`RAZORPAY_PRIMARY_KEY_ID`, `RAZORPAY_PRIMARY_KEY_SECRET`** (test mode) |
| 8 | `/root/api.env.bak-p11live` | root:hyperchat | 600 | 2903 | 2026-10-08 21:55 | 13 (as #7) | **the same superseded Razorpay test pair** |
| 9 | `/root/api.env.bak-p11e` | root:hyperchat | 600 | 2902 | 2026-10-08 22:14 | 13 (as #7) | **the same superseded Razorpay test pair** |
| 10 | `/root/api.env.bak-p11e-disable` | root:hyperchat | 600 | 2952 | 2026-10-09 12:24 | 13 (as #7) | **the same superseded Razorpay test pair** |
| 11 | `/root/rehearsal-dev/api.env.before-C` | root:hyperchat | 600 | 2954 | 2026-10-14 00:02 | 13 | none (all current) |

**Parent directories:**
- `/etc/hyperchat`: root:hyperchat 750.
- `/root` and `/root/rehearsal-dev`: root:root 700.

All 11 are regular files with a single link; none is identical to the active file. Copies 7–10 hold one identical Razorpay key pair (checked yes/no). In the active file, `before-C` holds a different, newer pair. Both key IDs are `rzp_test_` (mode checked, never the value).

**Not in scope, and never touched:**

| File | Why |
|---|---|
| `/etc/hyperchat/api.env` (root:hyperchat 600, 3181 bytes, modified 2026-10-24 16:53) | the active DEV file |
| `/etc/hyperchat-prod/api.env` | production |
| `/var/backups/hyperchat` | production database backups |

## 2. Dependencies (procedures checked)
| Procedure | Uses a copy? |
|---|---|
| `docs/operations/dev-rehearsal-0020-0028.md`, Phase C (C1 creates, the C rollback restores) and Phase F ("Returning to `$ST/api.env.before-C` is a separate decision") | **#11 only** (`$ST/api.env.before-C`) |
| `deploy/README.md` "Rollback" (release link back, restore the previous `api.env` *if it changed*) | none: `api.env` has not changed since 2026-10-24 16:53, so the current rollback target `c2fe883be71d` and the running `a9b9486efd86` use the same file |
| `docs/operations/email-category-keys.md` (key installation, rollback) | none: it writes its own dated rollback copy |
| `docs/backup-recovery.md`, `hyperchat-pgbackup` | none: database backups only; `/etc/hyperchat` is not backed up |
| systemd units, cron, `/usr/local/{s,}bin`, scripts under `/root` | none. Only root's shell history mentions #5 and #6, as past commands |

## 3. Classification
| Class | Copies | Reason |
|---|---|---|
| **REQUIRED FOR ROLLBACK** (keep) | #11 | The Phase C restore source and the Phase F option. Keep until Phase F is formally retired by the owner |
| **HISTORICAL / REDUNDANT**, batch **A** | #1–#6 | Pre-v2 and early-phase states (2026-10-04 to 10-07) for releases far behind schema 0028. No procedure uses them. Every secret in them is still current, so they add no credential the active file doesn't already hold, but they would keep those secrets after any future rotation |
| **HISTORICAL / REDUNDANT, with a superseded credential**, batch **B** | #7–#10 | Bots phase 11 experiments (`BOTS_ENABLED=true` in #9 and #10) and the state just before the Razorpay test key change on 2026-10-14. No procedure uses them. They are the only copies of the **superseded Razorpay test key pair** |
| **UNCERTAIN** | none | #7's origin is not written down (a manual copy 90 s before `before-C`), but nothing depends on it, and its only unique content is the superseded key pair |

## 4. Credential exposure and rotation
- **Superseded Razorpay test key pair (#7–#10).**
  - **Who can read it:** root only. #7 is root:root 640 in a group-readable directory, but group `root` has no members besides root; #8–#10 are in `/root`, mode 700.
  - **Recommendation:** the owner checks in the Razorpay **test** dashboard whether that key is still active, and revokes it if so. It cannot move real money, but it can create and read test-mode data.
  - **Why revoking matters most:** deleting the copies does not erase the data from the disk. ext4 has no full-disk encryption here, and `shred` is unreliable on a journaling filesystem. **Revocation is the real fix.** No rotation is performed by this plan.
- **Current secrets in the historical copies (`AUTH_SECRET`, R2, Pusher, Telegram, Discord, Razorpay webhook secret, upload grant secret).**
  - **No new exposure today:**
    - #2–#4 are root:hyperchat 640, so the `hyperchat` service user can read them, but they hold only `AUTH_SECRET`, which that process already has.
    - The others are root-only.
  - **The risk is persistence:** after a future rotation these copies would still hold the old values. No rotation of current secrets is needed because of these copies.
- **Root's shell history** mentions two copies by name. Commands typed in earlier phases could include secrets. It was **not** read; the owner may review it privately.

## 5. After cleanup: recovery
- **Rollback to Phase C state:** #11 (kept).
- **Release rollback:** `deploy/README.md` "Rollback" needs no copy, since `api.env` is unchanged.
- **Key installation:** `install_email_keys` writes a dated rollback copy of the then-current file.
- **Disaster recovery of the active file:** there is no off-server copy of `/etc/hyperchat/api.env`, and there wasn't one before either. None of the copies to be deleted serves this purpose.
  - **Owner decision, recommended:** an encrypted, off-server, owner-held copy of the *current* file. For example, encrypt it to the owner's own key and keep it in the owner's password manager, created by the owner outside any session that logs output.
  - The alternative is to rebuild it from the provider dashboards and the repository templates.
  - No historical copy needs an encrypted archive.

## 6. Cleanup procedure (runs only after approval, one batch at a time)
**Safeguards:**
- **Exact paths, pinned:** a fixed manifest pins path, owner, mode, link count, inode, size, mtime and ctime (nanoseconds). Any difference (a changed, replaced, re-owned, re-linked or touched file) refuses the **whole batch**, and nothing is deleted.
- **No symlinks:** each parent is opened with `O_NOFOLLOW` and checked (owner, mode, inode). Every file is checked with `lstat` through that directory fd, so it must be a regular file, not a symlink. It is deleted with `unlink` through the same fd, so a path swapped between the check and the unlink cannot redirect the deletion.
- **Exclusions:**
  - a hard-coded refusal for `/etc/hyperchat/api.env`, `/root/rehearsal-dev/api.env.before-C`, anything under `/etc/hyperchat-prod`, `/var/backups` or `/opt`, and any path not in the manifest;
  - the active DEV file and the production file are compared before and after, and must be unchanged.
- **Dry run by default.** Deleting needs `--delete` **and** the batch's file count typed back.
- Nothing is printed except paths, sizes and results. No content is read.

**1. Define the function (changes nothing).** Paste this block as is.
```bash
cleanup_env_backups() (   # usage: cleanup_env_backups A|B [--delete <count>]
  set -u
  read -r -d '' CLEAN <<'PY'
import grp, os, pwd, stat, sys
# path|owner:group|mode|links|inode|size|mtime_ns|ctime_ns (read-only inventory, 2026-10-25)
MANIFEST = {
 "A": """/etc/hyperchat/api.env.bak-phase1-74591d8b9d4f|root:hyperchat|640|1|503229|474|1791149279692849237|1791211780579658535
/etc/hyperchat/api.env.bak-pre-v2-f592286f4d9f|root:hyperchat|640|1|503217|1143|1791211780595709462|1791218284928100508
/etc/hyperchat/api.env.bak-pre-phase3-4093ec1d4f87|root:hyperchat|640|1|503205|1127|1791218284928234810|1791224430581366405
/etc/hyperchat/api.env.bak-pre-devsplit-20261006T180116Z|root:hyperchat|640|1|502983|1471|1791224430582813486|1791309676034512906
/etc/hyperchat/api.env.bak-pre-phase6-r2|root:root|640|1|512531|1479|1791322288528298032|1791322288528298032
/etc/hyperchat/api.env.bak-pre-phase7-razorpay|root:root|640|1|512519|1997|1791347698424404480|1791347698424404480""",
 "B": """/etc/hyperchat/api.env.bak-20261014-000051|root:root|640|1|503126|2953|1791936051149059601|1791936051149059601
/root/api.env.bak-p11live|root:hyperchat|600|1|9623|2903|1791496525875078657|1791497684109169678
/root/api.env.bak-p11e|root:hyperchat|600|1|135|2902|1791497684111384386|1791548672529476969
/root/api.env.bak-p11e-disable|root:hyperchat|600|1|1244|2952|1791548672530476979|1791549876297244381""",
}
PARENTS = {"/etc/hyperchat": ("root:hyperchat", 0o750, 503462), "/root": ("root:root", 0o700, 1165)}
NEVER = ("/etc/hyperchat/api.env", "/root/rehearsal-dev/api.env.before-C")
NEVER_UNDER = ("/etc/hyperchat-prod/", "/var/backups/", "/opt/", "/root/rehearsal-dev/")
GUARDED = ("/etc/hyperchat/api.env", "/etc/hyperchat-prod/api.env")
args = sys.argv[1:]
if not args or args[0] not in MANIFEST:
    sys.exit("STOP: usage: cleanup_env_backups A|B [--delete <count>]")
entries = [line.split("|") for line in MANIFEST[args[0]].splitlines()]
delete = args[1:2] == ["--delete"]
if delete and args[2:3] != [str(len(entries))]:
    sys.exit(f"STOP: --delete needs the batch's file count ({len(entries)}) typed back; nothing deleted")
def owner(s):
    return f"{pwd.getpwuid(s.st_uid).pw_name}:{grp.getgrgid(s.st_gid).gr_name}"
def snap(p):
    s = os.lstat(p); return (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_uid, s.st_gid)
guard_before = {p: snap(p) for p in GUARDED}
dirs, problems = {}, []
def check(path, own, mode, links, ino, size, mtime, ctime):
    if path in NEVER or path.startswith(NEVER_UNDER) or os.path.normpath(path) != path:
        return "excluded path"
    parent, name = os.path.split(path)
    if parent not in PARENTS:
        return "parent not allowed"
    if parent not in dirs:
        try:
            dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return "parent is not a real directory (symlink or missing)"
        d = os.fstat(dfd)
        if (owner(d), d.st_mode & 0o7777, d.st_ino) != PARENTS[parent]:
            os.close(dfd); return "parent directory changed (owner, mode or identity)"
        dirs[parent] = dfd
    try:
        s = os.stat(name, dir_fd=dirs[parent], follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(s.st_mode):
        return "not a regular file (symlink or other)"
    got = (owner(s), f"{s.st_mode & 0o7777:o}", str(s.st_nlink), str(s.st_ino), str(s.st_size), str(s.st_mtime_ns), str(s.st_ctime_ns))
    want = (own, mode, links, ino, size, mtime, ctime)
    names = ("owner", "mode", "links", "inode", "size", "mtime", "ctime")
    diff = [n for n, g, w in zip(names, got, want) if g != w]
    return "changed: " + ", ".join(diff) if diff else None
for e in entries:
    why = check(*e)
    if why:
        problems.append(f"{e[0]}: {why}")
if problems:
    print("\n".join(problems))
    sys.exit("STOP: the batch does not match the approved manifest; nothing deleted")
if not delete:
    for e in entries:
        print(f"would delete {e[0]} ({e[5]} bytes)")
    print(f"dry run: {len(entries)} files match the manifest; nothing deleted")
    sys.exit(0)
for e in entries:
    if check(*e):   # once more, immediately before the unlink, through the same directory fd
        sys.exit(f"STOP: {e[0]} changed during the run; files already deleted are listed above")
    parent, name = os.path.split(e[0])
    os.unlink(name, dir_fd=dirs[parent])
    print(f"deleted {e[0]}")
for dfd in dirs.values():
    os.fsync(dfd); os.close(dfd)
left = [e[0] for e in entries if os.path.lexists(e[0])]
changed = [p for p in GUARDED if snap(p) != guard_before[p]]
if left or changed:
    sys.exit(f"STOP: still present: {left}; guarded files changed: {changed}")
print(f"done: {len(entries)} deleted; {', '.join(GUARDED)} unchanged")
PY
  python3 -c "$CLEAN" "$@"
)
```

**2. Dry run (read-only).** Expect `dry run: 6 files match` (A) or `4 files match` (B).
```bash
cleanup_env_backups A
cleanup_env_backups B
```

**3. Delete one approved batch.** The count is typed back as confirmation.
```bash
cleanup_env_backups A --delete 6      # only after approval of batch A
cleanup_env_backups B --delete 4      # only after approval of batch B (and the owner's Razorpay test-key decision)
```

**4. Check (read-only).** Expect only `api.env` (and any later dated key-install copy) in `/etc/hyperchat`. In `/root` and `/root/rehearsal-dev`, expect no `api.env.bak*` files and `api.env.before-C` still present.
```bash
ls -la /etc/hyperchat; ls -la /root/api.env* /root/rehearsal-dev/api.env.before-C 2>&1
stat -c '%n %U:%G %a %s %y' /etc/hyperchat/api.env /etc/hyperchat-prod/api.env
```

**If a STOP appears:** nothing was deleted, except in the case "changed during the run", which lists what was already deleted. Report it, and don't edit the manifest without a new read-only inventory and a new approval.

**Limits:**
- Deletion removes the names. On this unencrypted ext4 disk, the data can remain in freed blocks until overwritten. This is why the superseded Razorpay test key should be revoked, not merely deleted.
- Root's shell history is not part of this plan.
