: permfix-dev
# HyperChat DEV permission repair: the 117 recorded paths only (2 application directories + 115 __pycache__).
# Each path: root:root 0755 directory -> root:hyperchat 0750. Explicit paths only: no recursion, no symlink
# followed, no file outside the manifest touched, no service restarted or reloaded. Fails closed at the first
# surprise. Production (/opt/hyperchat-prod) is never touched.
A=/opt/hyperchat; SVC=hyperchat; REL=229fe42381e2
AUDIT=/root/hyperchat-c2fe883/deploy/check_isolation.sh
PF=/root/rehearsal-dev/permfix
MANIFEST_SHA=0f172cd0c6e286a05b97713c41f8aa215fa1f7b0a02fe87bd793e04fe162b9f9
manifest() { cat <<'EOF'
/opt/hyperchat
/opt/hyperchat/releases
/opt/hyperchat/releases/1fa9ac548c50/backend/app/__pycache__
/opt/hyperchat/releases/1fa9ac548c50/backend/app/auth/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/admin/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/auth/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/bots/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/core/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/jobs/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/payments/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/security/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/229fe42381e2/backend/app/storage/__pycache__
/opt/hyperchat/releases/229fe42381e2/database/migrations/__pycache__
/opt/hyperchat/releases/229fe42381e2/database/migrations/versions/__pycache__
/opt/hyperchat/releases/3c0618847bed/backend/app/__pycache__
/opt/hyperchat/releases/3c0618847bed/backend/app/core/__pycache__
/opt/hyperchat/releases/3c0618847bed/database/migrations/__pycache__
/opt/hyperchat/releases/3c0618847bed/database/migrations/versions/__pycache__
/opt/hyperchat/releases/4093ec1d4f87/backend/app/__pycache__
/opt/hyperchat/releases/4093ec1d4f87/backend/app/core/__pycache__
/opt/hyperchat/releases/4093ec1d4f87/database/migrations/versions/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/admin/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/audio/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/auth/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/authz/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/bots/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/core/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/dashboard/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/db/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/jobs/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/obs/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/partners/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/payments/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/public/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/realtime/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/routers/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/security/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/storage/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/backend/app/uploads/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/database/migrations/__pycache__
/opt/hyperchat/releases/569ed5dd2a7e/database/migrations/versions/__pycache__
/opt/hyperchat/releases/6368fffab9f5/database/migrations/__pycache__
/opt/hyperchat/releases/6368fffab9f5/database/migrations/versions/__pycache__
/opt/hyperchat/releases/9042fb7fff40/database/migrations/__pycache__
/opt/hyperchat/releases/9042fb7fff40/database/migrations/versions/__pycache__
/opt/hyperchat/releases/940137070657/database/migrations/__pycache__
/opt/hyperchat/releases/940137070657/database/migrations/versions/__pycache__
/opt/hyperchat/releases/a041ac62ac46/database/migrations/__pycache__
/opt/hyperchat/releases/a041ac62ac46/database/migrations/versions/__pycache__
/opt/hyperchat/releases/b88cf58b388f/backend/app/__pycache__
/opt/hyperchat/releases/b88cf58b388f/backend/app/core/__pycache__
/opt/hyperchat/releases/c11ce68b114d/database/migrations/__pycache__
/opt/hyperchat/releases/c11ce68b114d/database/migrations/versions/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/admin/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/auth/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/bots/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/core/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/jobs/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/payments/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/security/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/c1bf2804878a/backend/app/storage/__pycache__
/opt/hyperchat/releases/c1bf2804878a/database/migrations/versions/__pycache__
/opt/hyperchat/releases/c3f8276dba88/database/migrations/__pycache__
/opt/hyperchat/releases/c3f8276dba88/database/migrations/versions/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/admin/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/auth/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/bots/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/core/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/jobs/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/payments/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/security/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/d5f8010452b7/backend/app/storage/__pycache__
/opt/hyperchat/releases/d6c03ae825e0/database/migrations/versions/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/admin/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/audio/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/auth/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/authz/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/bots/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/core/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/dashboard/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/db/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/jobs/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/obs/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/partners/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/payments/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/public/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/realtime/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/routers/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/security/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/storage/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/backend/app/uploads/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/database/migrations/__pycache__
/opt/hyperchat/releases/dbaa74c66d88/database/migrations/versions/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/admin/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/auth/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/bots/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/core/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/jobs/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/payments/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/security/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/soundboard/__pycache__
/opt/hyperchat/releases/e953eab498f6/backend/app/storage/__pycache__
/opt/hyperchat/releases/e953eab498f6/database/migrations/__pycache__
/opt/hyperchat/releases/e953eab498f6/database/migrations/versions/__pycache__
/opt/hyperchat/releases/f592286f4d9f/database/migrations/__pycache__
/opt/hyperchat/releases/f592286f4d9f/database/migrations/versions/__pycache__
EOF
}
TOP="$A $A/releases"
pycache() { manifest | grep '/__pycache__$'; }
violations() {  # the audit's strict predicate, sorted; non-zero when find or sort fails
  local v s
  v="$(find "$A" ! -type l \( ! -user root -o -perm /0022 -o -perm /0007 \) -print)" || return 1
  [ -n "$v" ] || return 0
  s="$(printf '%s\n' "$v" | LC_ALL=C sort)" || return 1
  printf '%s\n' "$s"
}
is_plain_dir() { [ -d "$1" ] && [ ! -L "$1" ] && [ "$(readlink -f "$1")" = "$1" ]; }
svc_state() {  # fails unless systemctl answers and both DEV services are active
  local x; x="$(systemctl show hyperchat-api hyperchat-worker -p MainPID -p NRestarts -p ActiveState)" || return 1
  [ "$(printf '%s\n' "$x" | grep -cx 'ActiveState=active')" = 2 ] || return 1
  printf '%s' "$x" | tr '\n' ' '
}
svc_access() {  # the service user reaches and reads its active release
  local R; R="$(readlink -f "$A/current")"
  sudo -u "$SVC" test -x "$A" && sudo -u "$SVC" test -x "$A/releases" && sudo -u "$SVC" test -r "$R/backend/app/main.py" \
    && sudo -u "$SVC" test -x "$R/venv/bin/python"
}
api_ok() {  # one /health read: status ok, database ok, release $REL
  curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null | python3 -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$REL"
}
consumers() {  # every installed unit that runs code from $A, with its user; nginx and cron must name none
  local f
  for f in $(grep -lE "/opt/hyperchat(/|$|[[:space:]])" /etc/systemd/system/*.service /etc/systemd/system/*.timer /lib/systemd/system/*.service 2>/dev/null); do
    echo "$f User=$(grep -E '^User=' "$f" | tail -1 | cut -d= -f2)"
  done
  grep -rlE "/opt/hyperchat(/|;|[[:space:]]|$)" /etc/nginx /etc/cron* /var/spool/cron 2>/dev/null
  return 0
}
CONSUMERS="/etc/systemd/system/hyperchat-api.service User=hyperchat
/etc/systemd/system/hyperchat-cleanup.service User=hyperchat
/etc/systemd/system/hyperchat-worker.service User=hyperchat"
# what must not change at all: the DEV release symlink, production (tree top, symlink, health, services), the
# PostgreSQL client-auth files of both clusters, the env-file metadata and the unit files (read-only)
# Fails (non-zero) when any command fails or returns nothing; production must be healthy and its API active.
snapshot() {
  local x h
  x="$(readlink "$A/current")" && [ -n "$x" ] || return 1; echo "dev current -> $x"
  x="$(stat -c '%i %U:%G %Z' "$A/current")" || return 1; echo "dev current link $x"
  x="$(stat -c '%U:%G %a %Y' /etc/hyperchat/api.env)" || return 1; echo "dev env $x"
  x="$(readlink /opt/hyperchat-prod/current)" && [ -n "$x" ] || return 1; echo "prod current -> $x"
  stat -c '%U:%G %a %i %Y %Z %n' /opt/hyperchat-prod /opt/hyperchat-prod/releases /opt/hyperchat-prod/current || return 1
  h="$(curl -fsS --max-time 5 http://127.0.0.1:8001/health)" || return 1
  x="$(printf '%s' "$h" | python3 -c 'import json, sys; d = json.load(sys.stdin); assert d["status"] == "ok" and d["database"]["status"] == "ok"; print(d["status"], d.get("release"), d["database"]["status"])')" || return 1
  echo "prod health $x"
  x="$(systemctl show hyperchat-prod-api -p MainPID -p NRestarts -p ActiveState)" || return 1
  printf '%s\n' "$x" | grep -qx 'ActiveState=active' || return 1
  echo "prod api $(printf '%s' "$x" | tr '\n' ' ')"
  sha256sum /etc/postgresql/15/restoretest/pg_hba.conf /etc/postgresql/15/restoretest/pg_ident.conf \
    /etc/postgresql/17/hyperchat/pg_hba.conf /etc/postgresql/17/hyperchat/pg_ident.conf /etc/systemd/system/hyperchat*.service || return 1
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

# 1. Before-state: manifest intact, live state == manifest, every path a plain root:root 0755 directory, the
#    running services hold group hyperchat, API healthy. Records everything under $PF (single-use: mkdir fails if
#    it exists).
pf_pre() {
  local p g g2 a
  [ "$(id -u)" = 0 ] || { echo "STOP: run as root"; return 1; }
  [ "$(manifest | sha256sum | cut -c1-64)" = "$MANIFEST_SHA" ] || { echo "STOP: embedded manifest altered"; return 1; }
  [ "$(manifest | wc -l)" = 117 ] && [ "$(pycache | wc -l)" = 115 ] || { echo "STOP: manifest is not 2 + 115 paths"; return 1; }
  [ "$(manifest | grep -vc '^/opt/hyperchat/releases/[0-9a-f]\{12\}/[A-Za-z0-9_/]*/__pycache__$')" = 2 ] \
    && [ "$(manifest | grep -v '/__pycache__$')" = "$A"$'\n'"$A/releases" ] || { echo "STOP: a manifest path is outside the DEV release tree"; return 1; }
  [ "$(readlink -f "$A/current")" = "$A/releases/$REL" ] || { echo "STOP: current is not release $REL"; return 1; }
  [ "$(consumers)" = "$CONSUMERS" ] || { echo "STOP: unexpected consumers of $A:"; consumers; return 1; }
  [ -d /root/rehearsal-dev ] && mkdir -m 700 "$PF" || { echo "STOP: $PF exists or /root/rehearsal-dev missing"; return 1; }
  snapshot > "$PF/invariants-before.txt" 2> "$PF/invariants-before.err" && [ ! -s "$PF/invariants-before.err" ] \
    || { echo "STOP: cannot collect the invariants (a command failed or production is not healthy):"; head -n 5 "$PF/invariants-before.err"; return 1; }
  violations > "$PF/violations-before.txt" 2> "$PF/violations-before.err" && [ ! -s "$PF/violations-before.err" ] \
    || { echo "STOP: the violations scan failed (find or sort)"; head -n 5 "$PF/violations-before.err"; return 1; }
  diff <(manifest) "$PF/violations-before.txt" > "$PF/drift.diff" \
    || { echo "STOP: live violations differ from the manifest (< manifest only / > live only):"; cat "$PF/drift.diff"; return 1; }
  while IFS= read -r p; do
    is_plain_dir "$p" || { echo "STOP: $p is not a plain directory (symlink on the way?)"; return 1; }
    [ "$(stat -c '%U:%G %a %F' "$p")" = "root:root 755 directory" ] || { echo "STOP: $p is $(stat -c '%U:%G %a %F' "$p")"; return 1; }
    a="$(lsattr -d "$p")" && [ -n "$a" ] || { echo "STOP: cannot read the attributes of $p"; return 1; }
    [ "$(printf '%s' "$a" | cut -c5)" != i ] || { echo "STOP: $p is immutable"; return 1; }
  done < <(manifest)
  manifest | xargs -d '\n' stat -c '%U:%G %a %F %n' > "$PF/before.txt" && [ "$(grep -c '^root:root 755 directory /' "$PF/before.txt")" = 117 ] \
    || { echo "STOP: cannot record the before-state of the 117 paths"; return 1; }
  g="$(getent group "$SVC" | cut -d: -f3)"; [ -n "$g" ] && id -G "$SVC" | tr ' ' '\n' | grep -qx "$g" \
    || { echo "STOP: $SVC is not in group $SVC"; return 1; }
  g2="$(systemctl show -p MainPID --value hyperchat-api hyperchat-worker)" && [ "$(printf '%s\n' $g2 | grep -c '^[1-9][0-9]*$')" = 2 ] \
    || { echo "STOP: cannot read the API and worker main PIDs"; return 1; }
  for p in $g2; do
    [ "$p" -gt 0 ] 2>/dev/null && grep -E '^Groups:' "/proc/$p/status" | tr -s ' \t' '\n' | grep -qx "$g" \
      || { echo "STOP: running process $p does not hold group $SVC ($g)"; return 1; }
  done
  svc_access || { echo "STOP: $SVC cannot read its active release before the change"; return 1; }
  api_ok || { echo "STOP: API not healthy on $REL before the change"; return 1; }
  svc_state > "$PF/services-before.txt" || { echo "STOP: cannot read the DEV service state, or a service is not active"; return 1; }
  echo "pre OK: 117 paths == manifest, all root:root 755 plain directories; $(cat "$PF/services-before.txt")"
}

# 2. Application directories: group first (service access through the group), check, then remove "other".
#    An access or health failure puts that directory's mode back to 0755 at once and stops.
pf_top() {
  local d
  for d in $TOP; do chgrp --no-dereference "$SVC" "$d" || { echo "STOP: chgrp $d failed"; return 1; }; done
  for d in $TOP; do [ "$(stat -c '%U:%G %a' "$d")" = "root:$SVC 755" ] || { echo "STOP: $d is $(stat -c '%U:%G %a' "$d") after chgrp"; return 1; }; done
  for d in $A/releases $A; do
    chmod 0750 "$d" || { echo "STOP: chmod $d failed"; return 1; }
    [ "$(stat -c '%U:%G %a' "$d")" = "root:$SVC 750" ] && svc_access && api_ok \
      || { chmod 0755 "$d"; echo "STOP: $SVC lost access or API unhealthy after chmod $d; its mode is back to 0755"; return 1; }
  done
  echo "top OK: $A and $A/releases are root:$SVC 750; $SVC reads its release; API healthy"
}

# 3. The 115 __pycache__ directories, one by one (their 0640 root:root .pyc files are already compliant: untouched).
pf_pycache() {
  local p n=0
  while IFS= read -r p; do
    is_plain_dir "$p" && [ "$(stat -c '%U:%G %a' "$p")" = "root:root 755" ] || { echo "STOP: $p changed since pf_pre ($n done)"; return 1; }
    chgrp --no-dereference "$SVC" "$p" && chmod 0750 "$p" || { echo "STOP: change failed on $p ($n done)"; return 1; }
    n=$((n + 1))
  done < <(pycache)
  [ "$n" = 115 ] || { echo "STOP: $n of 115 changed"; return 1; }
  echo "pycache OK: 115 directories root:$SVC 750"
}

# 4. After: no violation left, exactly the 117 paths changed, service access, health, same PIDs and restart counts,
#    and the audit prints exactly the expected lines (the two legacy pg_hba / pg_ident FAILs only).
pf_verify() {
  local rc
  violations > "$PF/violations-after.txt" 2> "$PF/violations-after.err" && [ ! -s "$PF/violations-after.err" ] \
    || { echo "STOP: the violations scan failed after the change (find or sort)"; head -n 5 "$PF/violations-after.err"; return 1; }
  [ ! -s "$PF/violations-after.txt" ] || { echo "STOP: violations remain:"; head -n 5 "$PF/violations-after.txt"; return 1; }
  manifest | xargs -d '\n' stat -c '%U:%G %a %F %n' > "$PF/after.txt" \
    && [ "$(grep -c "^root:$SVC 750 directory /" "$PF/after.txt")" = 117 ] || { echo "STOP: not all 117 paths are root:$SVC 750"; return 1; }
  svc_access || { echo "STOP: $SVC cannot read its active release"; return 1; }
  api_ok || { echo "STOP: API not healthy on $REL"; return 1; }
  svc_state > "$PF/services-after.txt" || { echo "STOP: cannot read the DEV service state, or a service is not active"; return 1; }
  diff "$PF/services-before.txt" "$PF/services-after.txt" || { echo "STOP: a service restarted or changed state"; return 1; }
  snapshot > "$PF/invariants-after.txt" 2> "$PF/invariants-after.err" && [ ! -s "$PF/invariants-after.err" ] \
    || { echo "STOP: cannot collect the invariants after the change (a command failed or production is not healthy):"; head -n 5 "$PF/invariants-after.err"; return 1; }
  diff "$PF/invariants-before.txt" "$PF/invariants-after.txt" \
    || { echo "STOP: the release symlink, production, PostgreSQL config or a unit changed"; return 1; }
  bash "$AUDIT" dev > "$PF/audit.txt" 2> "$PF/audit.err"; rc=$?
  [ "$rc" = 1 ] && [ ! -s "$PF/audit.err" ] || { echo "STOP: audit exited $rc or wrote to stderr"; return 1; }
  audit_expected dev > "$PF/audit.expected" || { echo "STOP: cannot build the expected lines"; return 1; }
  diff <(LC_ALL=C sort "$PF/audit.expected") <(LC_ALL=C sort "$PF/audit.txt") > "$PF/audit.diff" \
    || { echo "STOP: audit differs from the expected lines:"; cat "$PF/audit.diff"; return 1; }
  echo "verify OK: 0 violations; 117 paths root:$SVC 750; API healthy on $REL; services, release symlink, production and PostgreSQL config unchanged; audit: $(grep -c '^OK' "$PF/audit.txt") OK + 2 legacy FAIL"
}

pf_pre && pf_top && pf_pycache && pf_verify
