#!/usr/bin/env bash
# PAY-15 6a-2 / OD-14: the CI identity capture (I-1 to I-4) that backend/tests/payments/test_verify_db.py compares
# the live server against before any verifier DB test runs. Run once per job, after setup-test-db.sh and right
# before the backend pytest step:
#
#   HYPERCHAT_CI_DB_SETUP=1 PGHOST=localhost PGPORT=5432 PGUSER=postgres .github/scripts/ci-identity-capture.sh <15|17>
#
# It marks hyperchat_ci with a fresh nonce (freshness only), records the server's own system_identifier,
# data_directory, server_version_num and port to a JSON file, and exports the CI-mode variables to $GITHUB_ENV.
# OD-14 threat model: the container is disposable; this detects MISROUTING to a non-CI server. It is not a boundary
# against repository-controlled test code.
set -euo pipefail

MAJOR="${1:?PostgreSQL major (15 or 17)}"
[ "${HYPERCHAT_CI_DB_SETUP:-}" = "1" ] || { echo "refusing: set HYPERCHAT_CI_DB_SETUP=1 (throwaway servers only)" >&2; exit 2; }
case "$MAJOR" in 15|17) ;; *) echo "refusing: major must be 15 or 17" >&2; exit 2;; esac
[ "${PGHOST:-}" = "localhost" ] && [ "${PGPORT:-}" = "5432" ] || { echo "refusing: CI shape is localhost:5432 only" >&2; exit 2; }

PSQL=(psql -X -q -v ON_ERROR_STOP=1 -d postgres)
NONCE="$(python -c 'import secrets; print(secrets.token_hex(16))')"
OUT="${RUNNER_TEMP:-/tmp}/hyperchat-ci-identity.json"

"${PSQL[@]}" -c "COMMENT ON DATABASE hyperchat_ci IS 'hyperchat-ci-disposable:${NONCE}'"
IFS='|' read -r SYSID DATADIR VNUM PORT < <("${PSQL[@]}" -tAF'|' -c \
  "SELECT (SELECT system_identifier::text FROM pg_catalog.pg_control_system()), current_setting('data_directory'),
          current_setting('server_version_num'), inet_server_port()::text")
[ "$PORT" = "5432" ] || { echo "refusing: server port $PORT is not 5432" >&2; exit 2; }
[ "${VNUM:0:2}" = "$MAJOR" ] || { echo "refusing: server version $VNUM is not PostgreSQL $MAJOR" >&2; exit 2; }

SYSID="$SYSID" DATADIR="$DATADIR" VNUM="$VNUM" PORT="$PORT" NONCE="$NONCE" OUT="$OUT" python -c '
import json, os
keys = ("sysid", "datadir", "version_num", "port", "nonce")
data = {"source": "hyperchat-ci-provisioning-v1", **{k: os.environ[e] for k, e in zip(keys, ("SYSID", "DATADIR", "VNUM", "PORT", "NONCE"))}}
with open(os.environ["OUT"], "w", encoding="utf-8") as f:
    json.dump(data, f)
'
{
  echo "HYPERCHAT_TEST_CI_IDENTITY=1"
  echo "HYPERCHAT_TEST_CI_IDENTITY_FILE=$OUT"
  echo "HYPERCHAT_TEST_CI_PG_MAJOR=$MAJOR"
  echo "HYPERCHAT_TEST_CI_MARKER=$NONCE"
} >> "${GITHUB_ENV:-/dev/stdout}"
echo "identity captured: PostgreSQL $VNUM, port $PORT, sysid $SYSID ($OUT)"
