#!/usr/bin/env bash
# Read-only audit of one HyperChat environment's process and file isolation. Run as root on the server; it
# changes nothing. Prints one line per check (OK / FAIL); exit 0 when every check passed, 1 otherwise.
#
#   bash deploy/check_isolation.sh dev|prod
#
# Checks: the code the service runs (releases, `current`, and wherever their symlinks lead) and the env file are
# root-owned and not writable by the service user, so a compromised API cannot change code that later runs
# elsewhere (e.g. a verifier under another user); installed units run as the service user; the service account
# cannot log in; the cluster's pg_hba rule covers every HyperChat login role and takes effect (no earlier rule
# shadows it); pg_ident maps the service user to exactly its roles.
#
# HYPERCHAT_CHECK_PREFIX / _OWNER / _GROUP / _MOUNTINFO exist only for tests/test_deploy_isolation.py (a fake
# root owned by the test user; there, symlinks must be relative: an absolute one leaves the fake root and fails).
# On a server they stay unset. Needs namei (util-linux).
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
MOUNTINFO="${HYPERCHAT_CHECK_MOUNTINFO:-/proc/self/mountinfo}"
# The service user's database roles (pg_ident.conf / pg_ident.production.conf) and every login role (roles.sql).
SVC_ROLES="hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_worker"
LOGIN_ROLES="hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_migrator hyperchat_readonly hyperchat_worker"
FAILED=0


ok() { echo "OK    $1"; }
fail() { echo "FAIL  $1"; FAILED=1; }
# check LABEL COMMAND [ARGS...]: OK only when the command exits 0 AND prints nothing, on stdout or stderr. A helper
# that breaks (a bash error, a missing tool, a traversal error) therefore fails the check; it can never pass it.
check() {
  local label="$1" out rc; shift
  out="$("$@" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && [ -z "$out" ]; then ok "$label"; return; fi
  fail "$label: $(printf '%s' "${out:-no output}" | head -5 | tr '\n' ' ')$([ "$rc" -eq 0 ] || echo " [exit $rc]")"
}
collect() {  # a helper's problems as lines, with a line for a non-zero exit, for lists built over a loop
  local out rc; out="$("$@" 2>&1)"; rc=$?
  [ -z "$out" ] || printf '%s\n' "$out"
  [ "$rc" -eq 0 ] || printf '%s exited %s\n' "$1" "$rc"
}
lines() { printf '%s' "$1" | sed '/^$/d'; }

walk() {  # namei -l of one path: from / on a server, from the fake root in tests
  if [ -z "$P" ]; then namei -l "$1"; else (cd "$P" && namei -l "${1#"$P"/}"); fi
}
unsafe_hops() {  # each directory, symlink hop and final target on the way to $1 that is missing, not $OWNER's, or
                 # group/other-writable (sticky directories such as /tmp included)
  walk "$1" 2>&1 | awk -v owner="$OWNER" -v path="${1#"$P"}" '
    NR == 1 && /^f: / { next }
    $1 !~ /^[-dlcbps][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT][-rwxsStT]$/ {
      print path ": " $0; next }
    substr($1, 1, 1) == "l" { next }  # a link cannot be rewritten in place: its directory and its target are checked
    $2 != owner || substr($1, 6, 1) == "w" || substr($1, 9, 1) == "w" { print path ": " $1 " " $2 " " $NF }'
}
scan_tree() {  # files under $1 (links not followed) that are not $OWNER's or are group/other-writable; with
               # $2 = strict also any access for "other". find's errors are output too.
  local extra=(); [ "${2:-}" = strict ] && extra=(-o -perm /0007)
  find "$1" ! -type l \( ! -user "$OWNER" -o -perm /0022 "${extra[@]}" \) -print
}
mounts_under() {  # mount points strictly inside $1
  awk -v top="$1/" '{ m = $5; gsub(/\\040/, " ", m) } index(m, top) == 1 { print m }' "$MOUNTINFO"
}

# 1. Code: what the service executes is the application directory plus everything its symlinks lead to.
#    - The application directory is owned by $OWNER, never group/other-writable, and gives "other" nothing
#      (releases are root:<service> g+rX,g-w,o-rwx). find crosses mount points, so mounted content is judged like
#      the rest, and a mount point inside is refused outright.
#    - Every directory on the way to it, and every hop of every symlink (the intended `current`, the venv's python
#      and lib64) up to its final target, must be $OWNER's and not group/other-writable (namei).
#    - A symlink to a directory outside the application directory makes that directory one more root, scanned the
#      same way (recursively, mounts refused, its own symlinks followed in turn); "other" may read it (e.g.
#      /usr/lib). Each directory is scanned once, so directory cycles terminate; a link that cannot be resolved
#      (a loop, a missing target) or that points at a directory containing itself fails, and more than 50 linked
#      roots fail. Every find, namei and readlink error is reported, so a traversal error never passes.
if [ -d "$P$APP" ]; then
  check "release tree $APP owned by $OWNER, not writable by group/other, no access for other" \
    scan_tree "$P$APP" strict
  check "no writable directory on the way to $APP" unsafe_hops "$P$APP"
  ROOTS=("$P$APP"); SEEN=$'\n'"$P$APP"$'\n'; LINKS=""; LINKED=""; MOUNTS=""; i=0
  while [ "$i" -lt "${#ROOTS[@]}" ]; do
    R="${ROOTS[$i]}"; i=$((i + 1))
    if [ "$i" -gt 51 ]; then LINKED+="more than 50 symlinked directories"$'\n'; break; fi
    [ "$i" -eq 1 ] || LINKED+="$(collect scan_tree "$R")"$'\n'
    MOUNTS+="$(collect mounts_under "$R")"$'\n'
    while IFS= read -r -d '' L; do
      LINKS+="$(collect unsafe_hops "$L")"$'\n'
      if ! T="$(readlink -e "$L" 2>&1)"; then LINKS+="${L#"$P"}: cannot be resolved (loop or missing target)"$'\n'; continue; fi
      [ -d "$T" ] || continue
      case "$L/" in "$T"/*) LINKS+="${L#"$P"}: points at a directory that contains it (cycle)"$'\n'; continue ;; esac
      case "$T" in "$P$APP" | "$P$APP"/*) continue ;; esac  # inside the application directory: scanned already
      [ -z "$P" ] || case "$T" in "$P"/*) ;; *) continue ;; esac  # tests: left the fake root (unsafe_hops failed it)
      case "$SEEN" in *$'\n'"$T"$'\n'*) continue ;; esac
      SEEN+="$T"$'\n'; ROOTS+=("$T")
    done < <(find "$R" -type l -print0 2>/dev/null)  # find errors are already in scan_tree's output
  done
  check "every symlink under $APP resolves only through $OWNER-owned, non-writable paths" lines "$LINKS"
  check "every directory reached through a symlink is $OWNER's and not writable by group/other, recursively" \
    lines "$LINKED"
  check "no mount point inside $APP or a directory it links to" lines "$MOUNTS"
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
    find "$P$ETC" -maxdepth 0 \( ! -user "$OWNER" -o -perm /0022 \) -print
  check "no writable directory on the way to $ETC/api.env" unsafe_hops "$P$ETC/api.env"
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
  check "$U.service owned by $OWNER, not writable by group/other" find "$F" \( ! -user "$OWNER" -o -perm /0022 \) -print
  USERLINE="$(grep -E '^User=' "$F" | tail -1)"
  [ "$USERLINE" = "User=$SVC" ] && ok "$U runs as $SVC" || fail "$U runs as '${USERLINE#User=}' (want $SVC)"
done

# 4. The service account: no login shell, no sudo/adm membership, not in the other environment's group.
privileged_groups() {
  local groups; groups="$(id -nG "$SVC")" || return 1
  printf '%s\n' $groups | grep -Ex "sudo|adm|wheel|root|$OTHER"
  return 0
}
if [ -z "$P" ]; then
  SHELL_="$(getent passwd "$SVC" | cut -d: -f7)"
  case "$SHELL_" in
    /usr/sbin/nologin | /sbin/nologin | /bin/false | /usr/bin/false) ok "$SVC has no login shell" ;;
    *) fail "$SVC login shell is '${SHELL_:-no such user}'" ;;
  esac
  check "$SVC is not in sudo/adm/wheel/root/$OTHER" privileged_groups
fi

# 5. PostgreSQL. The HyperChat pg_hba rule lists every login role AND takes effect: PostgreSQL uses the FIRST
#    matching rule, so no earlier "local" rule may match a HyperChat role (user column all, +group, @file or a
#    HyperChat role), and there may be no include directive or continuation line this audit cannot order.
#    pg_ident maps $SVC to exactly its roles and never maps the other environment's user.
hba_missing_roles() {
  local listed
  listed="$(grep -E '^[^#]*map=hyperchat([[:space:]]|$)' "$HBA" | awk '{print $3}' | tr ',' '\n' | sort -u)"
  comm -23 <(printf '%s\n' $LOGIN_ROLES | sort) <(printf '%s\n' "$listed") | sed 's/^/missing: /'
}
hba_order() {
  awk -v roles="$LOGIN_ROLES" '
    function competes(users,   list, k, m, u) {
      m = split(users, list, ",")
      for (k = 1; k <= m; k++) { u = list[k]; gsub(/"/, "", u); if (u == "all" || u ~ /^[+@]/ || u in want) return 1 }
      return 0
    }
    function stop(msg) { print msg; done = 1; exit }
    BEGIN { n = split(roles, r, " "); for (i = 1; i <= n; i++) want[r[i]] = 1 }
    { t = $0; sub(/#.*/, "", t) }
    t ~ /^[[:space:]]*$/ { next }
    { c = t; sub(/[[:space:]]+$/, "", c) }
    substr(c, length(c)) == "\\" { stop("line " NR " is a continuation line") }
    { split(t, f) }
    f[1] ~ /^include/ { stop("line " NR " is an include directive") }
    t ~ /map=hyperchat([[:space:]]|$)/ {
      if (hc) stop("more than one HyperChat rule (lines " hc " and " NR ")")
      if (f[1] != "local") stop("the HyperChat rule (line " NR ") is not a local rule")
      hc = NR; next
    }
    !hc && !shadow && f[1] == "local" && competes(f[3]) { shadow = "line " NR " (" f[1] " " f[2] " " f[3] ") comes first" }
    END {
      if (done) exit
      if (!hc) print "no HyperChat rule"
      else if (shadow) print "the HyperChat rule is shadowed: " shadow
    }' "$HBA"
}
HBA="$P$PGCONF/pg_hba.conf"; IDENT="$P$PGCONF/pg_ident.conf"
if [ -f "$HBA" ] && [ -f "$IDENT" ]; then
  check "pg_hba HyperChat rule covers every login role" hba_missing_roles
  check "pg_hba HyperChat rule takes effect (no earlier local rule matches its roles)" hba_order
  MAPPED="$(awk -v u="$SVC" '$1 == "hyperchat" && $2 == u {print $3}' "$IDENT" | sort -u | tr '\n' ' ')"
  WANT="$(printf '%s\n' $SVC_ROLES | sort | tr '\n' ' ')"
  [ "$MAPPED" = "$WANT" ] && ok "pg_ident maps $SVC to: $MAPPED" || fail "pg_ident maps $SVC to: '$MAPPED' (want '$WANT')"
  check "pg_ident never maps $OTHER" awk -v u="$OTHER" '$1 == "hyperchat" && $2 == u' "$IDENT"
else
  fail "$PGCONF/pg_hba.conf or pg_ident.conf missing"
fi

exit "$FAILED"
