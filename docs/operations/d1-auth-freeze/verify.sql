-- D1 auth freeze verification (read-only). Expected for an active freeze, exactly one row with:
--   enabled = 'O' (enabled) or 'A' (enabled always)          -- 'D' = disabled, 'R' = replica only: NOT active
--   tgtype  = 31  (row-level, BEFORE, INSERT + DELETE + UPDATE) and no column list, no WHEN condition
--   function = hyperchat_ops.d1_auth_freeze_guard, plpgsql, body_sha256 equal to the value pinned in
--              database/etl/auth_hash_import.py (FREEZE_FUNCTION_SHA256)
-- The importer runs the same check (and stops --apply on any difference); this query lets the operator look first.
SELECT t.tgname,
       t.tgenabled AS enabled,
       t.tgtype,
       t.tgattr::text AS column_list,
       t.tgqual IS NOT NULL AS has_when,
       pn.nspname || '.' || p.proname AS function,
       l.lanname AS language,
       encode(sha256(convert_to(replace(p.prosrc, chr(13), ''), 'UTF8')), 'hex') AS body_sha256
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
JOIN pg_catalog.pg_language l ON l.oid = p.prolang
WHERE n.nspname = 'auth' AND c.relname = 'users' AND t.tgname = 'hyperchat_d1_auth_freeze' AND NOT t.tgisinternal;
