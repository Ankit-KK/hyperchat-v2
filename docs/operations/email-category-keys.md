# Installing the five DEV email keys (operator runbook)

**Status: instructions only. Nothing here has been run.** Installing the keys, changing `api.env`, restarting the API and activating any category each need **separate owner approval**. These steps are for DEV (`/etc/hyperchat/api.env`) only. Production has no approved sending domains: the API refuses to start with an active category in production.

## Rules
- **A key value never appears in a command line, shell history, `ps`, a log, terminal output, chat, Git or a PR.**
  - Keys are pasted only at a hidden `read -rs` prompt.
  - They are never written to a staging file; the only file that ever holds them is `api.env`.
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
**Properties of the procedure below:**
- **No plaintext copy of a key on disk.** Keys go from hidden `read -rs` prompts into a variable of a subshell, then through a pipe into Python. The only file that ever holds them is `api.env` itself.
- **Cleanup on every exit path.** The subshell's variables die with it, on success, on a STOP or on Ctrl-C. The candidate file is written next to `api.env` and removed by `finally` if anything fails before the atomic rename.
- **Validated before `api.env` changes.**
  - The deployed code checks the complete candidate as the service user, in memory (stdin), with every configured category treated as active.
  - On any refusal, `api.env` is left byte-for-byte unchanged and nothing is backed up or written.
- **Never activates.**
  - The function refuses to run unless `HYPERCHAT_EMAIL_BACKEND` is `disabled` (or unset) and `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES` is empty.
  - It writes only `HYPERCHAT_EMAIL_<CATEGORY>_FROM`, `_VERIFIED_DOMAIN` and `_RESEND_API_KEY` for the categories given.
  - It restarts nothing; the running API keeps its configuration until an approved restart.
- **`api.env` keeps its owner and mode** (DEV: `root:hyperchat 0600`, checked read-only on 2026-10-25). The new content is flushed to disk before the rename.
- **One dated, root-only rollback copy** (`/root/api.env.before-email-keys.<UTC time>`, 0600) is written only after validation passed and just before the rename. It never overwrites an earlier copy, and it holds the *previous* content, not the new keys.

**1. Preconditions (read-only).** Expect: the release has the categories, email is disabled, and no category is active.
```bash
cat /opt/hyperchat/current/backend/RELEASE   # a release with the email categories (a9b9486efd86 or later)
grep -E '^HYPERCHAT_EMAIL_(BACKEND|ACTIVE_CATEGORIES)=' /etc/hyperchat/api.env   # disabled (or absent) and empty (or absent)
stat -c '%U:%G %a' /etc/hyperchat/api.env
ls -a /etc/hyperchat   # no leftover .api.env.* candidate files
```

**2. Define the function (changes nothing).** Paste this block as is. It contains no secret.
```bash
install_email_keys() (   # a subshell: keys, variables and any failure stay inside it
  set -u; umask 077
  cats="${*:-ONBOARDING AUTHENTICATION NOTIFICATIONS PAYMENTS SUPPORT}"
  keys=""
  for c in $cats; do
    case "$c" in ONBOARDING|AUTHENTICATION|NOTIFICATIONS|PAYMENTS|SUPPORT) ;; *) echo "STOP: unknown category $c"; exit 1 ;; esac
    read -rs -p "$c key (hidden): " k || { echo; echo "STOP: no input"; exit 1; }
    echo; keys="$keys$c=$k"$'\n'; k=""
  done
  read -r -d '' MERGE <<'PY'
import datetime, os, subprocess, sys, tempfile
ENV = "/etc/hyperchat/api.env"
APP = "/opt/hyperchat/current/backend"
RUN = ["sudo", "-u", "hyperchat", "env", "PYTHONDONTWRITEBYTECODE=1", "../venv/bin/python"]
CATS = {"ONBOARDING": ("HyperChat", "onboarding"), "AUTHENTICATION": ("HyperChat Security", "auth"),
        "NOTIFICATIONS": ("HyperChat", "notifications"), "PAYMENTS": ("HyperChat Payments", "payments"),
        "SUPPORT": ("HyperChat Support", "support")}
VALIDATE = ("import sys; from app.core.config import Settings; "
            "s = Settings(_env_file='/dev/stdin', email_active_categories=sys.argv[1]); print('ok', sorted(s.email_categories))")
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
candidate = "\n".join(lines) + "\n"
configured = sorted(c.lower() for c in CATS if any(line.startswith(f"HYPERCHAT_EMAIL_{c}_RESEND_API_KEY=") for line in lines))
check = subprocess.run(RUN + ["-c", VALIDATE, ",".join(configured)], cwd=APP, input=candidate.encode(), capture_output=True)
if check.returncode != 0 or not check.stdout.startswith(b"ok"):
    for line in check.stderr.decode(errors="replace").splitlines():
        if "Refusing to start" in line:
            print(line.strip())
    sys.exit("STOP: the deployed code refused the candidate; api.env unchanged")
st, folder = os.stat(ENV), os.path.dirname(ENV)
backup = "/root/api.env.before-email-keys." + datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
fd, tmp = tempfile.mkstemp(dir=folder, prefix=".api.env.")
try:
    with open(ENV, "rb") as src, os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as dst:
        dst.write(src.read()); dst.flush(); os.fsync(dst.fileno())
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(candidate); f.flush(); os.fsync(f.fileno())
    os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, ENV)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
dfd = os.open(folder, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
print("validated:", check.stdout.decode().strip())
print("set:", " ".join(sorted(names)))
print("rollback copy:", backup)
PY
  printf '%s' "$keys" | python3 -c "$MERGE"; rc=$?
  keys=""; exit $rc
)
```

**3. Install the keys (the approved change).** Run it once and paste each key at its hidden prompt. It prints `validated: ok [...]`, the variable names it set and the rollback copy's path, or a `STOP` line. After a STOP, nothing has changed.
```bash
install_email_keys          # all five categories
```

**4. Check the result (read-only).** Expect:
- the same owner and mode as before;
- `disabled` and an empty or absent `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`;
- `15`: five categories × FROM, VERIFIED_DOMAIN and KEY;
- no leftover `.api.env.*` candidate file;
- exactly one new rollback copy.
```bash
stat -c '%U:%G %a' /etc/hyperchat/api.env
grep -E '^HYPERCHAT_EMAIL_(BACKEND|ACTIVE_CATEGORIES)=' /etc/hyperchat/api.env
grep -cE '^HYPERCHAT_EMAIL_(ONBOARDING|AUTHENTICATION|NOTIFICATIONS|PAYMENTS|SUPPORT)_(FROM|VERIFIED_DOMAIN|RESEND_API_KEY)=' /etc/hyperchat/api.env
ls -a /etc/hyperchat; ls -l /root/api.env.before-email-keys.*
```

**5. Stop here.**
- Don't restart the API, and don't change `HYPERCHAT_EMAIL_BACKEND` or `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`. Each is a separate approval.
- The running API is unaffected until a restart. A restart with email still `disabled` sends nothing.

**6. The rollback copy.**
- **Keep it** until the owner accepts the installation, or decides the first activation.
- **Then remove it:** `rm /root/api.env.before-email-keys.<time>`. It holds the previous `api.env`, with its other secrets, so it should not live longer than needed.
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
