# G0 (6b-0, Stage 6 plan §5): READ-ONLY pre-checks. Writes only state files in $ST (G0-facts.env, baselines).
# Nothing is stopped, started, built, backed up or migrated. Every fact the later gates compare against is recorded.
g0() { local n=0 bad=0 old rel set worker_cwd
  ok() { echo "OK    $1"; n=$((n+1)); }; no() { echo "FAIL  $1"; bad=$((bad+1)); }
  [ ! -e "$ST/G0-passed" ] || { echo "STOP: G0 already passed (a re-run needs a new staging directory)"; return 1; }
  echo "== G0.1 identity"
  host_ok && ok "the V2 DEV host ($HOST_ID)" || return 1
  target_ok && ok "live DEV database ($DEV_IDENTITY)" || return 1
  audit_ok "$ST/audit-before.txt" && ok "check_isolation dev 23/23" || no "isolation audit"
  echo "== G0.2 current head and release (re-verified, not assumed)"
  head_is "$PRE_HEAD" && ok "alembic $PRE_HEAD" || no "alembic is $(head_now), expected $PRE_HEAD"
  rel="$(readlink -f "$APP_CURRENT")"; old="${rel##*/}"
  [[ "$old" =~ ^[0-9a-f]{12}$ ]] && [ -d "$rel/backend" ] && ok "current release $old (brief: 8e1284871a05; C1 r3 record: 0963113493c7)" \
    || no "current release link $rel"
  [ "$old" != "$NEW_ID" ] && [ ! -e "$REL_NEW" ] && ok "$REL_NEW does not exist yet" || no "$REL_NEW already exists or is current"
  api_healthy "$old" >/dev/null && [ "$(api_cwd)" = "$rel/backend" ] && ok "API healthy on $old" || no "API not healthy on $old"
  worker_cwd="$(proc_cwd hyperchat-worker)"
  echo "INFO  worker runs $worker_cwd ($(unit_line hyperchat-worker))"
  [ -f "$rel/database/migrations/versions/0032_donation_reversal_accounting.py" ] \
    && no "the current release already has 0032 (unexpected for head 0029)" || ok "current release predates the writer migration 0032"
  echo "== G0.3 health and quiet: queue, backups, timers"
  [ "$(Q "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")" = 0 ] && ok "job queue empty" || no "jobs queued or running"
  [ "$(Q "SELECT count(*) FROM realtime_outbox WHERE status IN ('pending', 'publishing')")" = 0 ] && ok "realtime outbox drained" \
    || no "realtime outbox has pending or publishing rows"
  [ "$(systemctl is-active "$BACKUP_UNIT")" != active ] && [ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name '.partial-*')" ] \
    && ok "no backup running, no partial set" || no "a backup is running or a partial set exists"
  for t in hyperchat-cleanup.timer hyperchat-dev-pgbackup.timer; do echo "INFO  $t $(systemctl is-active "$t") next $(systemctl show -p NextElapseUSecRealtime --value "$t")"; done
  systemctl list-timers --all --no-pager 2>/dev/null | grep -Ei 'hyperchat|s0-6|offsite' | sed 's/^/INFO  timer: /'
  echo "== G0.4 capacity"
  echo "INFO  $(df -BM --output=avail,target "$SCRATCH_ROOT" "$BACKUP_DIR" /tmp | tr -s ' ' | tr '\n' ';')"
  [ "$(df -BG --output=avail "$SCRATCH_ROOT" | tail -1 | tr -dc 0-9)" -ge 4 ] && ok "$SCRATCH_ROOT has >= 4 GiB for two scratch clusters" || no "$SCRATCH_ROOT below 4 GiB"
  [ "$(df -BG --output=avail "$BACKUP_DIR" | tail -1 | tr -dc 0-9)" -ge 10 ] && ok "$BACKUP_DIR has >= 10 GiB" || no "$BACKUP_DIR below 10 GiB"
  echo "INFO  memory $(free -m | awk '/^Mem:/ {print $7 " MiB available of " $2}')"
  echo "== G0.5 the latest scheduled backup set"
  set="$(latest_set)"
  [ -n "$set" ] && set_ok "$set" && ok "latest set $set: dump and manifest SHA-256 verify" || no "no verifiable latest set"
  [ -n "$set" ] && [ "$(find "$BACKUP_DIR" -maxdepth 1 -name "$set.manifest.json" -mmin -1500 | wc -l)" = 1 ] \
    && ok "it is from the last 25 hours (the nightly run)" || no "the latest set is older than 25 hours"
  echo "== G0.6 pre-state record and the dependency baseline"
  prestate > "$ST/prestate-G0.txt" && [ "$(wc -l < "$ST/prestate-G0.txt")" = 9 ] && ok "pre-state recorded (9 lines)" || no "pre-state"
  sed 's/^/   /' "$ST/prestate-G0.txt"
  "$rel/venv/bin/pip" freeze 2>/dev/null | LC_ALL=C sort > "$ST/pip-old.txt"
  [ -s "$ST/pip-old.txt" ] && ok "the running release's freeze recorded ($(wc -l < "$ST/pip-old.txt") packages; B requires the new venv to equal it)" \
    || no "cannot read the running release's pip freeze"
  [ "$(file_mode "$API_ENV")" = "root:hyperchat 600" ] && env_switch_off && ok "api.env root:hyperchat 600, accounting switch not enabled" || no "api.env"
  [ -f "$ROLES" ] && [ "$(sha256sum "$ROLES" | cut -c1-16)" = "$ROLES_SHA" ] && ok "roles.sql copy $ROLES_SHA" || no "roles.sql copy"
  [ -f "$AUDIT" ] || no "isolation audit script missing"
  { echo "OLD_ID=$old"; echo "OLD_REL=$rel"; echo "ENV_SHA=$(env_sha)"; echo "SET_G0=$set"
    echo "API=$(unit_line hyperchat-api)"; echo "WORKER=$(unit_line hyperchat-worker)"; echo "WORKER_CWD=$worker_cwd"
    echo "DB_ACL=$(Q "SELECT coalesce(datacl::text, '-') FROM pg_database WHERE datname = current_database()")"
    echo "DB_SETTINGS=$(Q "SELECT coalesce(string_agg(setconfig::text, ';' ORDER BY setrole), '-') FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase WHERE d.datname = current_database()")"
  } > "$ST/G0-facts.env"
  echo "== $n OK, $bad FAIL"
  [ "$bad" = 0 ] || { echo "STOP: G0 failed; nothing was changed"; return 1; }
  date -u +%FT%TZ > "$ST/G0-passed" && echo "G0 OK: facts recorded in $ST/G0-facts.env; nothing changed"
}
g0
