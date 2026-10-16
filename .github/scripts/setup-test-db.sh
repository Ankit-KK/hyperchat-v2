#!/usr/bin/env bash
# Prepare a THROWAWAY PostgreSQL server for the test suites, the same way
# deploy/postgres/setup_cluster.sh prepares a real cluster: HyperChat roles + one database owned by
# hyperchat_owner, CONNECT only for the HyperChat roles. Then migrate it to head.
#
#   HYPERCHAT_CI_DB_SETUP=1 PGHOST=localhost PGPORT=5432 PGUSER=postgres \
#     .github/scripts/setup-test-db.sh <database> <python-with-alembic>
#
# Used by CI (PostgreSQL service container, trust auth) and for CI-equivalent runs on a temporary
# local cluster. Refuses to run without HYPERCHAT_CI_DB_SETUP=1 and refuses real database names.
set -euo pipefail

DB="${1:?database name}"
PY="${2:-python}"
[ "${HYPERCHAT_CI_DB_SETUP:-}" = "1" ] || { echo "refusing: set HYPERCHAT_CI_DB_SETUP=1 (throwaway servers only)" >&2; exit 2; }
case "$DB" in hyperchat|hyperchat_dev|hyperchat_restore_test) echo "refusing real database $DB" >&2; exit 2;; esac

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PSQL=(psql -X -q -v ON_ERROR_STOP=1 -d postgres)

"${PSQL[@]}" -f "$ROOT/deploy/postgres/roles.sql" >/dev/null
"${PSQL[@]}" -c "DROP DATABASE IF EXISTS \"$DB\""
"${PSQL[@]}" -c "CREATE DATABASE \"$DB\" OWNER hyperchat_owner"
"${PSQL[@]}" -c "REVOKE ALL ON DATABASE \"$DB\" FROM PUBLIC"
"${PSQL[@]}" -c "GRANT CONNECT ON DATABASE \"$DB\" TO hyperchat_migrator, hyperchat_app, hyperchat_worker, hyperchat_readonly, hyperchat_lifecycle, hyperchat_auth"

cd "$ROOT/database"
HYPERCHAT_MIGRATION_DSN="postgresql+psycopg://hyperchat_migrator@${PGHOST}:${PGPORT:-5432}/${DB}" \
  "$PY" -m alembic upgrade head
echo "test database $DB ready on ${PGHOST}:${PGPORT:-5432}"
