"""Onboarding Stage 4 Phase B (PDP v1.1, owner decisions Q8, Q9): creator_image_uploads, the record of every image
the pipeline (app.onboarding.images, docs/creator-images.md) stored, for validity, audit and tracked cleanup.

Revision ID: 0039
Revises: 0038

One row per upload of a profile picture (brand_logo_url, Q9) or a page background (background_url):
  * state 'ready'       the creator's current validated image of that kind (at most one per creator and kind);
          'superseded'  replaced by a later upload (superseded_at); its objects are deleted after 30 days;
          'failed'      objects a failed upload created and could not delete at once; deleted by the next cleanup;
  * variants: a JSON array of {key, url, width, height, bytes}, primary (pointer) variant first: the profile's
    1024 px and 256 px, the background's 1920x1080 and (when the source allows) 2560x1440. Every key is a pipeline
    key of THIS row's creator and kind: creator-assets/<streamer_id>/<logo|background>/<sha256>.webp;
  * source dimensions, the quality warning, the uploading user (no foreign key: an audit id), cleaned_at once its
    objects are gone.
Validity is the row, not the prefix: legacy copies (S6) live under the same prefix unvalidated and get no row.

The API's role (hyperchat_app) writes the rows: the pipeline in its upload transaction, the daily cleanup
(app.maintenance) its cleaned_at. A guard keeps them honest for every API role: a row starts 'ready' or 'failed'
with its own keys; afterwards only 'ready' -> 'superseded' (now, once) and cleaned_at (now, once, superseded or
failed rows only) change. DELETE and TRUNCATE are revoked; the worker gets nothing; the creator is ON DELETE RESTRICT
like every other creator audit table. Backfill: none (a migration cannot read R2, and Phase A uploads carry no
dimensions); a Phase A upload has no row until the creator uploads again.

Downgrade refuses while any row exists (nothing is changed then); on an empty table it drops the table, the guard
and the index. No row is ever written or deleted by either direction.
"""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
REFUSE = "USING ERRCODE = 'insufficient_privilege'"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.creator_image_uploads (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE RESTRICT,
            kind text NOT NULL CHECK (kind IN ('profile', 'background')),
            state text NOT NULL CHECK (state IN ('ready', 'superseded', 'failed')),
            variants jsonb NOT NULL CHECK (jsonb_typeof(variants) = 'array'
                                           AND jsonb_array_length(variants) BETWEEN 1 AND 4),
            source_width integer CHECK (source_width > 0),
            source_height integer CHECK (source_height > 0),
            quality_warning boolean NOT NULL DEFAULT false,
            uploaded_by uuid,  -- the user who uploaded (an audit id, no foreign key)
            error_code text CHECK (char_length(error_code) <= 64),
            created_at timestamptz NOT NULL DEFAULT now(),
            superseded_at timestamptz,
            cleaned_at timestamptz,
            CHECK ((state = 'superseded') = (superseded_at IS NOT NULL)),
            CHECK (cleaned_at IS NULL OR state IN ('superseded', 'failed')),
            CHECK ((state = 'failed') = (error_code IS NOT NULL))
        )
    """)
    op.execute("CREATE UNIQUE INDEX creator_image_uploads_one_ready ON public.creator_image_uploads (streamer_id, kind) "
               "WHERE state = 'ready'")
    op.execute("CREATE INDEX creator_image_uploads_cleanup ON public.creator_image_uploads (state, superseded_at) "
               "WHERE cleaned_at IS NULL AND state IN ('superseded', 'failed')")
    op.execute(f"""
        CREATE FUNCTION public.creator_image_uploads_guard() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
        DECLARE dir text := CASE NEW.kind WHEN 'profile' THEN 'logo' ELSE 'background' END;
        BEGIN
            IF session_user NOT IN {API_ROLES} THEN
                RETURN NEW;  -- the operator and migrations stay outside
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state NOT IN ('ready', 'failed') OR NEW.superseded_at IS NOT NULL OR NEW.cleaned_at IS NOT NULL
                   OR NEW.created_at <> now() THEN
                    RAISE EXCEPTION 'a creator image starts ready or failed, now' {REFUSE};
                END IF;
                IF EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.variants) v  -- IS NOT TRUE: a missing key refuses
                           WHERE (jsonb_typeof(v) = 'object' AND jsonb_typeof(v -> 'key') = 'string'
                                  AND (v ->> 'key') ~ ('^creator-assets/' || NEW.streamer_id::text || '/' || dir
                                                       || '/[0-9a-f]{{64}}\\.webp$')) IS NOT TRUE) THEN
                    RAISE EXCEPTION 'a creator image names only its own creator''s pipeline keys' {REFUSE};
                END IF;
                RETURN NEW;
            END IF;
            IF (to_jsonb(NEW) - 'state' - 'superseded_at' - 'cleaned_at')
                   <> (to_jsonb(OLD) - 'state' - 'superseded_at' - 'cleaned_at')
               OR (NEW.state IS DISTINCT FROM OLD.state
                   AND NOT (OLD.state = 'ready' AND NEW.state = 'superseded' AND NEW.superseded_at = now()))
               OR (NEW.state IS NOT DISTINCT FROM OLD.state AND NEW.superseded_at IS DISTINCT FROM OLD.superseded_at)
               OR (NEW.cleaned_at IS DISTINCT FROM OLD.cleaned_at
                   AND (OLD.cleaned_at IS NOT NULL OR NEW.cleaned_at <> now())) THEN
                RAISE EXCEPTION 'a creator image is only superseded or cleaned, once' {REFUSE};
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER creator_image_uploads_guard BEFORE INSERT OR UPDATE ON public.creator_image_uploads "
               "FOR EACH ROW EXECUTE FUNCTION public.creator_image_uploads_guard()")
    op.execute(f"REVOKE ALL ON FUNCTION public.creator_image_uploads_guard() FROM {EVERYONE}")
    # 0007's default privileges gave app and worker full DML: the API keeps SELECT, INSERT, UPDATE only.
    op.execute("REVOKE ALL ON public.creator_image_uploads FROM hyperchat_app, hyperchat_worker")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.creator_image_uploads TO hyperchat_app")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM public.creator_image_uploads;
            IF n > 0 THEN
                RAISE EXCEPTION 'downgrade of 0039 stopped: % creator image record(s) would be lost (nothing was '
                                'changed); the operational rollback is the previous release', n;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE public.creator_image_uploads")
    op.execute("DROP FUNCTION public.creator_image_uploads_guard()")
