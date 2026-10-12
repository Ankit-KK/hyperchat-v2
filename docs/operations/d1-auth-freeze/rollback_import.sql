-- D1 password-hash import: undo ONE import run on the V2 target. Only after import_status.sql has been reviewed and the
-- README says to roll back (fingerprint mismatch after exit 4, or an approved abort after --apply). Run as the
-- importer's target role (hyperchat_app) while V2 sign-in is OFF and legacy is still frozen:
--   psql -X -v ON_ERROR_STOP=1 -v run_id=<import_run_id> -f rollback_import.sql
-- It clears password_hash ONLY where it is still exactly the hash this run wrote (the ledger's SHA-256). Accounts whose
-- hash changed since (Argon2id upgrade at sign-in, reset, change) are left as they are and must be decided one by one
-- (import_status.sql lists them). It never clears a hash by its format, and never one another run or V2 wrote.
-- The ledger rows stay as history. Running it twice changes nothing the second time.
BEGIN;
SET LOCAL lock_timeout = '5s';

UPDATE public.users u
SET password_hash = NULL
FROM public.d1_hash_import_ledger l
WHERE l.run_id = :'run_id'
  AND u.id = l.user_id
  AND u.password_hash IS NOT NULL
  AND encode(sha256(convert_to(u.password_hash, 'UTF8')), 'hex') = l.hash_sha256;

COMMIT;
