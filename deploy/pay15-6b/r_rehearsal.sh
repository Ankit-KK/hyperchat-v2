# R (6b-1, Stage 6 plan §5): the rehearsal on restored copies of the latest DEV set. NO live DEV change: every write
# goes to a private scratch cluster in $SCRATCH_ROOT (Unix socket in its own directory, listen_addresses empty, no
# TCP), which is stopped and deleted at the end whatever happens. Live DEV is only read (before/after equality).
#   R1 the drilled restore check: restore_check --scratch --expect-head 0029 (validity + the timed restore drill, A2)
#   R2 a second scratch cluster built the same way (restore_check.scratch_cluster): restore, pre-state, the full
#      0029 -> 0035 upgrade with the new release's Alembic (timed), the verification SQL, the 0035 precondition, the
#      read-only verifier, then the downgrade-to-0029 check (expected to REFUSE at 0031: rollback = restore, A2)
R_SCRATCH_W=""; R_SCRATCH_DATA=""   # globals: the RETURN trap runs after r_rehearsal's locals are gone (set -u)
r_scratch_cleanup() {  # stop and delete R2's scratch cluster, once; the trap clears itself first
  trap - RETURN
  [ -z "$R_SCRATCH_DATA" ] || runuser -u postgres -- "$PGBIN/pg_ctl" -D "$R_SCRATCH_DATA" -m immediate stop >/dev/null 2>&1
  [ -z "$R_SCRATCH_W" ] || rm -rf -- "$R_SCRATCH_W"
  R_SCRATCH_W=""; R_SCRATCH_DATA=""; }
r_rehearsal() { local set rcc t0 t1 W="" PORT SOCK DATA started=0 dsn old_rel nok=0 nbad=0
  ok() { echo "OK    $1"; nok=$((nok+1)); }; bad() { echo "FAIL  $1"; nbad=$((nbad+1)); }
  [ -f "$ST/B-passed" ] && [ ! -e "$ST/R-passed" ] || { echo "STOP: B has not passed, or R already passed"; return 1; }
  window_ok 90 || return 1
  host_ok && target_ok && head_is "$PRE_HEAD" && [ "$(readlink -f "$APP_CURRENT")" = "$(facts OLD_REL)" ] \
    && [ "$(tree_fp "$REL_NEW")" = "$NEW_FP" ] && pins_ok "$REL_NEW" || { echo "STOP: not the B state"; return 1; }
  set="$(latest_set)"; set_ok "$set" || return 1
  prestate > "$ST/prestate-live-R-before.txt" || return 1
  # R1 uses the RUNNING release's restore_check: the set is at 0029, and the new release's checker (since 0031) treats
  # runtime writes on streamer_donator_totals as a leak although they are legitimate before 0031 (the C1 A-gate way)
  old_rel="$(facts OLD_REL)"
  echo "== R1 restore_check --scratch on $set with the running release's checker (expect-head $PRE_HEAD; TMPDIR=$SCRATCH_ROOT)"
  echo "INFO  checker: $old_rel/backend/app/backup/restore_check.py sha256 $(sha256sum "$old_rel/backend/app/backup/restore_check.py" | cut -c1-16) (release $(facts OLD_ID))"
  t0=$(date +%s)
  ( cd "$old_rel/backend" && env TMPDIR="$SCRATCH_ROOT" "$old_rel/venv/bin/python" -B -E -s -m app.backup.restore_check --dir "$BACKUP_DIR" \
      --set "$set" --env dev --pg-bin "$PGBIN" --scratch --scratch-user postgres --expect-head "$PRE_HEAD" --roles-sql "$ROLES" \
      --output "$ST/R1-restorecheck-$set.json" > "$ST/R1-restore_check.out" 2>&1 ); rcc=$?
  t1=$(date +%s); echo "INFO  restore drill: $((t1 - t0)) s (rollback path X restores the same way)"
  [ "$rcc" = 0 ] && ok "restore_check exit 0: structurally valid at $PRE_HEAD, witness consistent / not applicable" \
    || { [ "$rcc" = 3 ] && echo "INFO  restore_check exit 3 (witness: see the JSON; expected for an older set per backup-recovery.md)" \
         || bad "restore_check exit $rcc"; }
  [ -z "$(ls -d "$SCRATCH_ROOT"/hc-restorecheck-* 2>/dev/null)" ] && ok "R1 scratch cluster removed" || bad "an R1 scratch directory remains"
  echo "== R2 the upgrade rehearsal on a second scratch cluster"
  W="$(mktemp -d "$SCRATCH_ROOT/hc-6b1-XXXXXX")" && DATA="$W/data" && SOCK="$W/sock" && mkdir "$SOCK" && chown postgres "$W" "$SOCK" \
    || { echo "STOP: scratch directory"; return 1; }
  PORT="$(python3 -I -B -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  R_SCRATCH_W="$W"; R_SCRATCH_DATA="$DATA"
  trap r_scratch_cleanup RETURN
  runuser -u postgres -- "$PGBIN/initdb" -D "$DATA" -A trust -U postgres --no-sync >/dev/null \
    && runuser -u postgres -- "$PGBIN/pg_ctl" -D "$DATA" -w -l "$DATA/server.log" \
         -o "-p $PORT -k $SOCK -c listen_addresses='' -c fsync=off" start >/dev/null && started=1 \
    || { bad "scratch cluster did not start"; return 1; }
  S() { psql -X -q -v ON_ERROR_STOP=1 -h "$SOCK" -p "$PORT" -U postgres "$@"; }   # root as client, trust on the private socket
  S -d postgres -f "$ROLES" >/dev/null && S -d postgres -c "CREATE DATABASE $DEV_DB OWNER hyperchat_owner" >/dev/null \
    && "$PGBIN/pg_restore" --exit-on-error --single-transaction -h "$SOCK" -p "$PORT" -U postgres -d "$DEV_DB" \
         "$BACKUP_DIR/$set.dump" && ok "set $set restored into the scratch cluster (port $PORT socket only)" \
    || { bad "scratch restore"; return 1; }
  [ "$(S -d "$DEV_DB" -Atc 'SELECT string_agg(version_num, $$,$$) FROM alembic_version')" = "$PRE_HEAD" ] && ok "scratch head $PRE_HEAD" || bad "scratch head"
  QF "$PKG/sql/prestate.sql" "$SOCK" "$PORT" > "$ST/R2-prestate-before.txt" && ok "scratch pre-state recorded" || bad "scratch pre-state"
  dsn="postgresql+psycopg://hyperchat_migrator@/$DEV_DB?host=$SOCK&port=$PORT"
  t0=$(date +%s)
  migrate_to "$REL_NEW" "$TARGET_HEAD" "$dsn" > "$ST/R2-upgrade.out" 2>&1 && ok "alembic upgrade 0029 -> $TARGET_HEAD on scratch ($(( $(date +%s) - t0 )) s)" \
    || { bad "alembic upgrade on scratch (see R2-upgrade.out)"; return 1; }
  QF "$PKG/sql/prestate.sql" "$SOCK" "$PORT" > "$ST/R2-prestate-after.txt"
  diff "$ST/R2-prestate-before.txt" "$ST/R2-prestate-after.txt" >/dev/null && ok "pre-state identical after the upgrade (V2 totals checksum equal)" \
    || { bad "pre-state changed by the migrations (ABSOLUTE NO-GO)"; diff "$ST/R2-prestate-before.txt" "$ST/R2-prestate-after.txt" | sed 's/^/      /'; }
  verify_sql "$SOCK" "$PORT" > "$ST/R2-verify.txt" || bad "verification SQL errored"
  [ "$(verify_fixed < "$ST/R2-verify.txt")" = "$VERIFY_EXPECTED" ] && ok "V1, V3-V10 exactly as expected" \
    || { bad "verification SQL differs:"; diff <(echo "$VERIFY_EXPECTED") <(verify_fixed < "$ST/R2-verify.txt") | sed 's/^/      /'; }
  grep -E '^V11 ' "$ST/R2-verify.txt" | sed 's/^/INFO  recorded: /'
  ( cd "$REL_NEW/backend" && env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 "$REL_NEW/venv/bin/python" -B -E -s -c '
import asyncio, sys
from contextlib import asynccontextmanager
import psycopg
from app.payments import switch
@asynccontextmanager
async def connect():
    async with await psycopg.AsyncConnection.connect(sys.argv[1]) as c:
        yield c
asyncio.run(switch.require_hardening(connect)); print("precondition passes")' \
      "host=$SOCK port=$PORT dbname=$DEV_DB user=hyperchat_app" ) 2>&1 | grep -qx 'precondition passes' \
    && ok "the 6a-4 startup precondition passes at $TARGET_HEAD as hyperchat_app" || bad "the 6a-4 precondition fails on the upgraded copy"
  ( cd "$REL_NEW/backend" && env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_READONLY_DSN="host=$SOCK port=$PORT dbname=$DEV_DB user=hyperchat_readonly" \
      "$REL_NEW/venv/bin/python" -B -E -s -m app.payments.operator verify --max-seconds 300 > "$ST/R2-verifier.json" 2> "$ST/R2-verifier.err" ); rcc=$?
  echo "INFO  read-only verifier exit $rcc (pre-A5c: findings expected; recorded for the owner, decision OD-6b-3): $(python3 -I -B -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("status"), "findings", len(d.get("findings", [])), "gates", d.get("gates"))' "$ST/R2-verifier.json" 2>&1 | head -1)"
  [ "$rcc" -le 3 ] && ok "verifier produced a report (exit $rcc)" || bad "verifier crashed (exit $rcc)"
  migrate_to "$REL_NEW" "$PRE_HEAD" "$dsn" downgrade > "$ST/R2-downgrade.out" 2>&1; rcc=$?
  echo "INFO  downgrade to $PRE_HEAD: exit $rcc, head now $(S -d "$DEV_DB" -Atc 'SELECT string_agg(version_num, $$,$$) FROM alembic_version')"
  [ "$rcc" != 0 ] && grep -Eq 'downgrade of 003[0-4] stopped' "$ST/R2-downgrade.out" && ok "downgrade to $PRE_HEAD refused (0031 audit rows exist): rollback is the backup restore (X)" \
    || bad "downgrade did not refuse as designed (see R2-downgrade.out)"
  started=0
  echo "== R3 live DEV untouched"
  same prestate-live-R-before.txt prestate && head_is "$PRE_HEAD" && [ "$(readlink -f "$APP_CURRENT")" = "$(facts OLD_REL)" ] \
    && ok "live DEV pre-state, head and release unchanged" || bad "live DEV changed during R"
  cp "$ST/R2-prestate-before.txt" "$ST/R-prestate.txt"; grep -E '^V11 ' "$ST/R2-verify.txt" > "$ST/R-recorded.txt"
  echo "== $nok OK, $nbad FAIL"
  [ "$nbad" = 0 ] && echo "$set" > "$ST/R-passed" && echo "R OK: 0029 -> $TARGET_HEAD rehearsed on $set; scratch clusters removed"
  [ "$nbad" = 0 ]; }
r_rehearsal
