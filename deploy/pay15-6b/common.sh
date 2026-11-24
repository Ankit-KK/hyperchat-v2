# PAY-15 stage 6b: a DEV release + migrations 0030-0035, switch OFF. Shared definitions, sourced by run_gate.sh before
# every gate. Defining them changes nothing. The release being deployed is NOT pinned here (this file lives in the
# repository): the sealed packet's release.env, next to this file and covered by its manifest, names it.
# Gates, each with its own owner approval, in order:
#   G0 read-only pre-checks (writes only $ST)       B  build the release directory (inert; nothing switched)
#   R  rehearsal on scratch copies (no live change)  W  the DEV window (stop, backup, migrate 0035, switch, start)
#   S  post-window checks (smoke, reports, L4)       X  rollback (restore the W backup by database rename-swap)
# Scope: ONLY the V2 DEV environment (cluster 15/restoretest, database hyperchat_dev). host_ok / target_ok refuse any
# other host or cluster. Production is never read, connected to or named by a command here.
# Helpers marked (C1) are the executed C1 package's (hyperchat-deploy-0963113 r3, gates A-D passed on DEV).
[ "$(id -u)" = 0 ] || { echo "STOP: run as root"; exit 1; }
export PYTHONDONTWRITEBYTECODE=1
B6_PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# release.env (packet only): NEW_ID COMMIT TAR_SHA NEW_FP NEW_FILES SMOKE_SHA LOCK_NAME LOCK_SHA PINS, and the DEV data
# facts EXPECTED_FINDINGS (verifier keys invariant|class|entity_kind|entity_id, one per line) and KNOWN_ORPHANS (ids of
# deleted creators whose append-only credit history remains, e.g. from earlier smoke runs; may be empty) (FU-1, FU-2)
[ -f "$B6_PKG/release.env" ] && . "$B6_PKG/release.env" || { echo "STOP: $B6_PKG/release.env is missing"; exit 1; }
[[ "${NEW_ID:-}" =~ ^[0-9a-f]{12}$ && "${COMMIT:-}" =~ ^[0-9a-f]{40}$ && "${COMMIT:0:12}" = "$NEW_ID" \
   && "${TAR_SHA:-}" =~ ^[0-9a-f]{64}$ && "${NEW_FP:-}" =~ ^[0-9a-f]{64}$ && "${NEW_FILES:-}" =~ ^[0-9]+$ \
   && "${SMOKE_SHA:-}" =~ ^[0-9a-f]{64}$ && "${LOCK_SHA:-}" =~ ^[0-9a-f]{64}$ && "${LOCK_NAME:-}" =~ ^[A-Za-z0-9._-]+$ \
   && -n "${PINS:-}" && -n "${EXPECTED_FINDINGS:-}" && "${KNOWN_ORPHANS:-}" =~ ^([0-9a-f-]{36}( |$))*$ ]] \
  || { echo "STOP: release.env is incomplete or malformed"; exit 1; }
if [ "${B6_TEST:-}" != 1 ]; then   # the server: always the real facts and paths (overrides are for mock tests only)
  # DEV identity (C1, read-only facts of 2026-10-30; G0 re-checks them and stops if they changed)
  DEV_IDENTITY="hyperchat_dev 5433 15/restoretest 7692889322508027580 /var/lib/postgresql/15/restoretest"
  HOST_ID="hal-server-871241 f1f19d658eac2c2d"; ROLES_SHA=fbb277d57be9eea9
  RELEASES=/opt/hyperchat/releases; REL_NEW=$RELEASES/$NEW_ID; APP_CURRENT=/opt/hyperchat/current
  ST=/root/deploy-dev-6b-$NEW_ID; API_ENV=/etc/hyperchat/api.env
  BACKUP_DIR=/var/backups/hyperchat-dev; BACKUP_UNIT=hyperchat-dev-pgbackup.service
  ROLES=/root/hyperchat-roles-8ec2e02/roles.sql; PGBIN=/usr/lib/postgresql/15/bin; MACHINE_ID_FILE=/etc/machine-id
  AUDIT=/root/hyperchat-c2fe883/deploy/check_isolation.sh; SCRATCH_ROOT=/var/tmp   # /tmp is a 1.9 GiB tmpfs (S0-2b)
fi
PKG="$ST/pkg"; TAR="$PKG/rel-$NEW_ID.tar"; SMOKE_TAR="$PKG/smoke-$NEW_ID.tar"; LOCK="$PKG/$LOCK_NAME"
DEV_DB=hyperchat_dev; DEV_PORT=5433; DEV_SOCK=/var/run/postgresql
MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@/$DEV_DB?host=$DEV_SOCK&port=$DEV_PORT"
DEV_UNITS="hyperchat-api hyperchat-worker"; DEV_TIMERS="hyperchat-cleanup.timer"   # never a shared or production unit
TARGET_HEAD=0035; PRE_HEAD=0029
VERIFY_EXPECTED="V1 head 0035
V3 buckets credited beyond their total 0
V4 donations outside their credit bounds 0
V5 donations whose history differs from credited 0
V6 credit differs from the no-reversal target 0
V7 reversals 0 non-backfill/writer history 0 state off/hold events 0 runs 0 evaluations 0
V8 triggers donation_accounting_evaluations_ack_guard donation_credit_events_guard donations_credit_delete_guard donations_credit_delete_guard_truncate donations_credit_writer donations_protect_history_columns payment_accounting_state_guard payment_integrity_events_guard payment_webhook_events_guard
V8 privileges false false false
V9 refund snapshots 0
V10 0035 hardening true true 2"
# V11 (backfill rows and sum) is RECORDED by R and must be equal in W when the pre-state equals R's. V6 is exact: the
# 0031 backfill sets creditable_inr to the capped credit, so a short row's target equals its credit
# (database/tests/test_pay15_6b_packet.py proves every line on a fresh 0001 -> 0029 -> 0035 database).

fp() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64); }   # (C1)
tree_fp() { (cd "$1" && find . -type f ! -path './venv/*' ! -path './backend/RELEASE' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64); }
# read-only psql on live DEV (C1)
# both run from / : postgres cannot enter root's working directory ("could not change directory", gate R 2026-11-24)
Q() { ( cd / && sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -v ON_ERROR_STOP=1 -h "$DEV_SOCK" -p "$DEV_PORT" -d "$DEV_DB" -Atc "$1" ); }
QF() { ( cd / && sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -q -v ON_ERROR_STOP=1 -h "${2:-$DEV_SOCK}" -p "${3:-$DEV_PORT}" -d "${4:-$DEV_DB}" -At < "$1" ); }   # stdin: postgres cannot read the root-only $PKG
head_now() { Q 'SELECT string_agg(version_num, $$,$$) FROM alembic_version'; }
head_is() { local now; now="$(head_now)" || { echo "STOP: cannot read alembic_version"; return 1; }
            [ "$now" = "$1" ] || { echo "STOP: database at '$now', expected $1"; return 1; }; }
target_ok() {  # (C1) Q reaches EXACTLY live DEV: database, port, cluster, system identifier, data directory
  [ "$DEV_DB" = hyperchat_dev ] && [ "$DEV_PORT" = 5433 ] && [ "$DEV_SOCK" = /var/run/postgresql ] \
    && [ "$MIGRATION_DSN" = "postgresql+psycopg://hyperchat_migrator@/hyperchat_dev?host=/var/run/postgresql&port=5433" ] \
    && [ "$(Q "SELECT current_database() || ' ' || current_setting('port') || ' ' || current_setting('cluster_name') || ' ' || (SELECT system_identifier FROM pg_control_system()) || ' ' || current_setting('data_directory')")" = "$DEV_IDENTITY" ] \
    || { echo "STOP: the database target is not live DEV ($DEV_IDENTITY)"; return 1; }; }
host_ok() { [ "$(hostname) $(sha256sum "$MACHINE_ID_FILE" 2>/dev/null | cut -c1-16)" = "$HOST_ID" ] \
  || { echo "STOP: this is not the V2 DEV host ($HOST_ID)"; return 1; }; }   # (C1)
pins_ok() { local rel=$1 sha path
  while read -r sha path; do [ "$(sha256sum "$rel/$path" 2>/dev/null | cut -c1-64)" = "$sha" ] || { echo "STOP: $path is not the $NEW_ID file"; return 1; }; done <<< "$PINS"; }
api_healthy() { local i out   # (C1) $1 = the release id /health must report
  for i in $(seq 1 24); do
    out="$(curl -fsS --max-time 5 http://127.0.0.1:8000/health 2>/dev/null)" \
      && printf '%s' "$out" | python3 -I -B -c 'import json, sys; d = json.load(sys.stdin); sys.exit(0 if d.get("status") == "ok" and d.get("release") == sys.argv[1] and (d.get("database") or {}).get("status") == "ok" else 1)' "$1" \
      && { echo "API healthy on release $1"; return 0; }
    sleep 5
  done
  echo "STOP: API not healthy on release $1 within 2 minutes"; return 1; }
proc_cwd() { readlink "/proc/$(systemctl show -p MainPID --value "$1")/cwd" 2>/dev/null; }   # $1 = a DEV unit
api_cwd() { proc_cwd hyperchat-api; }
file_mode() { stat -c '%U:%G %a' "$1"; }
unit_line() { echo "$1 $(systemctl is-active "$1") pid $(systemctl show -p MainPID --value "$1") restarts $(systemctl show -p NRestarts --value "$1")"; }
link_to() {  # (C1) atomic: a new symlink, then rename(2) over current
  rm -f "$APP_CURRENT.next" && ln -s "$1" "$APP_CURRENT.next" && mv -T "$APP_CURRENT.next" "$APP_CURRENT" \
    && [ "$(readlink -f "$APP_CURRENT")" = "$1" ]; }
env_sha() { sha256sum "$API_ENV" | cut -c1-64; }
env_switch_off() {  # the accounting setting must stay unset or false in api.env (names only are read)
  ! grep -Eiq '^[[:space:]]*HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED[[:space:]]*=[[:space:]]*["'"'"']?(1|true|yes|on)' "$API_ENV" \
    || { echo "STOP: api.env enables HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED (6b keeps the switch OFF)"; return 1; }; }
prestate() { QF "$PKG/sql/prestate.sql"; }
prestate_core() { prestate | grep -v "^jobs "; }   # once services run, jobs may move; money and records may not
verify_sql() { QF "$PKG/sql/verify_0035.sql" "$@"; }
verify_fixed() { grep -Ev '^V11 ' ; }   # the exactly-expected lines (V11 is recorded)
same() { diff -q "$ST/$1" <($2) >/dev/null || { echo "STOP: $2 differs from $1:"; diff "$ST/$1" <($2) | sed 's/^/      /'; return 1; }; }
orphan_list() {  # deleted creators with credit history left: streamer_id|first_epoch|last_epoch (FU-1 A)
  Q "SELECT e.streamer_id, extract(epoch FROM min(e.created_at))::bigint, extract(epoch FROM max(e.created_at))::bigint
     FROM public.donation_credit_events e WHERE NOT EXISTS (SELECT 1 FROM public.streamers s WHERE s.id = e.streamer_id)
     GROUP BY 1 ORDER BY 1"; }
findings_ok() {  # $1 = verifier JSON, then findings_check.py options: the findings must be EXACTLY the expected set (FU-2)
  printf '%s\n' "$EXPECTED_FINDINGS" > "$ST/expected-findings.txt"
  python3 -I -B "$PKG/findings_check.py" "$1" "$ST/expected-findings.txt" --known-orphans "${KNOWN_ORPHANS:-}" "${@:2}"; }
facts() { grep -m1 "^$1=" "$ST/G0-facts.env" | cut -d= -f2-; }   # a fact recorded by G0 (OLD_ID, ENV_SHA, ...)
window_ok() {  # $1 = minutes the step needs. Never inside the DEV backup window or near the S0-6 / off-site runs.
  python3 -I -B -c '
import sys, datetime as dt
need = int(sys.argv[1]); now = dt.datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.datetime.now(dt.timezone.utc)
blocked = [((1, 20), (4, 30)), ((4, 5), (4, 35)), ((5, 15), (5, 50)), ((16, 0), (16, 35))]
def bad(t):
    m = t.hour * 60 + t.minute
    return any(a[0] * 60 + a[1] <= m <= b[0] * 60 + b[1] for a, b in blocked)
sys.exit(1 if any(bad(now + dt.timedelta(minutes=k)) for k in range(0, need + 1)) else 0)' "$1" ${B6_NOW:+"$B6_NOW"} \
    || { echo "STOP: the next $1 minutes overlap a blocked time (UTC 01:20-04:30 backups, 04:15 S0-6, 05:30 off-site, 16:15 S0-6)"; return 1; }; }
audit_summary() { local out rc   # (C1) the DEV isolation audit, release names normalized
  out="$(bash "$AUDIT" dev 2>&1)"; rc=$?
  printf '%s\n' "$out" | sed -E "s#releases/[0-9a-f]{12}#releases/<REL>#g"; return $rc; }
audit_ok() { local f=$1; audit_summary > "$f" || { echo "STOP: the DEV isolation audit did not exit 0"; return 1; }
  [ "$(grep -c '^FAIL' "$f")" = 0 ] && [ "$(grep -c '^OK' "$f")" -ge 23 ] || { echo "STOP: the isolation audit is not 23/23 OK"; return 1; }; }
latest_set() { ls -1 "$BACKUP_DIR"/*.manifest.json 2>/dev/null | LC_ALL=C sort | tail -1 | sed 's#.*/##; s#\.manifest\.json$##'; }
set_ok() {  # $1 = set base: the dump's SHA-256 and every manifest-listed file's SHA-256 (C1 A2, without stdin python)
  ( cd "$BACKUP_DIR" && sha256sum -c --quiet "$1.dump.sha256" ) || { echo "STOP: $1 dump SHA-256 mismatch"; return 1; }
  python3 -I -B -c '
import hashlib, json, os, sys
d, base = sys.argv[1:]
m = json.load(open(os.path.join(d, base + ".manifest.json")))
files = [v for v in m.values() if isinstance(v, dict) and "sha256" in v and "file" in v]
ok = bool(files) and all(hashlib.sha256(open(os.path.join(d, f["file"]), "rb").read()).hexdigest() == f["sha256"] for f in files)
sys.exit(0 if ok else 1)' "$BACKUP_DIR" "$1" || { echo "STOP: $1 manifest incomplete or a listed file differs"; return 1; }; }
migrate_to() {  # $1 = release dir, $2 = revision, $3 = DSN. Exactly that revision, never 'head' (C1 run_migration)
  ( cd "$1/database" && env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONDONTWRITEBYTECODE=1 HYPERCHAT_MIGRATION_DSN="$3" \
      "$1/venv/bin/python" -B -E -s -m alembic "${4:-upgrade}" "$2" ); }
