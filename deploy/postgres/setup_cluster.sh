#!/usr/bin/env bash
# Create (or re-check) a HyperChat PostgreSQL cluster and its roles. Idempotent.
#
#   setup_cluster.sh <version> <cluster> <port> [databases...]
#   setup_cluster.sh 17 hyperchat 5432 hyperchat          # production cluster, empty schema
#   setup_cluster.sh 15 restoretest 5433 hyperchat_dev    # dev DB next to the restore source
#
# Never touches existing databases other than creating the listed ones (owned by hyperchat_owner).
set -euo pipefail

VER="$1"; CLUSTER="$2"; PORT="$3"; shift 3
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="/etc/postgresql/${VER}/${CLUSTER}"
PSQL=(sudo -u postgres psql -X -v ON_ERROR_STOP=1 -h /var/run/postgresql -p "$PORT")

if ! pg_lsclusters -h | awk '{print $1"/"$2}' | grep -qx "${VER}/${CLUSTER}"; then
  pg_createcluster "$VER" "$CLUSTER" --port="$PORT" --start-conf=auto -- --data-checksums
  pg_conftool "$VER" "$CLUSTER" set listen_addresses 'localhost'
fi

# pg_hba: HyperChat peer line above the generic local rule (once).
if ! grep -q "map=hyperchat" "$CONF/pg_hba.conf"; then
  LINE="$(grep -v '^#' "$HERE/pg_hba.conf" | grep -v '^\s*$')"
  awk -v line="$LINE" '!done && /^local[[:space:]]+all[[:space:]]+all/ {print line; done=1} {print}' \
      "$CONF/pg_hba.conf" > "$CONF/pg_hba.conf.new"
  install -o postgres -g postgres -m 640 "$CONF/pg_hba.conf.new" "$CONF/pg_hba.conf"
  rm -f "$CONF/pg_hba.conf.new"
fi
# pg_ident: append the map (once).
if ! grep -q "^hyperchat[[:space:]]" "$CONF/pg_ident.conf"; then
  grep -v '^#' "$HERE/pg_ident.conf" | grep -v '^\s*$' >> "$CONF/pg_ident.conf"
fi

# "start" also succeeds when the cluster is already running, so always reload afterwards
# to pick up pg_hba.conf / pg_ident.conf changes.
if ! pg_lsclusters -h | awk -v v="$VER" -v c="$CLUSTER" '$1==v && $2==c {print $4}' | grep -q online; then
  pg_ctlcluster "$VER" "$CLUSTER" start
fi
pg_ctlcluster "$VER" "$CLUSTER" reload

# Fed on stdin: the postgres OS user cannot read files in a root-only checkout.
"${PSQL[@]}" -d postgres -f - < "$HERE/roles.sql" >/dev/null

for DB in "$@"; do
  if ! "${PSQL[@]}" -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$DB'" | grep -q 1; then
    "${PSQL[@]}" -d postgres -c "CREATE DATABASE \"$DB\" OWNER hyperchat_owner"
  fi
  "${PSQL[@]}" -d postgres -c "REVOKE ALL ON DATABASE \"$DB\" FROM PUBLIC"
  "${PSQL[@]}" -d postgres -c "GRANT CONNECT ON DATABASE \"$DB\" TO hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly"
done

pg_lsclusters | awk -v v="$VER" -v c="$CLUSTER" 'NR==1 || ($1==v && $2==c)'
