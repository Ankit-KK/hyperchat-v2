# Legacy creator assets: mapping and copy plan (D-M2b, checklist CRE-8)

**Status: plan only. Nothing here has run against DEV, production or R2.** Owner decision D-M2b (2026-10-29,
confirmed 2026-11-25): the legacy creator branding assets move to Cloudflare R2 **by copy**, the originals are never
deleted or overwritten, every URL keeps working throughout, and an asset mapping plus a verification plan come first.
This document is that plan. The inventory tool is `deploy/tools/legacy_asset_inventory.py` (read-only, no network
calls to any URL; tests in `backend/tests/ops/test_legacy_asset_inventory.py`).

**Out of scope here:** the image upload API for new creators (profile picture and 16:9 background, validated and
optimised by the backend, replaceable later). That is part of the creator onboarding plan (PDP v1.1, document to
follow) and decides the upload design; nothing in this plan builds or constrains an upload endpoint. The owner's PDP v1.1
(2026-11-25) proposes 5 MB (profile) / 10 MB (background) limits for new uploads, so the size limits for new uploads
are decided by the onboarding plan. Here the D-M2c figures (logo ≤ 1 MB, background ≤ 3 MB, JPEG/PNG/WebP only) serve
only as the **reporting threshold for legacy files** until that plan decides; legacy files are copied byte for byte
(section 4) and only *reported* against them.

## 0. Open owner inputs (needed before step 3 of section 4 can start)

| # | Input | Why | Status |
|---|---|---|---|
| O-1 | **Where the legacy files live**: the legacy frontend repository (path, commit) or the deployed site that serves `/assets/streamers/...`, and read access to it | the source of the bytes to copy; the inventory only knows the paths | open |
| O-2 | **The legacy frontend origin** that serves those paths today (e.g. `https://<legacy host>`) | the "before" URL of every legacy path for the verification in section 5 | open |
| O-3 | **The custom media domain** `HYPERCHAT_R2_PUBLIC_BASE_URL` of DEV and of production (names only; D9: never `r2.dev` for production) | the new URL written to the database is `<base>/<key>`; also the `--r2-base` argument of the inventory | open |
| O-4 | **Approval for R2 writes** under the new prefix `creator-assets/` (DEV first, then production), each window approved separately; `HYPERCHAT_SMOKE_R2` stays off and no code in this PR writes to R2 | D-M2b authorises the plan, not the migration ("migration not yet authorised") | open |
| O-5 | **The third-party URL row** (section 7): the owner's decision for streamer `________` (8-character id prefix, to be filled from the first inventory run; it is not recorded in the repository and this plan invents none), column `________` | separate decision per D-M2b | open |
| O-6 | **Legacy files outside the D-M2c types or sizes** (e.g. GIF, SVG, a background over 3 MB), if the inventory of the files finds any | copy as is, or an owner-supplied replacement; never transcoded by this procedure | open, depends on O-1 |
| O-7 | **Which role runs the SQL** of section 4 step 5 (proposal: `hyperchat_owner` via `SET ROLE` from `hyperchat_migrator`, as migrations do; `hyperchat_app` has no reason to write these columns) and who reviews each per-creator statement | one reviewed statement per creator | open |

Until O-1..O-4 are answered, only sections 1 and 2 can run (the inventory is read-only).

## 1. Inventory (read-only, runs now)

From a root-only staged copy of this commit's `deploy/tools/` on the host (a release has no `deploy/`), with the
release's Python and `-B`, as a script file sent per the OPS-2 rules (`deploy/README.md`, "Running commands against a
server"), output outside the release tree:

```
HYPERCHAT_READONLY_DSN='host=/var/run/postgresql port=5433 dbname=hyperchat_dev user=hyperchat_readonly' \
  /opt/hyperchat/current/venv/bin/python -B /root/d-m2b-<commit>/legacy_asset_inventory.py \
  --r2-base <O-3 DEV base> --csv /root/d-m2b-<commit>/inventory-dev-<UTC>.csv
```

It prints counts per class and column, legacy paths by creator status, the number of distinct legacy paths, and the
`third_party` / `unknown` rows by 8-character id prefix. The CSV holds the full values and the proposed target key
template per legacy cell; it stays on the host (root-only) or in the owner's evidence folder, never in the repository.
Expected on DEV (checklist CRE-8): 140 `legacy_frontend_path` cells, 1 `third_party`, the rest `empty` or `r2`. Any
`unknown` cell is reviewed before anything else happens.

The ETL (`database/etl/mapping.py`, `MEDIA_URL_COLUMNS`) rewrites these two columns only when the value starts with a
legacy R2 base; a `/assets/...` path is not a URL and passes through unchanged. **Production has not been loaded from
legacy yet**, so the production cutover re-creates the 140 legacy paths: section 8 says how the mapping is applied
again there.

## 2. Target key scheme

```
creator-assets/<streamer uuid>/<logo|background>/<sha256 hex of the bytes>.<jpg|png|webp|gif>
```

- **Per-creator isolation** by the streamer's uuid, as `tts-audio/<creator uuid>/...` already does (not the slug: slugs
  are not meant to change, but the uuid is the stable key the audit record also uses).
- **Content-addressed**: the key carries the file hash, so a key is unique and never rewritten, which is what the
  repository's `Cache-Control: public, max-age=31536000, immutable` policy (`backend/app/storage/r2.py`, `IMMUTABLE`)
  assumes. A later replacement (onboarding, PDP v1.1) is a new key; the old object can stay or be removed by a
  later, separately decided cleanup.
- **Consistent with `WRITABLE_KEY`**: the store refuses writes outside its patterns today, so the copy step cannot
  use `R2Store` until a later PR adds `CREATOR_ASSET_KEY =
  ^creator-assets/<uuid>/(logo|background)/[0-9a-f]{64}\.(jpg|png|webp|gif)$` to `WRITABLE_KEY` (and a matching
  `head_` check). That PR comes with O-4, not before, and not before 2026-12-01 (migration freeze, section 3). The daily cleanup lists only `voice-messages/` and
  `donation-media/`, so it never sees these objects.
- Extension from the legacy file name, lower-cased (`.jpeg` becomes `.jpg`); the content type written to R2 is the
  **sniffed** type (`backend/app/uploads/media.py`, `sniff_image`), and a mismatch between extension and bytes stops
  that creator (section 4 step 3).

The inventory emits the template `creator-assets/<uuid>/<kind>/{sha256}.<ext>` per legacy cell; the copy step fills
`{sha256}`.

## 3. Audit record (decided here; the migration is written later, with O-4)

A new table, not a column: a creator has two assets, each can be copied and rolled back independently, and the
history must survive a second run.

```
public.streamer_asset_migrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE RESTRICT,
    column_name text NOT NULL CHECK (column_name IN ('brand_logo_url', 'background_url')),
    old_value text NOT NULL,              -- the legacy path, byte for byte
    new_value text NOT NULL,              -- <base>/<key>
    r2_key text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content_type text NOT NULL,
    bytes bigint NOT NULL CHECK (bytes > 0),
    source text NOT NULL,                 -- O-1 location of the original (repository path @ commit, or URL)
    migrated_at timestamptz NOT NULL DEFAULT now(),
    rolled_back_at timestamptz,
    UNIQUE (streamer_id, column_name, migrated_at)
)
```

Grants: SELECT to `hyperchat_readonly`; INSERT/UPDATE only to the role of O-7; the API roles get nothing (the API
never reads it). `ON DELETE RESTRICT`: a creator with a migration record is not deleted by accident. The migration
file (`database/migrations/versions/00xx_streamer_asset_migrations.py`) is written in the O-4 PR.
**Migration freeze:** no migration, this audit table included, can merge or apply before 2026-12-01.
`backend/app/payments/verify.py` pins `EXPECTED_HEADS` to `0034` / `0035` and is frozen during the PAY-15 soak. The O-4 PR
therefore lands after the soak and after a separate PR that widens the verifier pins. Once applied, the previous release
ignores the table.

## 4. Copy procedure (per creator, DEV first, then production; every window approved)

Preconditions: O-1..O-4 answered, O-7 decided, the inventory CSV of the same day, a verified backup set of that
morning (`docs/backup-recovery.md`), and `HYPERCHAT_SMOKE_R2` still off (the smoke suite never writes R2 during this).

1. **Read the original.** From the O-1 source, read the file for each legacy cell of the creator. A path with no file
   is recorded as `missing` in the run log and that cell is skipped (the database keeps the legacy path; owner decision
   later). Compute `sha256`, size, and the sniffed image type.
2. **Report against D-M2c** (sizes and types) without acting: a file outside the limits goes to O-6, the cell is skipped.
3. **Copy to R2** at the section 2 key with the sniffed content type and `IMMUTABLE` cache control. If the key already
   exists (a re-run), do not overwrite: `HEAD` it and continue to step 4. Nothing is ever deleted from R2 and nothing
   is written outside `creator-assets/`.
4. **Verify the copy**: `HEAD` returns the same size and content type; `GET <base>/<key>` over the public base returns
   bytes whose `sha256` equals the original's. A mismatch stops that creator (the object is left in place, nothing is
   written to the database).
5. **One reviewed SQL statement per creator**, in one transaction, run by the O-7 role, checked in by the reviewer
   before it runs (values from the CSV and the step 4 output; placeholders in angle brackets):

   ```sql
   BEGIN;
   INSERT INTO public.streamer_asset_migrations
       (streamer_id, column_name, old_value, new_value, r2_key, sha256, content_type, bytes, source)
   VALUES ('<uuid>', 'brand_logo_url', '/assets/streamers/<slug>/logo.png', '<base>/creator-assets/<uuid>/logo/<sha256>.png',
           'creator-assets/<uuid>/logo/<sha256>.png', '<sha256>', 'image/png', <bytes>, '<O-1 source>');
   UPDATE public.streamers SET brand_logo_url = '<base>/creator-assets/<uuid>/logo/<sha256>.png', updated_at = now()
    WHERE id = '<uuid>' AND brand_logo_url = '/assets/streamers/<slug>/logo.png';
   -- the UPDATE must report exactly 1 row; 0 rows = the value changed since the inventory: ROLLBACK and re-inventory
   COMMIT;
   ```

   The guard `AND <column> = <old value>` makes the statement idempotent and refuses to overwrite a value that changed
   in between. Both cells of one creator may share one transaction (two INSERTs, two UPDATEs, each checked).
6. **Verify the creator** (section 5) before the next one. A batch is at most 10 creators per approved window on the
   first DEV run; the owner may widen it after the first window passes.

The copy step itself is a script written in the O-4 PR (Python, boto3 through `R2Store` once `CREATOR_ASSET_KEY`
exists, or an equivalent reviewed tool). It reads the CSV, never the database for writing, and prints the SQL of step 5
for review instead of executing it.

## 5. Verification

Before any copy (baseline, once per environment), and after each creator:

| Check | How | Expected |
|---|---|---|
| V-1 every legacy URL resolves before | `GET <O-2 origin><legacy path>` for each legacy cell (read-only, from the operator's machine, not from the host) | 200 and an image content type; a non-200 is recorded and that cell goes to O-6 |
| V-2 the copy resolves after | `GET <base>/<key>`: 200, `Content-Type` = sniffed type, `sha256` = original | exact match |
| V-3 the public API returns the new URL | `GET /api/v1/public/streamers/<slug>` (E1) after the 60 s cache (`docs/public-api.md`) for `active` creators; for `pending` / `rejected` creators (not public) a read-only `SELECT` as `hyperchat_readonly` | `brand_logo_url` / `background_url` equal `new_value` |
| V-4 originals untouched | the O-1 source is unchanged: same `sha256` per file and, for a repository, the same commit; for a served site, V-1 repeated | identical |
| V-5 audit record | `SELECT count(*) FROM public.streamer_asset_migrations WHERE rolled_back_at IS NULL` equals the number of migrated cells; every `new_value` is `<base>/<r2_key>` | consistent |
| V-6 no stray writes | the inventory re-run: the migrated cells are `r2`, every other cell unchanged from the baseline CSV (`diff` of the two CSVs shows only those cells) | only expected diffs |
| V-7 smoke | `tests/e2e/smoke_dev_backend.py` (E1 field set) still passes; nothing else in the API reads these columns | pass |

The old URLs keep working throughout because nothing at O-1 or O-2 is touched (D-M2b: "URLs working throughout");
the frontend simply receives the new URL from E1 once it is written.

## 6. Rollback (per cell, any time)

```sql
BEGIN;
UPDATE public.streamers s SET brand_logo_url = m.old_value, updated_at = now()
  FROM public.streamer_asset_migrations m
 WHERE m.streamer_id = s.id AND m.column_name = 'brand_logo_url' AND m.rolled_back_at IS NULL
   AND s.id = '<uuid>' AND s.brand_logo_url = m.new_value;   -- guarded on the new value: exactly 1 row
UPDATE public.streamer_asset_migrations SET rolled_back_at = now()
 WHERE streamer_id = '<uuid>' AND column_name = 'brand_logo_url' AND rolled_back_at IS NULL;
COMMIT;
```

The R2 object stays (nothing is deleted); the legacy original was never touched, so the old URL works at once. V-3
confirms the API returns the old value after the cache window. A rollback of a whole window is the same statement per
creator, in reverse order of migration.

## 7. The third-party URL (separate owner decision, O-5)

One DEV cell holds a URL to a web page on another site, not an image. The inventory names it by 8-character id prefix
only (`review: third_party streamer <prefix> <column>`); the full value is in the CSV. It is **not** part of the copy:
this procedure never fetches a third-party URL. Options for the owner:
- (a) leave it as is;
- (b) set the cell to NULL with one guarded, reviewed SQL statement, the old value kept in the signed run log (the
  audit table of section 3 records copies only, so it is not used for this);
- (c) the creator supplies an image through the onboarding flow (PDP v1.1) once it exists.

The plan recommends (c) and, until then, (a).

## 8. Production cutover

Production is loaded from legacy by the ETL at cutover (`database/README.md`, "Media URLs at cutover"), which brings
the legacy paths back. Two options, decided with O-4:

- **A (preferred): migrate DEV, export the mapping, apply it at cutover.** The DEV `streamer_asset_migrations` rows
  (`old_value` → `new_value` with the production base) are applied after `--apply` and before reconciliation as the
  same guarded per-creator SQL of section 4 step 5, against the production database, with the objects copied to the
  production bucket beforehand (same keys: the hash is the same bytes). The reconciliation then checks that no
  `legacy_frontend_path` cell remains for a migrated creator.
- **B: a `--media-path-map` in the ETL** that rewrites `/assets/streamers/...` from a CSV during the load. More code
  in the ETL for a one-off; only if A's window is too tight.

Either way the production bucket receives the objects under the same `creator-assets/` keys, approved as a separate
O-4 window.

## 9. What this PR does not do

- No migration, no table, no change to `WRITABLE_KEY` or `R2Store`, no R2 write, no database write.
- No upload API, no branding endpoint: the creator onboarding plan (PDP v1.1) owns the upload design.
- No fetch of any URL: the inventory reads the database only.
