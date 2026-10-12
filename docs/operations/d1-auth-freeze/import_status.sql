-- D1 password-hash import: status of ONE import run on the V2 target (read-only). Run against the V2 database as the
-- operator, with the run id the importer printed (import_run_id):
--   psql -X -v ON_ERROR_STOP=1 -v run_id=<import_run_id> -f import_status.sql
-- A run is one transaction: its ledger rows exist if and only if its hashes were committed.
--   imported              rows recorded by the run: 0 = it did NOT commit; the planned count = it committed
--   still_imported_hash   accounts whose password_hash is still exactly what the run wrote
--   cleared               accounts whose password_hash is empty now (e.g. after rollback_import.sql)
--   changed_since_import  accounts whose password_hash changed after the run: an Argon2id upgrade at sign-in (which
--                         does not set password_changed_at), a reset or a change. rollback_import.sql never touches
--                         them; each one needs a decision (README, "Accounts changed after the import").
--   source_fingerprints   the reviewed source fingerprint(s) the run was applied against (exactly one)
SELECT count(*) AS imported,
       count(*) FILTER (WHERE u.password_hash IS NOT NULL
                          AND encode(sha256(convert_to(u.password_hash, 'UTF8')), 'hex') = l.hash_sha256)
           AS still_imported_hash,
       count(*) FILTER (WHERE coalesce(u.password_hash, '') = '') AS cleared,
       count(*) FILTER (WHERE coalesce(u.password_hash, '') <> ''
                          AND encode(sha256(convert_to(u.password_hash, 'UTF8')), 'hex') <> l.hash_sha256)
           AS changed_since_import,
       count(DISTINCT l.source_fingerprint) AS source_fingerprints,
       min(l.imported_at) AS imported_at
FROM public.d1_hash_import_ledger l
JOIN public.users u ON u.id = l.user_id
WHERE l.run_id = :'run_id';

-- The accounts behind changed_since_import (V2 user ids only; no hash or email is shown).
SELECT l.user_id AS changed_since_import_user_id, u.password_changed_at, u.last_login_at
FROM public.d1_hash_import_ledger l
JOIN public.users u ON u.id = l.user_id
WHERE l.run_id = :'run_id'
  AND coalesce(u.password_hash, '') <> ''
  AND encode(sha256(convert_to(u.password_hash, 'UTF8')), 'hex') <> l.hash_sha256
ORDER BY l.user_id;
