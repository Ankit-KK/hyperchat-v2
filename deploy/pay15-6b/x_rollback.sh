# X (Stage 6 plan §8): rollback of the 6b window. Only with its own owner approval. Nothing is deleted:
#   X1 stop the DEV API and worker (and the cleanup timer)
#   X2 restore the W2 window set into a NEW database hyperchat_dev_restore_<UTC> on the DEV cluster (pg_restore,
#      single transaction) and check it: head 0029, pre-state equal to W-prestate.txt
#   X3 swap by rename: hyperchat_dev -> hyperchat_dev_failed_<UTC> (kept for the incident), restore -> hyperchat_dev
#   X4 the link back to the previous release; start the API (and the worker if it ran before); verify head 0029,
#      health on the old release, the pre-state equal; isolation 23/23. Then an incident note (README §6).
x_rollback() { local set ts old rel R F
  [ -f "$ST/G0-passed" ] && [ -f "$ST/W-set.txt" ] && [ ! -e "$ST/X-done" ] || { echo "STOP: no window backup recorded, or X already ran"; return 1; }
  host_ok && target_ok || return 1
  set="$(cat "$ST/W-set.txt")"; ts="$(date -u +%Y%m%dT%H%M%SZ)"; old="$(facts OLD_ID)"; rel="$(facts OLD_REL)"
  R="${DEV_DB}_restore_$ts"; F="${DEV_DB}_failed_$ts"
  ( cd "$BACKUP_DIR" && sha256sum -c --quiet "$ST/W-set.sha256" ) && set_ok "$set" || { echo "STOP: the window set no longer verifies"; return 1; }
  A() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -h "$DEV_SOCK" -p "$DEV_PORT" -d postgres -Atc "$1"; }   # admin, DEV cluster
  echo "== X1 stop the DEV services"
  systemctl stop hyperchat-cleanup.timer hyperchat-api hyperchat-worker || { echo "STOP: a DEV unit did not stop"; return 1; }
  echo "== X2 restore $set into $R"
  # peer auth on the live cluster: pg_restore runs as postgres and reads the dump on stdin (root opens the file)
  A "CREATE DATABASE $R OWNER hyperchat_owner" && sudo -u postgres "$PGBIN/pg_restore" --exit-on-error --single-transaction -h "$DEV_SOCK" \
      -p "$DEV_PORT" -d "$R" < "$BACKUP_DIR/$set.dump" || { echo "STOP: restore into $R failed ($R kept for inspection); live DEV unchanged, services DOWN"; return 1; }
  [ "$(QF /dev/stdin "$DEV_SOCK" "$DEV_PORT" "$R" <<< 'SELECT string_agg(version_num, $$,$$) FROM alembic_version')" = "$PRE_HEAD" ] \
    && diff "$ST/W-prestate.txt" <(QF "$PKG/sql/prestate.sql" "$DEV_SOCK" "$DEV_PORT" "$R") \
    || { echo "STOP: $R is not head $PRE_HEAD with the W pre-state; live DEV unchanged, services DOWN"; return 1; }
  echo "OK    $R is head $PRE_HEAD with the recorded pre-state"
  echo "== X3 swap by rename (no database is dropped)"
  [ "$(A "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('$DEV_DB', '$R') AND pid <> pg_backend_pid()")" = 0 ] \
    || { echo "STOP: sessions still use $DEV_DB or $R; services DOWN"; return 1; }
  A "ALTER DATABASE $DEV_DB RENAME TO $F" && A "ALTER DATABASE $R RENAME TO $DEV_DB" \
    || { echo "UNRESOLVED: rename swap incomplete; check pg_database; services DOWN; escalate"; return 1; }
  echo "OK    $DEV_DB is the restored set; the migrated database is kept as $F"
  # a per-database dump carries no database-level ACL or settings: they must equal what G0 recorded before any start
  [ "$(A "SELECT coalesce(datacl::text, '-') FROM pg_database WHERE datname = '$DEV_DB'")" = "$(facts DB_ACL)" ] \
    && [ "$(A "SELECT coalesce(string_agg(setconfig::text, ';' ORDER BY setrole), '-') FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase WHERE d.datname = '$DEV_DB'")" = "$(facts DB_SETTINGS)" ] \
    || { echo "UNRESOLVED: $DEV_DB's database ACL / settings differ from G0's (re-apply them as recorded in G0-facts.env); services DOWN"; return 1; }
  echo "== X4 previous release back, services started, verified"
  link_to "$rel" && systemctl start hyperchat-api && api_healthy "$old" && head_is "$PRE_HEAD" && [ "$(grep -v '^jobs ' "$ST/W-prestate.txt")" = "$(prestate_core)" ] \
    || { echo "UNRESOLVED: see above; escalate"; return 1; }
  [ "$(. "$ST/decisions.env"; echo "${WORKER_WAS:-}")" != active ] || systemctl start hyperchat-worker
  systemctl start hyperchat-cleanup.timer; audit_ok "$ST/audit-X.txt" || echo "FAIL  isolation audit after X"
  echo "$ts $set $F" > "$ST/X-done"
  echo "X DONE: DEV back on $old at $PRE_HEAD from $set; $F kept (drop only on approval); write the incident note (README §6)"
}
x_rollback
