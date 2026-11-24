# W (6b-2, Stage 6 plan §5): the DEV window, switch OFF. One approval; the owner's decisions are read from
# $ST/decisions.env (README §4; W refuses without it). Order (plan A1: the old code cannot run on 0030+, so the API and
# worker are stopped BEFORE the migrations and only the new release is started afterwards):
#   W0 preflight   W1 stop DEV API, worker, cleanup timer   W2 one manual backup + verify   W3 pre-state record
#   W4 alembic upgrade exactly 0035 (live)   W5 verification SQL (stop on mismatch: X)   W6 link switch, API, then worker
#   W7 post-checks. Before W4 any failure restarts the OLD services (schema unchanged). From W4 on, a failure STOPS
#   with the services down and names X (restore the W2 set): never an automatic downgrade or a start of the old code.
MIGRATED=0; TIMER_WAS=""
restart_old() {  # only before W4: the schema is still 0029, the old release is still current
  [ "$MIGRATED" = 0 ] || { echo "UNRESOLVED: migration started; services stay DOWN; run gate X (README §6)"; return 1; }
  echo "RESTORING SERVICE: old release $(facts OLD_ID) (schema $PRE_HEAD unchanged)"
  systemctl start hyperchat-api && api_healthy "$(facts OLD_ID)" >/dev/null || { echo "UNRESOLVED: the old API did not come back; escalate"; return 1; }
  [ "$(. "$ST/decisions.env"; echo "$WORKER_WAS")" != active ] || systemctl start hyperchat-worker || echo "UNRESOLVED: worker did not start"
  [ "$TIMER_WAS" != active ] || systemctl start hyperchat-cleanup.timer || echo "UNRESOLVED: cleanup timer did not start"
  echo "W NOT DONE: DEV back on $(facts OLD_ID) at $PRE_HEAD; nothing migrated"; }
w_window() { local set_before set_w old rel
  [ -f "$ST/R-passed" ] && [ ! -e "$ST/W-passed" ] || { echo "STOP: R has not passed, or W already passed"; return 1; }
  [ -f "$ST/decisions.env" ] && (. "$ST/decisions.env"; [ "$OD_6B_WINDOW_APPROVED" = yes ] && case "$OD_6B_WORKER" in start|stopped) ;; *) exit 1 ;; esac) \
    || { echo "STOP: $ST/decisions.env missing or incomplete (OD_6B_WINDOW_APPROVED=yes, OD_6B_WORKER=start|stopped)"; return 1; }
  old="$(facts OLD_ID)"; rel="$(facts OLD_REL)"
  echo "== W0 preflight (read-only)"
  window_ok 75 && host_ok && target_ok && head_is "$PRE_HEAD" && [ "$(readlink -f "$APP_CURRENT")" = "$rel" ] \
    && api_healthy "$old" >/dev/null && [ "$(env_sha)" = "$(facts ENV_SHA)" ] && env_switch_off \
    && [ "$(tree_fp "$REL_NEW")" = "$NEW_FP" ] && pins_ok "$REL_NEW" && audit_ok "$ST/audit-W0.txt" \
    && [ "$(systemctl is-active "$BACKUP_UNIT")" != active ] || { echo "STOP: not the R state; nothing changed"; return 1; }
  [ "$(Q "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')")" = 0 ] || { echo "STOP: jobs queued or running; nothing changed"; return 1; }
  echo "WORKER_WAS=$(systemctl is-active hyperchat-worker)" >> "$ST/decisions.env"
  TIMER_WAS="$(systemctl is-active hyperchat-cleanup.timer)"
  echo "== W1 stop the DEV API, worker and cleanup timer (DEV units only; nothing shared)"
  systemctl stop hyperchat-cleanup.timer && systemctl stop hyperchat-api && systemctl stop hyperchat-worker \
    || { echo "STOP: a DEV unit did not stop"; restart_old; return 1; }
  sleep 3
  [ "$(Q "SELECT count(*) FROM pg_stat_activity WHERE datname = '$DEV_DB' AND usename IN ('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')")" = 0 ] \
    || { echo "STOP: an application session still holds the DEV database"; restart_old; return 1; }
  echo "OK    API, worker and cleanup timer stopped; no application session"
  echo "== W2 one manual backup with the DEV unit, then verify the set"
  set_before="$(latest_set)"
  systemctl start "$BACKUP_UNIT" || { echo "STOP: $BACKUP_UNIT failed"; restart_old; return 1; }
  set_w="$(latest_set)"
  [ -n "$set_w" ] && [ "$set_w" != "$set_before" ] && set_ok "$set_w" || { echo "STOP: no new verifiable set"; restart_old; return 1; }
  ( cd "$BACKUP_DIR" && sha256sum "$set_w".* ) > "$ST/W-set.sha256" && echo "$set_w" > "$ST/W-set.txt"
  echo "OK    window backup $set_w verified (the rollback reference for X)"
  echo "== W3 pre-state record"
  prestate > "$ST/W-prestate.txt" || { restart_old; return 1; }
  sed 's/^/   /' "$ST/W-prestate.txt"
  echo "== W4 alembic upgrade exactly $TARGET_HEAD on live DEV (from the new release, as root peer-mapped to hyperchat_migrator)"
  MIGRATED=1
  migrate_to "$REL_NEW" "$TARGET_HEAD" "$MIGRATION_DSN" > "$ST/W-upgrade.out" 2>&1 \
    || { echo "STOP: the migration failed (see W-upgrade.out; head now $(head_now)). Services stay DOWN: run X"; return 1; }
  head_is "$TARGET_HEAD" || { echo "Services stay DOWN: run X"; return 1; }
  echo "== W5 verification SQL (absolute no-go on any mismatch)"
  same W-prestate.txt prestate || { echo "NO-GO: the migrations changed the pre-state. Services stay DOWN: run X"; return 1; }
  verify_sql > "$ST/W-verify.txt" && [ "$(verify_fixed < "$ST/W-verify.txt")" = "$VERIFY_EXPECTED" ] \
    || { echo "NO-GO: verification SQL differs:"; diff <(echo "$VERIFY_EXPECTED") <(verify_fixed < "$ST/W-verify.txt") | sed 's/^/      /'; echo "Services stay DOWN: run X"; return 1; }
  if diff -q "$ST/R-prestate.txt" "$ST/W-prestate.txt" >/dev/null; then
    diff -q "$ST/R-recorded.txt" <(grep -E '^V11 ' "$ST/W-verify.txt") >/dev/null \
      || { echo "NO-GO: same data as the rehearsal but V11 differs. Services stay DOWN: run X"; return 1; }
  else
    echo "INFO  DEV data changed since the rehearsal set; V11 recorded for the owner:"; grep -E '^V11 ' "$ST/W-verify.txt" | sed 's/^/      /'
  fi
  echo "OK    V1-V11 as expected; totals checksum equal"
  echo "== W6 switch the release link, start the API, then (decision OD-6b-2) the worker"
  link_to "$REL_NEW" || { echo "STOP: link not switched (current $(readlink -f "$APP_CURRENT")). Services DOWN: run X"; return 1; }
  systemctl start hyperchat-api && api_healthy "$NEW_ID" && [ "$(api_cwd)" = "$REL_NEW/backend" ] \
    || { systemctl stop hyperchat-api; echo "STOP: the new API is not healthy; stopped again. Services DOWN: run X"; return 1; }
  if [ "$(. "$ST/decisions.env"; echo "$OD_6B_WORKER")" = start ]; then
    systemctl start hyperchat-worker && sleep 10 && [ "$(systemctl is-active hyperchat-worker)" = active ] \
      && [ "$(proc_cwd hyperchat-worker)" = "$REL_NEW/backend" ] \
      || { echo "STOP: the worker did not start on $NEW_ID (API stays up; decide X or a forward fix)"; return 1; }
  else echo "INFO  worker left stopped (OD-6b-2)"; fi
  [ "$TIMER_WAS" != active ] || systemctl start hyperchat-cleanup.timer || { echo "STOP: cleanup timer did not start"; return 1; }
  echo "== W7 post-checks (read-only)"
  head_is "$TARGET_HEAD" && [ "$(env_sha)" = "$(facts ENV_SHA)" ] && env_switch_off && audit_ok "$ST/audit-W7.txt" \
    && [ "$(Q 'SELECT state FROM payment_accounting_state')" = off ] && [ "$(grep -v '^jobs ' "$ST/W-prestate.txt")" = "$(prestate_core)" ] \
    && [ "$(verify_fixed < <(verify_sql))" = "$VERIFY_EXPECTED" ] \
    || { echo "STOP: a post-check failed (services are up on $NEW_ID; decide X or a forward fix)"; return 1; }
  echo "INFO  $(unit_line hyperchat-api); $(unit_line hyperchat-worker)"
  date -u +%FT%TZ > "$ST/W-passed" && echo "W OK: DEV runs $NEW_ID at $TARGET_HEAD, accounting state off, api.env unchanged"
}
w_window
