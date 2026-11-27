#!/usr/bin/env bash
# Close the runtime roles' reach into a non-HyperChat database on a cluster (security audit run-4 hardening,
# docs/operations/backend-readiness-plan.md R0-R4). The HyperChat pg_hba rule matches database "all", so its
# roles can connect to every database that grants CONNECT to PUBLIC; on DEV that includes hyperchat_restore_test,
# the read-only restore of the legacy production dump (pg_restore / createdb give it the default PUBLIC grants).
#
#   narrow_database_acl.sh <port> <database>              R0, read-only: the ACL, who may connect, sessions, role hyperchat
#   narrow_database_acl.sh <port> <database> --apply-r2   [APPROVAL] REVOKE CONNECT, TEMPORARY ... FROM PUBLIC (one transaction,
#                                                         committed only if no runtime role may still connect)
#   narrow_database_acl.sh <port> <database> --apply-r4   [APPROVAL] ALTER ROLE hyperchat NOLOGIN (the legacy peer path)
#
# Run as root on the server, outside the blocked windows, only on the owner's typed yes for each --apply step.
# Undo: R2  GRANT CONNECT, TEMPORARY ON DATABASE <database> TO PUBLIC;   R4  ALTER ROLE hyperchat LOGIN;
# Never for a HyperChat database (hyperchat, hyperchat_dev): setup_cluster.sh already lists its roles by name.
set -euo pipefail

PORT="${1:-}"; DB="${2:-}"; MODE="${3:-check}"
RUNTIME="hyperchat_app hyperchat_worker hyperchat_auth hyperchat_lifecycle hyperchat_readonly hyperchat_stepup"
usage() { echo "usage: $0 <port> <database> [--apply-r2|--apply-r4]" >&2; exit 2; }
[[ "$PORT" =~ ^[0-9]{4,5}$ && "$DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || usage
case "$MODE" in check | --apply-r2 | --apply-r4) ;; *) usage ;; esac
case "$DB" in hyperchat | hyperchat_dev | postgres | template0 | template1)
  echo "STOP: $DB is not a database this script narrows" >&2; exit 2 ;; esac
[ "$(id -u)" = 0 ] || { echo "STOP: run as root" >&2; exit 2; }
PSQL=(sudo -u postgres psql -X -q -At -v ON_ERROR_STOP=1 -h /var/run/postgresql -p "$PORT")
q() { (cd / && "${PSQL[@]}" -d postgres -c "$1"); }
roles_sql() { printf "'%s'," $RUNTIME | sed 's/,$//'; }

[ "$(q "SELECT count(*) FROM pg_database WHERE datname = '$DB'")" = 1 ] || { echo "STOP: no database $DB on :$PORT" >&2; exit 2; }
reach() {  # runtime roles that may still CONNECT to $DB
  q "SELECT string_agg(rolname, ' ' ORDER BY rolname) FROM pg_roles WHERE rolname IN ($(roles_sql))
       AND has_database_privilege(rolname, '$DB', 'CONNECT')"
}

echo "== R0 $DB on :$PORT ($(date -u +%FT%TZ))"
echo "acl: $(q "SELECT coalesce(datacl::text, '(default: CONNECT and TEMPORARY to PUBLIC)') FROM pg_database WHERE datname = '$DB'")"
echo "runtime roles that may connect: $(reach)"
echo "sessions on it now: $(q "SELECT count(*) FROM pg_stat_activity WHERE datname = '$DB' AND pid <> pg_backend_pid()")"
echo "role hyperchat: $(q "SELECT CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END FROM pg_roles WHERE rolname = 'hyperchat'" || true)"

case "$MODE" in
  check) echo "R0 done (read-only, nothing changed)" ;;
  --apply-r2)
    # One transaction: revoke, then refuse to commit while any runtime role can still connect.
    (cd / && "${PSQL[@]}" -d postgres) <<SQL
BEGIN;
REVOKE CONNECT, TEMPORARY ON DATABASE "$DB" FROM PUBLIC;
DO \$\$
DECLARE r text;
BEGIN
    SELECT string_agg(rolname, ' ') INTO r FROM pg_roles WHERE rolname IN ($(roles_sql))
      AND has_database_privilege(rolname, '$DB', 'CONNECT');
    IF r IS NOT NULL THEN
        RAISE EXCEPTION 'runtime roles can still connect to $DB: % (rolled back)', r;
    END IF;
END \$\$;
COMMIT;
SQL
    echo "R2 applied: acl now $(q "SELECT datacl::text FROM pg_database WHERE datname = '$DB'"); runtime reach: '$(reach)'"
    echo "undo: GRANT CONNECT, TEMPORARY ON DATABASE \"$DB\" TO PUBLIC;" ;;
  --apply-r4)
    q "ALTER ROLE hyperchat NOLOGIN" >/dev/null
    echo "R4 applied: role hyperchat is $(q "SELECT CASE WHEN rolcanlogin THEN 'LOGIN' ELSE 'NOLOGIN' END FROM pg_roles WHERE rolname = 'hyperchat'")"
    echo "undo: ALTER ROLE hyperchat LOGIN;" ;;
esac
