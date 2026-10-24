: permfix-prod-rollback
# Puts the 5 recorded production __pycache__ directories back to their recorded before-state, root:root 0755.
# Explicit paths only; no recursion; no symlink followed; no service restarted. Safe to re-run.
A=/opt/hyperchat-prod; SVC=hyperchat-prod; REL=940137070657
MANIFEST_SHA=c61c0dc479119ec8352e26925c079341c61637fe2964e76a1d4a04f5953fbb1c
manifest() { cat <<'EOF'
/opt/hyperchat-prod/releases/940137070657/backend/app/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/admin/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/auth/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/core/__pycache__
/opt/hyperchat-prod/releases/940137070657/backend/app/security/__pycache__
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
  x="$(manifest | xargs -d '\n' stat -c '%U:%G %a %F')" && [ "$(printf '%s\n' "$x" | grep -c '^root:root 755 directory$')" = 5 ] \
    || { echo "not all 5 paths are root:root 755"; bad=1; }
  sudo -u "$SVC" test -r "$(readlink -f "$A/current")/backend/app/main.py" || { echo "$SVC cannot read its release"; bad=1; }
  curl -fsS --max-time 5 http://127.0.0.1:8001/health 2>/dev/null | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$REL" \
    || { echo "production API not healthy on $REL"; bad=1; }
  x="$(systemctl show hyperchat-prod-api hyperchat-prod-cleanup.timer -p MainPID -p NRestarts -p ActiveState)" \
    && [ "$(printf '%s\n' "$x" | grep -cx 'ActiveState=active')" = 2 ] && printf '%s\n' "$x" | tr '\n' ' ' && echo \
    || { echo "cannot read the production unit state, or a unit is not active"; bad=1; }
  [ "$bad" = 0 ] && echo "rollback OK: 5 paths root:root 755" || { echo "rollback INCOMPLETE: see above"; return 1; }
}
rb
