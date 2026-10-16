#!/usr/bin/env bash
# Read-only audit of one HyperChat environment's process and file isolation. Run as root on the server; it
# changes nothing. Prints one line per check (OK / FAIL); exit 0 when every check passed, 1 otherwise.
#
#   bash deploy/check_isolation.sh dev|prod
#
# Checks: releases and env files are root-owned and not writable by the service user (so a compromised API
# cannot change code that later runs elsewhere, e.g. a verifier under another user); `current` points into
# releases/; installed units run as the service user; the service account cannot log in; the cluster's
# pg_hba line covers every HyperChat login role and pg_ident maps the service user to exactly its roles.
#
# HYPERCHAT_CHECK_PREFIX / _OWNER / _GROUP exist only for tests/test_deploy_isolation.py (a fake root owned by
# the test user); on a server they stay unset.
set -uo pipefail

case "${1:-}" in
  dev)  SVC=hyperchat;      OTHER=hyperchat-prod; APP=/opt/hyperchat;      ETC=/etc/hyperchat
        UNITS="hyperchat-api hyperchat-worker hyperchat-cleanup"; PGCONF=/etc/postgresql/15/restoretest ;;
  prod) SVC=hyperchat-prod; OTHER=hyperchat;      APP=/opt/hyperchat-prod; ETC=/etc/hyperchat-prod
        UNITS="hyperchat-prod-api hyperchat-prod-cleanup";        PGCONF=/etc/postgresql/17/hyperchat ;;
  *) echo "usage: $0 dev|prod" >&2; exit 2 ;;
esac
P="${HYPERCHAT_CHECK_PREFIX:-}"
OWNER="${HYPERCHAT_CHECK_OWNER:-root}"
GROUP="${HYPERCHAT_CHECK_GROUP:-$SVC}"
# The service user's database roles (pg_ident.conf / pg_ident.production.conf) and every login role (roles.sql).
SVC_ROLES="hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_worker"
LOGIN_ROLES="hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_migrator hyperchat_readonly hyperchat_worker"
FAILED=0

ok() { echo "OK    $1"; }
fail() { echo "FAIL  $1"; FAILED=1; }
check() { if [ -z "$2" ]; then ok "$1"; else fail "$1: $(printf '%s' "$2" | head -5 | tr '\n' ' ')"; fi; }

# 1. Code: everything under the application directory is owned by $OWNER, never group/other-writable, and
#    gives "other" nothing (releases are root:<service> g+rX,g-w,o-rwx). Symlinks are judged by their target.
if [ -d "$P$APP" ]; then
  check "release tree $APP owned by $OWNER, not writable by group/other, no access for other" \
    "$(find "$P$APP" -xdev ! -type l \( ! -user "$OWNER" -o -perm /0022 -o -perm /0007 \) -print 2>&1)"
  CUR="$(readlink -f "$P$APP/current" 2>/dev/null || true)"
  case "$CUR" in
    "$P$APP"/releases/?*) ok "current -> ${CUR#"$P"}" ;;
    *) fail "current does not point into $APP/releases/: ${CUR:-missing}" ;;
  esac
else
  fail "$APP missing"
fi

# 2. Secrets: the env directory and file are root-owned; the file is <owner>:<service> 0640 or 0600.
if [ -f "$P$ETC/api.env" ]; then
  check "env directory $ETC owned by $OWNER, not writable by group/other" \
    "$(find "$P$ETC" -maxdepth 0 \( ! -user "$OWNER" -o -perm /0022 \) -print)"
  MODE="$(stat -c '%U:%G %a' "$P$ETC/api.env")"
  case "$MODE" in
    "$OWNER:$GROUP 640" | "$OWNER:$GROUP 600" | "$OWNER:$OWNER 600") ok "api.env is $MODE" ;;
    *) fail "api.env is $MODE (want $OWNER:$GROUP 640)" ;;
  esac
else
  fail "$ETC/api.env missing"
fi

# 3. Units: root-owned, not group/other-writable, running as the service user (never root, never the other env's).
for U in $UNITS; do
  F="$P/etc/systemd/system/$U.service"
  [ -f "$F" ] || { ok "$U.service not installed (skipped)"; continue; }
  check "$U.service owned by $OWNER, not writable by group/other" \
    "$(find "$F" \( ! -user "$OWNER" -o -perm /0022 \) -print)"
  USERLINE="$(grep -E '^User=' "$F" | tail -1)"
  [ "$USERLINE" = "User=$SVC" ] && ok "$U runs as $SVC" || fail "$U runs as '${USERLINE#User=}' (want $SVC)"
done

# 4. The service account: no login shell, no sudo/adm membership, not in the other environment's group.
if [ -z "$P" ]; then
  SHELL_="$(getent passwd "$SVC" | cut -d: -f7)"
  case "$SHELL_" in
    /usr/sbin/nologin | /sbin/nologin | /bin/false | /usr/bin/false) ok "$SVC has no login shell" ;;
    *) fail "$SVC login shell is '${SHELL_:-no such user}'" ;;
  esac
  BAD="$(id -nG "$SVC" 2>/dev/null | tr ' ' '\n' | grep -Ex "sudo|adm|wheel|root|$OTHER" || true)"
  check "$SVC is not in sudo/adm/wheel/root/$OTHER" "$BAD"
fi

# 5. PostgreSQL: the HyperChat pg_hba line lists every login role; pg_ident maps $SVC to exactly its roles and
#    never maps the other environment's user.
HBA="$P$PGCONF/pg_hba.conf"; IDENT="$P$PGCONF/pg_ident.conf"
if [ -f "$HBA" ] && [ -f "$IDENT" ]; then
  LISTED="$(grep -E '^[^#]*map=hyperchat([[:space:]]|$)' "$HBA" | awk '{print $3}' | tr ',' '\n' | sort -u)"
  MISSING="$(comm -23 <(printf '%s\n' $LOGIN_ROLES | sort) <(printf '%s\n' "$LISTED"))"
  check "pg_hba HyperChat line covers every login role" "${MISSING:+missing: $MISSING}"
  MAPPED="$(awk -v u="$SVC" '$1 == "hyperchat" && $2 == u {print $3}' "$IDENT" | sort -u | tr '\n' ' ')"
  WANT="$(printf '%s\n' $SVC_ROLES | sort | tr '\n' ' ')"
  [ "$MAPPED" = "$WANT" ] && ok "pg_ident maps $SVC to: $MAPPED" || fail "pg_ident maps $SVC to: '$MAPPED' (want '$WANT')"
  check "pg_ident never maps $OTHER" "$(awk -v u="$OTHER" '$1 == "hyperchat" && $2 == u' "$IDENT")"
else
  fail "$PGCONF/pg_hba.conf or pg_ident.conf missing"
fi

exit "$FAILED"
