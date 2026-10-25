# Installing the five DEV email keys (operator runbook)

**Status: instructions only. Nothing here has been run.** Installing the keys, changing `api.env`, restarting the API and activating any category each need **separate owner approval**. These steps are for DEV (`/etc/hyperchat/api.env`) only. Production has no approved sending domains: the API refuses to start with an active category in production.

## Rules
- **A key value never appears in a command line, shell history, `ps`, a log, terminal output, chat, Git or a PR.**
  - Keys are pasted only at a hidden `read -rs` prompt.
  - They are written only to root-only files.
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
**1. Back up `api.env` (root-only).**
```bash
install -m 0600 -o root -g root /etc/hyperchat/api.env /root/api.env.before-email-keys
```

**2. Paste the five keys at hidden prompts into a root-only staging file.** `read` and `printf` are shell built-ins, so the value never appears in `ps` or in history.
```bash
umask 077; : > /root/email-keys.tmp
for c in ONBOARDING AUTHENTICATION NOTIFICATIONS PAYMENTS SUPPORT; do
  read -rs -p "$c key (hidden): " k; echo
  printf 'HYPERCHAT_EMAIL_%s_RESEND_API_KEY=%s\n' "$c" "$k" >> /root/email-keys.tmp; unset k
done
```

**3. Check the staging file without printing a value.** Expect `5 5 5`: five lines, five distinct keys, five `re_` keys.
```bash
echo "$(wc -l < /root/email-keys.tmp) $(cut -d= -f2- /root/email-keys.tmp | sort -u | wc -l) $(grep -c '_RESEND_API_KEY=re_' /root/email-keys.tmp)"
```

**4. Merge into `api.env`.** This sets the senders, verified domains and keys, and replaces earlier values of the same variables. It keeps `root:hyperchat 0640` and writes atomically. Only the variable names are printed.
```bash
python3 - /etc/hyperchat/api.env /root/email-keys.tmp <<'PY'
import os, sys
env, staged = sys.argv[1:]
senders = {"ONBOARDING": ("HyperChat", "onboarding"), "AUTHENTICATION": ("HyperChat Security", "auth"),
           "NOTIFICATIONS": ("HyperChat", "notifications"), "PAYMENTS": ("HyperChat Payments", "payments"),
           "SUPPORT": ("HyperChat Support", "support")}
fixed = []
for c, (name, label) in senders.items():
    fixed += [f"HYPERCHAT_EMAIL_{c}_FROM={name} <no-reply@{label}.quicktestspace.site>",
              f"HYPERCHAT_EMAIL_{c}_VERIFIED_DOMAIN={label}.quicktestspace.site"]
new = fixed + open(staged).read().splitlines()
names = {line.split("=", 1)[0] for line in new}
kept = [line for line in open(env).read().splitlines() if line.split("=", 1)[0] not in names]
st = os.stat(env)
fd = os.open(env + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(kept + new) + "\n")
os.chown(env + ".new", st.st_uid, st.st_gid); os.chmod(env + ".new", 0o640)
os.replace(env + ".new", env)
print("set:", " ".join(sorted(names)))
PY
shred -u /root/email-keys.tmp
stat -c '%U:%G %a' /etc/hyperchat/api.env   # expect root:hyperchat 640
grep -E '^HYPERCHAT_EMAIL_(BACKEND|ACTIVE_CATEGORIES)=' /etc/hyperchat/api.env   # expect disabled and empty
```

**5. Validate with the deployed code, without activating anything.** This needs a release that contains the P0 email categories. It runs the startup check as if all five categories were active and prints `ok`, or a refusal that names variables only (`hide_input_in_errors`).
```bash
cd /opt/hyperchat/current/backend && sudo -u hyperchat env PYTHONDONTWRITEBYTECODE=1 ../venv/bin/python -c "
from app.core.config import Settings
s = Settings(_env_file='/etc/hyperchat/api.env', email_active_categories='onboarding,authentication,notifications,payments,support')
print('ok', sorted(s.email_categories))"
```

**6. Stop here.** Don't restart the API and don't change `HYPERCHAT_EMAIL_BACKEND` or `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`. Each is a separate approval.

## Later: activation (separate approvals, one category at a time)
1. Set `HYPERCHAT_EMAIL_BACKEND=resend` and `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES=onboarding`, then restart `hyperchat-api`.
2. The startup log line `authentication` shows `email_categories: {"onboarding": "onboarding.quicktestspace.site"}`, with names and domains only.
3. Send one approved invitation to an owner-controlled inbox, then confirm delivery and the `email sent` log. That log carries the category, template, status and provider message id; it has no recipient identifier. Nothing is retried automatically, and there is no delivery-record table or domain-check CLI yet: check domains in the Resend dashboard.
4. **Watch for `invite_withdraw_failed`.** It means an invitation whose delivery was unconfirmed could not be revoked. Find and revoke it by following *Operator: unrevoked invitations* in `docs/email-architecture-proposal.md`.
5. Only then, and with approval, add `authentication`.

**Rollback:**
- Empty `HYPERCHAT_EMAIL_ACTIVE_CATEGORIES`, or set the backend to `disabled`, and restart.
- To remove the keys, restore `/root/api.env.before-email-keys`.
- If a key may have leaked, revoke it in Resend.

## Rotating or revoking a key
1. Create the new domain-restricted key in Resend.
2. Repeat steps 2–5 for that one category: the loop takes a list of category names.
3. Restart (approved).
4. Revoke the old key in Resend.
