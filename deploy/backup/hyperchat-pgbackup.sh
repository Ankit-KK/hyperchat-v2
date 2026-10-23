#!/usr/bin/env bash
# Nightly logical backup of one HyperChat database (docs/backup-recovery.md).
#
#   HYPERCHAT_BACKUP_ENV=prod (default)  database hyperchat      cluster 17/hyperchat    -> /var/backups/hyperchat
#   HYPERCHAT_BACKUP_ENV=dev             database hyperchat_dev  cluster 15/restoretest  -> /var/backups/hyperchat-dev
#   HYPERCHAT_BACKUP_ENV=test            disposable test databases only (hc_backuptest_*), never under /var/backups
#
# One run writes one backup SET, all or nothing:
#   <db>_<UTC>.dump            pg_dump -Fc with the source cluster's own major version (checked against the server)
#   <db>_<UTC>.dump.sha256     "sha256  <file>" (sha256sum -c format, as before)
#   <db>_<UTC>.witness         a copy of the environment's KYC restore witness (when configured; never restored blindly)
#   <db>_<UTC>.manifest.json   written LAST: the set is complete only when its manifest exists and its hashes match
# Everything is built in a private .partial-* directory, validated (pg_restore -l, sha256), fsynced and renamed into
# place; a failure leaves no set behind. flock refuses an overlapping run (exit 75). Retention removes only complete
# sets of THIS database older than KEEP_DAYS and always keeps the newest KEEP_MIN complete sets (never fewer than 1).
# Logs carry names, sizes, counts and hashes only. Exit: 0 ok; 2 refused configuration; 3 set written but the KYC
# witness is missing or unreadable (the unit fails visibly); 75 another backup is running; other: the backup failed.
set -euo pipefail
umask 077
export LC_ALL=C

die() { echo "backup refused: $*" >&2; exit 2; }
fail() { echo "backup FAILED: $*" >&2; exit 1; }
ENV_NAME="${HYPERCHAT_BACKUP_ENV:-prod}"
case "$ENV_NAME" in
  prod) P_DB=hyperchat;     P_PORT=5432; P_CLUSTER=17/hyperchat;   P_DIR=/var/backups/hyperchat
        P_WITNESS=/var/lib/hyperchat-kyc/prod.witness ;;
  dev)  P_DB=hyperchat_dev; P_PORT=5433; P_CLUSTER=15/restoretest; P_DIR=/var/backups/hyperchat-dev
        P_WITNESS=/var/lib/hyperchat-kyc/dev.witness ;;
  test) P_DB=""; P_PORT=""; P_CLUSTER=""; P_DIR=""; P_WITNESS="" ;;
  *) die "HYPERCHAT_BACKUP_ENV must be prod, dev or test" ;;
esac
DB="${HYPERCHAT_BACKUP_DB:-$P_DB}"
PORT="${HYPERCHAT_BACKUP_PORT:-$P_PORT}"
CLUSTER="${HYPERCHAT_BACKUP_CLUSTER:-$P_CLUSTER}"
DIR="${HYPERCHAT_BACKUP_DIR:-$P_DIR}"
WITNESS="${HYPERCHAT_BACKUP_WITNESS-$P_WITNESS}"   # set to "" to back up without a witness (test only)
KEEP_DAYS="${HYPERCHAT_BACKUP_KEEP_DAYS:-14}"
KEEP_MIN="${HYPERCHAT_BACKUP_KEEP_MIN:-3}"
HOST="${HYPERCHAT_BACKUP_HOST:-/var/run/postgresql}"
RUN_AS="${HYPERCHAT_BACKUP_RUN_AS-postgres}"
OWNER="${HYPERCHAT_BACKUP_OWNER:-root}"
MAJOR="${CLUSTER%%/*}"
BIN="${HYPERCHAT_BACKUP_PG_BIN:-/usr/lib/postgresql/$MAJOR/bin}"

# ── fail closed on anything ambiguous ─────────────────────────────────────────────────────────────────────
[[ "$DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || die "invalid database name"
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || die "invalid port"
[[ "$CLUSTER" =~ ^[0-9]{2}/[a-z0-9_-]+$ ]] || die "invalid cluster (want <major>/<name>)"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] && (( KEEP_DAYS >= 1 )) || die "KEEP_DAYS must be >= 1"
[[ "$KEEP_MIN" =~ ^[0-9]+$ ]] && (( KEEP_MIN >= 1 )) || die "KEEP_MIN must be >= 1"
[[ "$DIR" == /* && "$DIR" != / && "$DIR" != *"/./"* && "$DIR" != *"/../"* && "$DIR" != *"/.." && "$DIR" != *"//"* ]] \
  || die "backup directory must be an absolute, normalised path"
[[ -z "$WITNESS" || "$WITNESS" == /* ]] || die "witness path must be absolute"
case "$ENV_NAME" in  # an environment backs up its own database into its own directory, nothing else
  prod) [[ "$DB" == hyperchat && "$PORT" == 5432 && "$CLUSTER" == 17/hyperchat && "$DIR" == /var/backups/hyperchat \
           && "$WITNESS" == /var/lib/hyperchat-kyc/prod.witness ]] || die "prod backs up hyperchat (17/hyperchat :5432) into /var/backups/hyperchat only" ;;
  dev)  [[ "$DB" == hyperchat_dev && "$PORT" == 5433 && "$CLUSTER" == 15/restoretest && "$DIR" == /var/backups/hyperchat-dev \
           && "$WITNESS" == /var/lib/hyperchat-kyc/dev.witness ]] || die "dev backs up hyperchat_dev (15/restoretest :5433) into /var/backups/hyperchat-dev only" ;;
  test) [[ "$DB" == hc_backuptest_* ]] || die "test backs up hc_backuptest_* databases only"
        [[ "$DIR" != /var/backups* && "$DIR" != /var/lib/hyperchat* ]] || die "test never writes under /var/backups"
        [[ -z "$WITNESS" || "$WITNESS" != /var/lib/hyperchat-kyc/* ]] || die "test never reads a real witness" ;;
esac
[[ "$ENV_NAME" == test || ( "$OWNER" == root && "$HOST" == /var/run/postgresql && "$RUN_AS" == postgres ) ]] \
  || die "owner, host and run-as are fixed outside test"
for tool in pg_dump pg_restore psql; do [[ -x "$BIN/$tool" ]] || die "$BIN/$tool not found"; done

run_pg() {  # the PostgreSQL tools as the cluster's own superuser (peer auth); test: as the caller
  if [[ -n "$RUN_AS" && "$(id -un)" != "$RUN_AS" ]]; then sudo -u "$RUN_AS" "$@"; else "$@"; fi
}
q() { run_pg "$BIN/psql" -X -A -t -q -v ON_ERROR_STOP=1 -h "$HOST" -p "$PORT" -d "$DB" -c "$1"; }

# The tools must be the source cluster's major version, and so must the server we are about to dump.
for tool in pg_dump pg_restore; do
  v="$("$BIN/$tool" --version | grep -oE '[0-9]+(\.[0-9]+)?' | head -1)"
  [[ "${v%%.*}" == "$MAJOR" ]] || die "$tool is version $v, the cluster is $MAJOR"
done

# ── the directory: exists or is created private, owned by OWNER, never a symlink, never group/other accessible ──
if [[ ! -e "$DIR" ]]; then
  install -d -m 700 "$DIR"
  [[ "$OWNER" == "$(id -un)" ]] || chown "$OWNER:" "$DIR"
fi
[[ -d "$DIR" && ! -L "$DIR" ]] || die "backup directory is not a plain directory"
[[ "$(readlink -f "$DIR")" == "$DIR" ]] || die "backup directory path goes through a symlink"
[[ "$(stat -c '%U %a' "$DIR")" == "$OWNER 700" ]] || die "backup directory must be $OWNER, mode 700"

exec 9>"$DIR/.lock"
flock -n 9 || { echo "backup skipped: another backup of $DB is running" >&2; exit 75; }

SERVER_NUM="$(q 'SHOW server_version_num')"
[[ "${SERVER_NUM:0:2}" == "$MAJOR" ]] || die "server is version $SERVER_NUM, expected cluster $CLUSTER"
[[ "$(q 'SELECT current_database()')" == "$DB" ]] || die "connected to the wrong database"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="${DB}_${STAMP}"
[[ ! -e "$DIR/$BASE.manifest.json" && ! -e "$DIR/$BASE.dump" ]] || die "a set named $BASE already exists"
WORK="$(mktemp -d "$DIR/.partial-$STAMP-XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

# Facts recorded BEFORE the dump starts: the dump's snapshot is at least this recent.
ALEMBIC="$(q "SELECT CASE WHEN to_regclass('public.alembic_version') IS NULL THEN '' ELSE (SELECT version_num FROM public.alembic_version) END")"
KYC="$(q "SELECT CASE WHEN to_regclass('public.kyc_enforcement') IS NULL THEN '' ELSE (SELECT state || ' ' || generation FROM public.kyc_enforcement) END")"
STEPUP="$(q "SELECT CASE WHEN to_regclass('public.stepup_enforcement') IS NULL THEN '' ELSE (SELECT state FROM public.stepup_enforcement) END")"

run_pg "$BIN/pg_dump" -h "$HOST" -p "$PORT" -Fc -Z 9 "$DB" > "$WORK/$BASE.dump" || fail "pg_dump failed"
ENTRIES="$("$BIN/pg_restore" -l "$WORK/$BASE.dump" | grep -vc '^;')" || fail "pg_restore cannot read the new archive"
(( ENTRIES > 0 )) || fail "the archive has no entries"
DUMP_SHA="$(sha256sum "$WORK/$BASE.dump" | cut -d' ' -f1)"
echo "$DUMP_SHA  $BASE.dump" > "$WORK/$BASE.dump.sha256"
DUMP_BYTES="$(stat -c %s "$WORK/$BASE.dump")"

# The KYC witness, copied AFTER the dump: it is at least as new as the dump, so a restore of this set alone is
# detected (restore_suspected) whenever enforcement changed meanwhile. Never installed automatically (docs).
WITNESS_JSON=null; WITNESS_STATUS=not_configured
if [[ -n "$WITNESS" ]]; then
  if [[ -f "$WITNESS" && ! -L "$WITNESS" ]] && cat -- "$WITNESS" > "$WORK/$BASE.witness" 2>/dev/null; then
    W_SHA="$(sha256sum "$WORK/$BASE.witness" | cut -d' ' -f1)"
    WITNESS_JSON="{\"file\": \"$BASE.witness\", \"sha256\": \"$W_SHA\", \"bytes\": $(stat -c %s "$WORK/$BASE.witness")}"
    WITNESS_STATUS=copied
  else
    rm -f -- "$WORK/$BASE.witness"; WITNESS_STATUS=missing
  fi
fi

KYC_STATE="${KYC%% *}"; KYC_GEN="${KYC##* }"
[[ -n "$KYC" ]] || { KYC_STATE=""; KYC_GEN=0; }
[[ "$KYC_GEN" =~ ^[0-9]+$ && "$ALEMBIC" =~ ^[0-9a-z_]*$ && "$KYC_STATE" =~ ^[a-z]*$ && "$STEPUP" =~ ^[a-z]*$ ]] \
  || die "unexpected database facts"
cat > "$WORK/$BASE.manifest.json" <<EOF
{"format": 1, "environment": "$ENV_NAME", "database": "$DB", "cluster": "$CLUSTER", "server_version_num": $SERVER_NUM,
 "created_at": "$STAMP", "alembic_version": "$ALEMBIC", "kyc_enforcement": {"state": "$KYC_STATE", "generation": $KYC_GEN},
 "stepup_enforcement": "$STEPUP",
 "dump": {"file": "$BASE.dump", "sha256": "$DUMP_SHA", "bytes": $DUMP_BYTES, "toc_entries": $ENTRIES},
 "witness_status": "$WITNESS_STATUS", "witness": $WITNESS_JSON}
EOF
sync -- "$WORK"/*  # on disk before any rename makes the set visible

for f in "$BASE.dump" "$BASE.dump.sha256" "$BASE.witness" "$BASE.manifest.json"; do  # the manifest last
  if [[ -e "$WORK/$f" ]]; then mv -n -- "$WORK/$f" "$DIR/$f"; fi
done
sync -- "$DIR"
[[ -f "$DIR/$BASE.manifest.json" && ! -e "$WORK/$BASE.manifest.json" ]] || fail "the set did not complete"
echo "backup ok env=$ENV_NAME db=$DB set=$BASE bytes=$DUMP_BYTES toc_entries=$ENTRIES sha256=$DUMP_SHA alembic=$ALEMBIC witness=$WITNESS_STATUS"

# ── retention: complete sets of THIS database only; the newest KEEP_MIN always stay ──────────────────────
find "$DIR" -maxdepth 1 -mindepth 1 -type d -name '.partial-*' -mmin +1440 -user "$OWNER" -exec rm -rf -- {} + 2>/dev/null || true
mapfile -t SETS < <(find "$DIR" -maxdepth 1 -type f -regextype posix-extended \
  -regex ".*/${DB}_[0-9]{8}T[0-9]{6}Z\.manifest\.json" -printf '%f\n' | sort -r)
i=0
for m in "${SETS[@]}"; do
  i=$((i + 1))
  if (( i <= KEEP_MIN )); then continue; fi
  if [[ -z "$(find "$DIR/$m" -mtime +"$KEEP_DAYS" -print)" ]]; then continue; fi
  set_base="${m%.manifest.json}"
  for suffix in .dump .dump.sha256 .witness .manifest.json; do  # the manifest last: an interrupted prune is incomplete
    if [[ -f "$DIR/$set_base$suffix" && ! -L "$DIR/$set_base$suffix" ]]; then
      rm -f -- "$DIR/$set_base$suffix"; echo "pruned $set_base$suffix"
    fi
  done
done
# Files of the old layout (before manifests): only once KEEP_MIN complete sets exist.
if (( ${#SETS[@]} >= KEEP_MIN )); then
  while read -r f; do
    b="$(basename "$f")"; b="${b%.sha256}"; b="${b%.dump}"
    if [[ ! -e "$DIR/$b.manifest.json" ]]; then rm -f -- "$f"; echo "pruned $(basename "$f")"; fi
  done < <(find "$DIR" -maxdepth 1 -type f -regextype posix-extended \
             -regex ".*/${DB}_[0-9]{8}T[0-9]{6}Z\.dump(\.sha256)?" -mtime +"$KEEP_DAYS")
fi

if [[ "$WITNESS_STATUS" == missing ]]; then
  echo "backup WARNING: the KYC witness $WITNESS is missing or unreadable" >&2
  exit 3
fi
exit 0
