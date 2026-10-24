: permfix-prod
# HyperChat PRODUCTION permission repair: the 5 recorded __pycache__ directories of the active release only.
# Each path: root:root 0755 directory -> root:hyperchat-prod 0750. Explicit paths only: no recursion, no symlink
# followed, no file outside the manifest touched, no service restarted or reloaded, no database touched. Fails
# closed at the first surprise. DEV (/opt/hyperchat) is never touched; it is checked unchanged.
A=/opt/hyperchat-prod; SVC=hyperchat-prod; REL=940137070657; PORT=8001
AUDIT=/root/hyperchat-c2fe883/deploy/check_isolation.sh
PF=/root/prod-permfix
MANIFEST_SHA=c61c0dc479119ec8352e26925c079341c61637fe2964e76a1d4a04f5953fbb1c
manifest() { cat <<'EOF'
/opt/hyperchat-prod/releases/940137070657/backend/app/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/admin/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/auth/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/core/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/security/__pycache__
EOF
}
violations() {  # the audit's strict predicate, sorted; non-zero when find or sort fails
  local v s
  v="$(find "$A" ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -print)" || return 1
  [ -n "$v" ] || return 0
  s="$(printf '%s\n' "$v" | LC_ALL=C sort)" || return 1
  printf '%s\n' "$s"
}
is_plain_dir() { [ -d "$1" ] && [ ! -L "$1" ] && [ "$(readlink -f "$1")" = "$1" ]; }
svc_state() {  # fails unless systemctl answers, the production API is active and the cleanup timer is active
  local x; x="$(systemctl show hyperchat-prod-api hyperchat-prod-cleanup.timer -p MainPID -p NRestarts -p ActiveState)" || return 1
  [ "$(printf '%s\n' "$x" | grep -cx 'ActiveState=active')" = 2 ] || return 1
  printf '%s' "$x" | tr '\n' ' '
}
svc_access() {  # the service user reaches and reads its active release
  local R; R="$(readlink -f "$A/current")"
  sudo -u "$SVC" test -x "$A" && sudo -u "$SVC" test -x "$A/releases" && sudo -u "$SVC" test -r "$R/backend/app/main.py" \
    && sudo -u "$SVC" test -x "$R/venv/bin/python"
}
health_ok() {  # health_ok PORT RELEASE: one /health read: status ok, database ok, that release
  curl -fsS --max-time 5 "http://127.0.0.1:$1/health" 2>/dev/null | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$2"
}
consumers() {  # every installed unit that runs code from $A, with its user; nginx and cron must name none
  local f
  for f in $(grep -lE "/opt/hyperchat-prod(/|$|[[:space:]])" /etc/systemd/system/*.service /etc/systemd/system/*.timer /lib/systemd/system/*.service 2>/dev/null); do
    echo "$f User=$(grep -E '^User=' "$f" | tail -1 | cut -d= -f2)"
  done
  grep -rlE "/opt/hyperchat-prod(/|;|[[:space:]]|$)" /etc/nginx /etc/cron* /var/spool/cron 2>/dev/null
  return 0
}
CONSUMERS="/etc/systemd/system/hyperchat-prod-api.service User=hyperchat-prod
/etc/systemd/system/hyperchat-prod-cleanup.service User=hyperchat-prod"
# What must not change at all (read-only): production's release symlink, tree top and env metadata; DEV's release
# symlink, tree top, health and services; both clusters' client-auth files, the unit files and the backup script.
# Fails (non-zero) when any command fails or returns nothing; DEV must be healthy and its services active.
snapshot() {
  local x
  x="$(readlink "$A/current")" && [ -n "$x" ] || return 1; echo "prod current -> $x"
  stat -c '%U:%G %a %i %Y %Z %n' "$A" "$A/releases" "$A/current" "$A/releases/$REL" /etc/hyperchat-prod/api.env || return 1
  x="$(readlink /opt/hyperchat/current)" && [ -n "$x" ] || return 1; echo "dev current -> $x"
  stat -c '%U:%G %a %i %Y %Z %n' /opt/hyperchat /opt/hyperchat/releases /opt/hyperchat/current || return 1
  health_ok 8000 229fe42381e2 || return 1; echo "dev health ok 229fe42381e2"
  x="$(systemctl show hyperchat-api hyperchat-worker -p MainPID -p NRestarts -p ActiveState)" || return 1
  [ "$(printf '%s\n' "$x" | grep -cx 'ActiveState=active')" = 2 ] || return 1
  echo "dev services $(printf '%s' "$x" | tr '\n' ' ')"
  sha256sum /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf \
    /etc/postgresql/17/hyperchat/pg_hba.conf /etc/postgresql/17/hyperchat/pg_ident.conf /etc/systemd/system/hyperchat*.service \
    /usr/local/sbin/hyperchat-pgbackup || return 1
}
audit_expected() {  # $1 = dev | prod
  local svc other app etc rel units u mode
  case "$1" in
    dev)  svc=hyperchat; other=hyperchat-prod; app=/opt/hyperchat; etc=/etc/hyperchat; rel=229fe42381e2
          units="hyperchat-api hyperchat-worker hyperchat-cleanup" ;;
    prod) svc=hyperchat-prod; other=hyperchat; app=/opt/hyperchat-prod; etc=/etc/hyperchat-prod; rel=940137070657
          units="hyperchat-prod-api hyperchat-prod-worker hyperchat-prod-cleanup" ;;
    *) return 1 ;;
  esac
  mode="$(stat -c '%U:%G %a' "$etc/api.env")"
  case "$mode" in "root:$svc 640" | "root:$svc 600" | "root:root 600") ;; *) mode="root:$svc 640" ;; esac
  printf '%s\n' "OK    release tree $app owned by root, not writable by group/other, no access for other" \
    "OK    no writable directory on the way to $app" \
    "OK    every symlink under $app resolves only through root-owned, non-writable paths" \
    "OK    every directory reached through a symlink is root's and not writable by group/other, recursively" \
    "OK    no mount point inside $app or a directory it links to" \
    "OK    current -> $app/releases/$rel" \
    "OK    env directory $etc owned by root, not writable by group/other" \
    "OK    no writable directory on the way to $etc/api.env" \
    "OK    api.env is $mode"
  for u in $units; do
    if [ -f "/etc/systemd/system/$u.service" ]; then
      printf '%s\n' "OK    $u.service owned by root, not writable by group/other" "OK    $u runs as $svc"
    else
      printf '%s\n' "OK    $u.service not installed (skipped)"
    fi
  done
  printf '%s\n' "OK    $svc has no login shell" "OK    $svc is not in sudo/adm/wheel/root/$other" \
    "FAIL  pg_hba HyperChat rule covers every login role: missing: hyperchat_auth missing: hyperchat_lifecycle missing: hyperchat_stepup" \
    "OK    pg_hba HyperChat rule takes effect (no earlier local rule matches its roles)" \
    "FAIL  pg_ident maps $svc to: 'hyperchat_app hyperchat_worker ' (want 'hyperchat_app hyperchat_auth hyperchat_lifecycle hyperchat_worker ')" \
    "OK    pg_ident never maps $other" \
    "OK    only $svc-stepup maps to hyperchat_stepup, and $svc-stepup to nothing else" \
    "OK    step-up verifier not installed (skipped)"
}
audit_exact() {  # audit_exact ENV TAG: the audit exits 1, writes no stderr and prints exactly audit_expected ENV
  local rc
  bash "$AUDIT" "$1" > "$PF/audit-$2-$1.txt" 2> "$PF/audit-$2-$1.err"; rc=$?
  [ "$rc" = 1 ] && [ ! -s "$PF/audit-$2-$1.err" ] || { echo "STOP: $1 audit exited $rc or wrote to stderr"; return 1; }
  audit_expected "$1" > "$PF/audit-$2-$1.expected" || { echo "STOP: cannot build the expected $1 lines"; return 1; }
  diff <(LC_ALL=C sort "$PF/audit-$2-$1.expected") <(LC_ALL=C sort "$PF/audit-$2-$1.txt") > "$PF/audit-$2-$1.diff" \
    || { echo "STOP: $1 audit differs from the expected lines:"; cat "$PF/audit-$2-$1.diff"; return 1; }
}

# 1. Before-state (read-only apart from the records): manifest intact, live state == manifest, every path a plain
#    root:root 0755 directory, the release tree around it as recorded, the running API holds group hyperchat-prod,
#    production and DEV healthy. Records everything under $PF (single-use: mkdir fails if it exists).
pf_pre() {
  local p g pid a
  [ "$(id -u)" = 0 ] || { echo "STOP: run as root"; return 1; }
  [ "$(manifest | sha256sum | cut -c1-64)" = "$MANIFEST_SHA" ] || { echo "STOP: embedded manifest altered"; return 1; }
  [ "$(manifest | wc -l)" = 5 ] && [ "$(manifest | grep -c "^$A/releases/$REL/backend/app\(/[a-z_]*\)\?/__pycache__\$")" = 5 ] \
    || { echo "STOP: the manifest is not 5 __pycache__ directories of release $REL"; return 1; }
  [ "$(readlink -f "$A/current")" = "$A/releases/$REL" ] || { echo "STOP: current is not release $REL"; return 1; }
  for p in "$A" "$A/releases" "$A/releases/$REL"; do
    [ "$(stat -c '%U:%G %a %F' "$p")" = "root:$SVC 750 directory" ] || { echo "STOP: $p is not root:$SVC 750"; return 1; }
  done
  [ "$(consumers)" = "$CONSUMERS" ] || { echo "STOP: unexpected consumers of $A:"; consumers; return 1; }
  mkdir -m 700 "$PF" || { echo "STOP: $PF exists"; return 1; }
  snapshot > "$PF/invariants-before.txt" 2> "$PF/invariants-before.err" && [ ! -s "$PF/invariants-before.err" ] \
    || { echo "STOP: cannot collect the invariants (a command failed, or DEV is not healthy):"; head -n 5 "$PF/invariants-before.err"; return 1; }
  violations > "$PF/violations-before.txt" 2> "$PF/violations-before.err" && [ ! -s "$PF/violations-before.err" ] \
    || { echo "STOP: the violations scan failed (find or sort)"; head -n 5 "$PF/violations-before.err"; return 1; }
  diff <(manifest) "$PF/violations-before.txt" > "$PF/drift.diff" \
    || { echo "STOP: live violations differ from the manifest (< manifest only / > live only):"; cat "$PF/drift.diff"; return 1; }
  while IFS= read -r p; do
    is_plain_dir "$p" || { echo "STOP: $p is not a plain directory (symlink on the way?)"; return 1; }
    [ "$(stat -c '%U:%G %a %F' "$p")" = "root:root 755 directory" ] || { echo "STOP: $p is $(stat -c '%U:%G %a %F' "$p")"; return 1; }
    [ "$(stat -c '%U:%G %a' "$(dirname "$p")")" = "root:$SVC 750" ] || { echo "STOP: the parent of $p is not root:$SVC 750"; return 1; }
    a="$(lsattr -d "$p")" && [ -n "$a" ] || { echo "STOP: cannot read the attributes of $p"; return 1; }
    [ "$(printf '%s' "$a" | cut -c5)" != i ] || { echo "STOP: $p is immutable"; return 1; }
  done < <(manifest)
  manifest | xargs -d '\n' stat -c '%U:%G %a %F %n' > "$PF/before.txt" && [ "$(grep -c '^root:root 755 directory /' "$PF/before.txt")" = 5 ] \
    || { echo "STOP: cannot record the before-state of the 5 paths"; return 1; }
  g="$(getent group "$SVC" | cut -d: -f3)"; [ -n "$g" ] && id -G "$SVC" | tr ' ' '\n' | grep -qx "$g" \
    || { echo "STOP: $SVC is not in group $SVC"; return 1; }
  pid="$(systemctl show -p MainPID --value hyperchat-prod-api)" && [ "$(printf '%s\n' $pid | grep -c '^[1-9][0-9]*$')" = 1 ] \
    || { echo "STOP: cannot read the production API main PID"; return 1; }
  grep -E '^Groups:' "/proc/$pid/status" | tr -s ' \t' '\n' | grep -qx "$g" \
    || { echo "STOP: running process $pid does not hold group $SVC ($g)"; return 1; }
  svc_access || { echo "STOP: $SVC cannot read its active release before the change"; return 1; }
  health_ok "$PORT" "$REL" || { echo "STOP: production API not healthy on $REL before the change"; return 1; }
  svc_state > "$PF/services-before.txt" || { echo "STOP: cannot read the production service state, or a unit is not active"; return 1; }
  echo "pre OK: 5 paths == manifest, all root:root 755 plain directories; $(cat "$PF/services-before.txt")"
}

# 2. The 5 __pycache__ directories, one by one (their 0640 root:root .pyc files are already compliant: untouched).
pf_pycache() {
  local p n=0
  while IFS= read -r p; do
    is_plain_dir "$p" && [ "$(stat -c '%U:%G %a' "$p")" = "root:root 755" ] || { echo "STOP: $p changed since pf_pre ($n done)"; return 1; }
    chgrp --no-dereference "$SVC" "$p" && chmod 0750 "$p" || { echo "STOP: change failed on $p ($n done)"; return 1; }
    n=$((n + 1))
  done < <(manifest)
  [ "$n" = 5 ] || { echo "STOP: $n of 5 changed"; return 1; }
  echo "pycache OK: 5 directories root:$SVC 750"
}

# 3. After: no violation left, exactly the 5 paths changed, service access, production health, same PID and restart
#    count, DEV and the shared configuration unchanged, and both audits print exactly the expected lines (the two
#    legacy pg_hba / pg_ident FAILs only).
pf_verify() {
  violations > "$PF/violations-after.txt" 2> "$PF/violations-after.err" && [ ! -s "$PF/violations-after.err" ] \
    || { echo "STOP: the violations scan failed after the change (find or sort)"; head -n 5 "$PF/violations-after.err"; return 1; }
  [ ! -s "$PF/violations-after.txt" ] || { echo "STOP: violations remain:"; head -n 5 "$PF/violations-after.txt"; return 1; }
  manifest | xargs -d '\n' stat -c '%U:%G %a %F %n' > "$PF/after.txt" \
    && [ "$(grep -c "^root:$SVC 750 directory /" "$PF/after.txt")" = 5 ] || { echo "STOP: not all 5 paths are root:$SVC 750"; return 1; }
  svc_access || { echo "STOP: $SVC cannot read its active release"; return 1; }
  health_ok "$PORT" "$REL" || { echo "STOP: production API not healthy on $REL"; return 1; }
  svc_state > "$PF/services-after.txt" || { echo "STOP: cannot read the production service state, or a unit is not active"; return 1; }
  diff "$PF/services-before.txt" "$PF/services-after.txt" || { echo "STOP: a production unit restarted or changed state"; return 1; }
  snapshot > "$PF/invariants-after.txt" 2> "$PF/invariants-after.err" && [ ! -s "$PF/invariants-after.err" ] \
    || { echo "STOP: cannot collect the invariants after the change (a command failed, or DEV is not healthy):"; head -n 5 "$PF/invariants-after.err"; return 1; }
  diff "$PF/invariants-before.txt" "$PF/invariants-after.txt" \
    || { echo "STOP: a release symlink, DEV, the PostgreSQL config, a unit or the backup script changed"; return 1; }
  audit_exact prod after && audit_exact dev after || return 1
  echo "verify OK: 0 violations; 5 paths root:$SVC 750; production healthy on $REL; units, DEV and PostgreSQL config unchanged; audits: prod $(grep -c '^OK' "$PF/audit-after-prod.txt") OK, dev $(grep -c '^OK' "$PF/audit-after-dev.txt") OK, 2 legacy FAIL each"
}

pf_pre && pf_pycache && pf_verify
