: permfix-dev-rollback
# Puts the 117 recorded paths back to their recorded before-state, root:root 0755 (application directories first,
# so "other" access returns before anything else). Explicit paths only; no recursion; no symlink followed; no
# service restarted. Safe to re-run.
A=/opt/hyperchat; REL=229fe42381e2
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
rb() {
  local p x bad=0
  [ "$(id -u)" = 0 ] || { echo "STOP: run as root"; return 1; }
  [ "$(manifest | sha256sum | cut -c1-64)" = "$MANIFEST_SHA" ] || { echo "STOP: embedded manifest altered"; return 1; }
  while IFS= read -r p; do
    [ -d "$p" ] && [ ! -L "$p" ] && [ "$(readlink -f "$p")" = "$p" ] || { echo "SKIP: $p is not a plain directory"; bad=1; continue; }
    chmod 0755 "$p" && chgrp --no-dereference root "$p" || { echo "FAILED: $p"; bad=1; }
  done < <(manifest)
  x="$(manifest | xargs -d '\n' stat -c '%U:%G %a %F')" && [ "$(printf '%s\n' "$x" | grep -c '^root:root 755 directory$')" = 117 ] \
    || { echo "not all 117 paths are root:root 755"; bad=1; }
  sudo -u hyperchat test -r "$(readlink -f "$A/current")/backend/app/main.py" || { echo "hyperchat cannot read its release"; bad=1; }
  curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$REL" \
    || { echo "DEV API not healthy on $REL"; bad=1; }
  x="$(systemctl show hyperchat-api hyperchat-worker -p MainPID -p NRestarts -p ActiveState)" \
    && [ "$(printf '%s\n' "$x" | grep -cx 'ActiveState=active')" = 2 ] && printf '%s\n' "$x" | tr '\n' ' ' && echo \
    || { echo "cannot read the DEV service state, or a service is not active"; bad=1; }
  [ "$bad" = 0 ] && echo "rollback OK: 117 paths root:root 755" || { echo "rollback INCOMPLETE: see above"; return 1; }
}
rb
