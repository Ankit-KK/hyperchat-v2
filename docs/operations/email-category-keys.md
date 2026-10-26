# Installing the five DEV email keys (operator runbook)

**Status: instructions only. Nothing here has been run.** Installing the keys, changing `api.env`, restarting the API and activating any category each need **separate owner approval**. These steps are for DEV (`/etc/hyperchat/api.env`) only. Production has no approved sending domains: the API refuses to start with an active category in production.

## Rules
- **A key value never appears in a command line, shell history, `ps`, a log, terminal output, chat, Git or a PR.**
  - Keys are pasted only at a hidden `read -rs` prompt.
  - There is no staging file. On disk they exist only as an unnamed candidate (freed automatically if the process dies), for a few system calls as a named candidate next to `api.env`, and then in `api.env` itself. A crash in that short window leaves a candidate that the next run detects; see "Where the keys exist" below.
  - Every check prints **variable names**, never values.
- **One domain-restricted sending key per category.**
  - The API refuses a key that is shared with another active category.
  - It refuses a key that doesn't look like a Resend key (`re_…`).
- **Installing is not activating.**
  - `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES` stays empty and `HYPERCHAT_EMAIL_BACKEND` stays `disabled` until activation is separately approved.
  - Order of activation: `onboarding` first, then `authentication`.

| Category | Variables (`HYPERCHAT_EMAIL_<CATEGORY>_…`) | Sender | Verified domain |
|---|---|---|---|
| ONBOARDING | `FROM`, `VERIFIED_DOMAIN`, `RESEND_API_KEY` | `HyperChat <no-reply@onboarding.quicktestspace.site>` | `onboarding.quicktestspace.site` |
| AUTHENTICATION | same | `HyperChat Security <no-reply@auth.quicktestspace.site>` | `auth.quicktestspace.site` |
| NOTIFICATIONS | same | `HyperChat <no-reply@notifications.quicktestspace.site>` | `notifications.quicktestspace.site` |
| PAYMENTS | same | `HyperChat Payments <no-reply@payments.quicktestspace.site>` | `payments.quicktestspace.site` |
| SUPPORT | same | `HyperChat Support <no-reply@support.quicktestspace.site>` | `support.quicktestspace.site` |

**Today only two categories have a template:**
- ONBOARDING: every invitation, an admin's included.
- AUTHENTICATION: password reset.

The other three keys can be installed, but nothing sends through them.

## Steps (as root on the VPS, in an interactive shell)
### Where the keys exist, and what the procedure guarantees
| Copy | When | Protection |
|---|---|---|
| Subshell variable and pipe (memory) | while the function runs | ends with the function, on success, STOP or Ctrl-C |
| Validation input (memory: a pipe into the check, then a `memfd` owned by the check's own process) | during the check | never written to disk; the check runs with an empty environment (below). The pipe is root's, so the check copies it into its own `memfd` instead of reopening `/dev/stdin` (which the kernel refuses to `hyperchat`) |
| **Unnamed** candidate (`O_TMPFILE` in `/etc/hyperchat`) | while it is written and fsynced | no directory entry, mode 0600, root. If the process dies (including SIGKILL or a crash), the kernel frees it: no file is left to find |
| **Named** candidate `/etc/hyperchat/.api.env.email-keys-candidate.<pid>` | only between `link` and `rename`, a few system calls | already complete and fsynced, with `api.env`'s owner and mode, created with `O_EXCL`. Removed by `finally` on any Python failure |
| `/etc/hyperchat/api.env` | from the rename on | the intended copy; existing owner and mode kept |

**Validation is isolated.**
- The deployed code checks the complete candidate in memory (an anonymous `memfd`, no file name), as `hyperchat`, through `env -i` (an empty environment, plus a fixed `PATH` and `PYTHONDONTWRITEBYTECODE=1`). It runs `python -E -s`, so no `PYTHON*` variables and no user site.
- The check itself refuses to run if any `HYPERCHAT_*` variable is present. So no exported variable, and no `sudo` environment policy, can override a candidate value: pydantic would otherwise prefer the environment over the file.
- The application needs no other runtime configuration to build its settings: everything comes from the candidate.

**Other guarantees:**
- **Never activates.** The function refuses unless `HYPERCHAT_EMAIL_BACKEND` is `disabled` or absent and `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES` is empty. It writes only `_FROM`, `_VERIFIED_DOMAIN` and `_RESEND_API_KEY`, and restarts nothing.
- **Any refusal leaves `api.env` byte-for-byte unchanged.** No named candidate is left, and no rollback copy is written before validation passes.
- **No secret in a command line.** Keys travel only through stdin. The only arguments are the code and the category list.
- **Stale candidates block a new run.** The function stops before prompting while any `.api.env.email-keys-candidate.*` exists. `email_keys_recover` lists such files without reading them, and deletes only regular, root-owned, single-link files with exactly that name pattern.

**Limitations that cannot be removed:**
- **The named-candidate window.** A SIGKILL, power loss or host crash in the moment between `link` and `rename` leaves one named candidate. It is complete, root-owned, has `api.env`'s mode (DEV: 0600) and holds the new keys. The next run detects it and refuses; `email_keys_recover --delete` removes it.
- **Freed disk blocks.** Data blocks of freed files (an unnamed candidate after a crash, a removed candidate, every earlier version of `api.env`) can stay on the disk until they are overwritten. `shred` cannot fix this on ext4 with journaling (the DEV filesystem). Only full-disk encryption would, and that is out of scope.
- **The rollback copy.** It holds the previous `api.env`, with its other secrets, and lives in `/root` until it is removed (step 6).
- **Older backups.** The older `api.env.bak-*` files in `/etc/hyperchat`, from earlier phases (seven seen read-only on 2026-10-25), are plaintext copies of earlier secrets. This procedure never touches them; removing them is a separate owner decision.

**1. Preconditions (read-only).** Expect:
- a release with the email categories;
- `disabled` (or absent) and an empty (or absent) `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`;
- an `ext4` filesystem (`O_TMPFILE` support);
- no `.api.env.email-keys-candidate.*` file.
```bash
cat /opt/hyperchat/current/backend/RELEASE   # a9b9486efd86 or later
grep -E '^HYPERCHAT_EMAIL_(BACKEND|ACTIVE_CATEGORIES)=' /etc/hyperchat/api.env
stat -c '%U:%G %a' /etc/hyperchat/api.env; stat -f -c '%T' /etc/hyperchat
ls -a /etc/hyperchat
```

**2. Define the two functions (changes nothing).** Paste this block as is. It contains no secret.
```bash
email_keys_recover() (   # stale candidates from an interrupted run: list (never read); --delete removes only safe matches
  set -u; envdir=/etc/hyperchat
  find "$envdir" -maxdepth 1 -name '.api.env.email-keys-candidate.*' -printf '%y %u:%g %m %s bytes %TY-%Tm-%Td %TH:%TM %f\n'
  [ "${1:-}" = --delete ] || exit 0
  find "$envdir" -maxdepth 1 -name '.api.env.email-keys-candidate.*' -type f -user root -links 1 -printf 'removed %f\n' -delete
  left="$(find "$envdir" -maxdepth 1 -name '.api.env.email-keys-candidate.*' -printf '%y %u %f\n')"
  [ -z "$left" ] || { echo "STOP: not removed (not a regular, root-owned, single-link file; inspect by hand):"; echo "$left"; exit 1; }
)
install_email_keys() (   # a subshell: keys, variables and any failure stay inside it
  set -u; umask 077; envdir=/etc/hyperchat
  [ -z "$(find "$envdir" -maxdepth 1 -name '.api.env.email-keys-candidate.*' -print -quit)" ] \
    || { echo "STOP: a candidate from an interrupted run exists; run email_keys_recover (then --delete)"; exit 1; }
  cats="${*:-ONBOARDING AUTHENTICATION NOTIFICATIONS PAYMENTS SUPPORT}"
  keys=""
  for c in $cats; do
    case "$c" in ONBOARDING|AUTHENTICATION|NOTIFICATIONS|PAYMENTS|SUPPORT) ;; *) echo "STOP: unknown category $c"; exit 1 ;; esac
    read -rs -p "$c key (hidden): " k || { echo; echo "STOP: no input"; exit 1; }
    echo; keys="$keys$c=$k"$'\n'; k=""
  done
  read -r -d '' MERGE <<'PY'
import datetime, glob, os, subprocess, sys
ENV = "/etc/hyperchat/api.env"
APP = "/opt/hyperchat/current/backend"
RUN = ["sudo", "-u", "hyperchat", "env", "-i", "PATH=/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1", "../venv/bin/python", "-E", "-s"]
CATS = {"ONBOARDING": ("HyperChat", "onboarding"), "AUTHENTICATION": ("HyperChat Security", "auth"),
        "NOTIFICATIONS": ("HyperChat", "notifications"), "PAYMENTS": ("HyperChat Payments", "payments"),
        "SUPPORT": ("HyperChat Support", "support")}
VALIDATE = ("import os, sys\n"
            "leak = sorted(k for k in os.environ if k.upper().startswith('HYPERCHAT_'))\n"
            "if leak: sys.exit('STOP: HYPERCHAT_ variables in the validation environment: ' + ' '.join(leak))\n"
            # the stdin pipe belongs to root, so hyperchat cannot reopen /dev/stdin: copy it into this process's memfd
            "fd = os.memfd_create('candidate'); os.write(fd, sys.stdin.buffer.read())\n"
            "from app.core.config import Settings\n"
            "s = Settings(_env_file=f'/proc/self/fd/{fd}', email_active_categories=sys.argv[1])\n"
            "print('ok', sorted(s.email_categories))")
folder = os.path.dirname(ENV)
CAND = f".api.env.email-keys-candidate.{os.getpid()}"   # relative to the directory fd below
if glob.glob(os.path.join(folder, ".api.env.email-keys-candidate.*")):
    sys.exit("STOP: a candidate from an interrupted run exists; api.env unchanged")
pairs = [line.split("=", 1) for line in sys.stdin.read().splitlines() if line]
keys = dict(pairs)
if not keys or len(keys) != len(pairs) or set(keys) - CATS.keys():
    sys.exit("STOP: unexpected category list; api.env unchanged")
values = list(keys.values())
if len(set(values)) != len(values) or not all(v.startswith("re_") and len(v) > 10 and v == v.strip() for v in values):
    sys.exit("STOP: each key must be a distinct Resend key (values not shown); api.env unchanged")
current = open(ENV, encoding="utf-8").read().splitlines()
def value(name):
    found = [line.split("=", 1)[1].strip().strip("\"'") for line in current if line.split("=", 1)[0].strip() == name]
    return found[-1] if found else ""
if value("HYPERCHAT_EMAIL_BACKEND") not in ("", "disabled") or value("HYPERCHAT_EMAIL_ACTIVE_CATEGORIES"):
    sys.exit("STOP: email is not disabled with no active category; installing keys never activates; api.env unchanged")
new = []
for c, key in keys.items():
    name, label = CATS[c]
    new += [f"HYPERCHAT_EMAIL_{c}_FROM={name} <no-reply@{label}.quicktestspace.site>",
            f"HYPERCHAT_EMAIL_{c}_VERIFIED_DOMAIN={label}.quicktestspace.site", f"HYPERCHAT_EMAIL_{c}_RESEND_API_KEY={key}"]
names = {line.split("=", 1)[0] for line in new}
lines = [line for line in current if line.split("=", 1)[0].strip() not in names] + new
candidate = ("\n".join(lines) + "\n").encode()
configured = sorted(c.lower() for c in CATS if any(line.startswith(f"HYPERCHAT_EMAIL_{c}_RESEND_API_KEY=") for line in lines))
check = subprocess.run(RUN + ["-c", VALIDATE, ",".join(configured)], cwd=APP, input=candidate, capture_output=True)
if check.returncode != 0 or not check.stdout.startswith(b"ok"):
    for line in check.stderr.decode(errors="replace").splitlines():
        if "Refusing to start" in line or line.startswith("STOP"):
            print(line.strip())
    sys.exit("STOP: the deployed code refused the candidate; api.env unchanged")
st = os.stat(ENV)
backup = ("/root/api.env.before-email-keys." + datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S.%fZ")
          + f".{os.getpid()}")   # unique even for two runs in the same second
dirfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)   # link, rename and cleanup all act on this directory
fd = os.open(folder, os.O_TMPFILE | os.O_WRONLY, 0o600)   # unnamed: nothing to find if the process dies here
named = False
try:
    with open(fd, "wb", closefd=False) as f:
        f.write(candidate); f.flush(); os.fsync(f.fileno())
    os.fchown(fd, st.st_uid, st.st_gid); os.fchmod(fd, st.st_mode & 0o777)
    with open(ENV, "rb") as src, os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dst:
        dst.write(src.read()); dst.flush(); os.fsync(dst.fileno())
    # linkat(AT_SYMLINK_FOLLOW): a directory fd makes CPython use linkat, which follows the /proc link to the file
    os.link(f"/proc/self/fd/{fd}", CAND, dst_dir_fd=dirfd, follow_symlinks=True)   # named only from here ...
    named = True
    os.replace(CAND, os.path.basename(ENV), src_dir_fd=dirfd, dst_dir_fd=dirfd)     # ... to here
    named = False
    os.fsync(dirfd)
finally:
    os.close(fd)
    if named:
        try:
            os.unlink(CAND, dir_fd=dirfd)
        except FileNotFoundError:
            pass
    os.close(dirfd)
print("validated:", check.stdout.decode().strip())
print("set:", " ".join(sorted(names)))
print("rollback copy:", backup)
PY
  printf '%s' "$keys" | python3 -c "$MERGE"; rc=$?
  keys=""; exit $rc
)
```

**3. Install the keys (the approved change).** Run it once and paste each key at its hidden prompt.
- **On success:** it prints `validated: ok [...]`, the variable names it set and the rollback copy's path.
- **On a `STOP`:** `api.env` is unchanged.
```bash
install_email_keys          # all five categories
```
**After an interrupted run** (a lost SSH session, a killed process or a crash):
1. Run `email_keys_recover`. It lists any candidate file: type, owner, mode, size, time and name, never the content.
2. Run step 4's checks to see whether `api.env` already holds the new content. The rename either happened completely or not at all.
3. Then run `email_keys_recover --delete`, and run step 3 again only if the keys are not installed.

**4. Check the result (read-only).** Expect:
- the same owner and mode as before;
- `disabled`, and an empty or absent `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`;
- `15`: five categories × FROM, VERIFIED_DOMAIN and KEY;
- no `.api.env.email-keys-candidate.*` file;
- one new rollback copy.
```bash
stat -c '%U:%G %a' /etc/hyperchat/api.env
grep -E '^HYPERCHAT_EMAIL_(BACKEND|ACTIVE_CATEGORIES)=' /etc/hyperchat/api.env
grep -cE '^HYPERCHAT_EMAIL_(ONBOARDING|AUTHENTICATION|NOTIFICATIONS|PAYMENTS|SUPPORT)_(FROM|VERIFIED_DOMAIN|RESEND_API_KEY)=' /etc/hyperchat/api.env
email_keys_recover; ls -l /root/api.env.before-email-keys.*
```

**5. Stop here.**
- Don't restart the API, and don't change `HYPERCHAT_EMAIL_BACKEND` or `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`. Each is a separate approval.
- The running API is unaffected until a restart. A restart with email still `disabled` sends nothing.

**6. The rollback copy.**
- **Keep it** until the owner accepts the installation, or decides the first activation.
- **Then remove it:** `rm /root/api.env.before-email-keys.<time>`. See the freed-blocks limitation above.
- **Rolling the installation back** (before any restart):
  1. `install -m <same mode> -o root -g hyperchat /root/api.env.before-email-keys.<time> /etc/hyperchat/api.env`
  2. Check with step 4.

## Later: activation (separate approvals, one category at a time)
1. Set `HYPERCHAT_EMAIL_BACKEND=resend` and `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES=onboarding`, then restart `hyperchat-api`.
2. The startup log line `authentication` shows `email_categories: {"onboarding": "onboarding.quicktestspace.site"}`, with names and domains only.
3. Send one approved invitation to an owner-controlled inbox, then confirm delivery and the `email sent` log. That log carries the category, template, status and provider message id; it has no recipient identifier. Nothing is retried automatically, and there is no delivery-record table or domain-check CLI yet: check domains in the Resend dashboard.
4. **Watch for `invite_withdraw_failed`.** It means an invitation whose delivery was unconfirmed could not be revoked. Find and revoke it by following *Operator: unrevoked invitations* in `docs/email-architecture-proposal.md`.
5. Only then, and with approval, add `authentication`.

**Rollback:**
- Empty `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`, or set the backend to `disabled`, and restart.
- To remove the keys, restore the dated rollback copy `/root/api.env.before-email-keys.<time>` (step 6), keeping `api.env`'s owner and mode.
- If a key may have leaked, revoke it in Resend.

## Rotating or revoking a key
1. Create the new domain-restricted key in Resend.
2. Run steps 1–4 for that one category, for example `install_email_keys PAYMENTS`. The function replaces only that category's three variables, checks every configured key against the others, and writes a new dated rollback copy.
3. Restart (approved).
4. Revoke the old key in Resend.
