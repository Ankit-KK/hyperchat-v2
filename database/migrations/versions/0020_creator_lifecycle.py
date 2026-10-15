"""Creator lifecycle: new creators start pending, `suspended` exists, legacy creators are tracked for KYC (PR1 of the
owner-approved manual approval + mandatory Didit KYC plan, 2026-10-15).

Revision ID: 0020
Revises: 0019

* streamers.status: new value 'suspended' (an admin decision; hidden and refused exactly like 'pending' because every
  public / checkout check is `status = 'active'`). The column default becomes 'pending': a creator row inserted by any
  path is never public by accident; activation is an explicit admin action (app.admin).
* streamers.kyc_track ('standard' | 'legacy'): every creator that exists when this runs is 'legacy' (it was active
  before KYC existed); new rows default to 'standard'. 'legacy' is NOT an exemption: it only means "not yet enrolled
  in the KYC rollout" (later PRs add verification, the audited rollout and its expiring grace grants).
* streamers.kyc_baseline_owner_id: the owner a legacy creator had when it entered V2. A legacy creator keeps its
  pre-rollout donation eligibility only while its current owner is still that person (owner decision 2026-10-15: an
  unverified new owner never accepts donations). No foreign key on purpose: if the user is ever deleted the stale id
  no longer matches the (now NULL) owner, so the creator fails closed instead of re-qualifying.
  Both columns are written only by migrations and the legacy import: the runtime roles may not set or change them.
* creator_status_events: append-only audit of admin activate / suspend / reactivate (the runtime roles may insert and
  read, never update or delete).
* public.creator_donation_block(streamer_id): the ONE eligibility rule for new donations, NULL when allowed, otherwise
  a reason code ('not_found', 'not_active', 'kyc_required'). The API calls it before checkout and uploads; a trigger
  on donations runs it again, under a share lock on the creator row, for every insert by a runtime role, so no API
  path can create a donation for an ineligible creator. Payment readiness (Razorpay health) stays where it is, in the
  checkout code: it may call Razorpay.

Nothing existing changes: no status, owner, donation or updated_at is modified (the baseline backfill runs with only
the updated_at trigger paused). Every existing active creator stays eligible: it is legacy and its owner is its
baseline. Changed behaviour: a creator created by the admin API is pending (and cannot be activated until KYC exists);
a legacy creator whose owner is assigned or transferred stops accepting new donations (owner decision).

Downgrade refuses (and changes nothing) while a creator is 'suspended' or the audit table has rows.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

RUNTIME = ("hyperchat_app", "hyperchat_worker")
_RUNTIME_SQL = "('hyperchat_app', 'hyperchat_worker')"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.streamers
            DROP CONSTRAINT streamers_status_check,
            ADD CONSTRAINT streamers_status_check CHECK (status IN ('pending', 'active', 'rejected', 'suspended')),
            ALTER COLUMN status SET DEFAULT 'pending',
            ADD COLUMN kyc_track text NOT NULL DEFAULT 'legacy' CHECK (kyc_track IN ('standard', 'legacy')),
            ADD COLUMN kyc_baseline_owner_id uuid
    """)
    # Existing rows took 'legacy' from the column default (no rewrite); rows created from now on are 'standard'.
    op.execute("ALTER TABLE public.streamers ALTER COLUMN kyc_track SET DEFAULT 'standard'")
    op.execute("ALTER TABLE public.streamers DISABLE TRIGGER streamers_set_updated_at")
    op.execute("UPDATE public.streamers SET kyc_baseline_owner_id = user_id WHERE user_id IS NOT NULL")
    op.execute("ALTER TABLE public.streamers ENABLE TRIGGER streamers_set_updated_at")

    op.execute(f"""
        CREATE FUNCTION public.streamers_protect_kyc_columns() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user IN {_RUNTIME_SQL} AND (
                   (TG_OP = 'INSERT' AND (NEW.kyc_track <> 'standard' OR NEW.kyc_baseline_owner_id IS NOT NULL))
                OR (TG_OP = 'UPDATE' AND (NEW.kyc_track IS DISTINCT FROM OLD.kyc_track
                                          OR NEW.kyc_baseline_owner_id IS DISTINCT FROM OLD.kyc_baseline_owner_id)))
            THEN
                RAISE EXCEPTION 'streamers.kyc_track / kyc_baseline_owner_id are set only by migrations and the '
                                'legacy import' USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER streamers_protect_kyc_columns BEFORE INSERT OR UPDATE ON public.streamers "
               "FOR EACH ROW EXECUTE FUNCTION public.streamers_protect_kyc_columns()")

    op.execute("""
        CREATE TABLE public.creator_status_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            streamer_id uuid NOT NULL REFERENCES public.streamers(id) ON DELETE CASCADE,
            action text NOT NULL CHECK (action IN ('activate', 'suspend', 'reactivate')),
            from_status text NOT NULL,
            to_status text NOT NULL,
            actor_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            reason text CHECK (char_length(reason) <= 500),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX creator_status_events_streamer_idx ON public.creator_status_events (streamer_id, created_at)")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON public.creator_status_events FROM hyperchat_app")
    op.execute("REVOKE ALL ON public.creator_status_events FROM hyperchat_worker")

    # VOLATILE (the default) on purpose: called from the trigger after the row lock, it must see the latest commit.
    op.execute("""
        CREATE FUNCTION public.creator_donation_block(p_streamer_id uuid) RETURNS text LANGUAGE sql AS $$
            SELECT CASE
                WHEN s.id IS NULL THEN 'not_found'
                WHEN s.status <> 'active' THEN 'not_active'
                -- Until KYC exists, only a legacy creator still owned by its baseline owner may accept donations.
                WHEN NOT (s.kyc_track = 'legacy' AND s.user_id IS NOT DISTINCT FROM s.kyc_baseline_owner_id)
                    THEN 'kyc_required'
            END
            FROM (SELECT 1) AS one LEFT JOIN public.streamers s ON s.id = p_streamer_id
        $$
    """)
    op.execute(f"GRANT EXECUTE ON FUNCTION public.creator_donation_block(uuid) TO {', '.join(RUNTIME)}")
    op.execute(f"""
        CREATE FUNCTION public.donations_require_eligible_creator() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            block text;
        BEGIN
            IF current_user IN {_RUNTIME_SQL} THEN
                -- Serialize with admin status changes and owner transfers (they update the creator row).
                PERFORM 1 FROM public.streamers WHERE id = NEW.streamer_id FOR SHARE;
                block := public.creator_donation_block(NEW.streamer_id);
                IF block IS NOT NULL THEN
                    RAISE EXCEPTION 'creator not eligible for new donations: %', block
                        USING ERRCODE = 'HC001', HINT = block;
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER donations_require_eligible_creator BEFORE INSERT ON public.donations "
               "FOR EACH ROW EXECUTE FUNCTION public.donations_require_eligible_creator()")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE suspended bigint; events bigint;
        BEGIN
            SELECT count(*) INTO suspended FROM public.streamers WHERE status = 'suspended';
            SELECT count(*) INTO events FROM public.creator_status_events;
            IF suspended > 0 OR events > 0 THEN
                RAISE EXCEPTION 'downgrade of 0020 stopped: % suspended creator(s), % status audit row(s) would be '
                                'lost (nothing was changed)', suspended, events;
            END IF;
        END $$
    """)
    op.execute("DROP TRIGGER donations_require_eligible_creator ON public.donations")
    op.execute("DROP FUNCTION public.donations_require_eligible_creator()")
    op.execute("DROP FUNCTION public.creator_donation_block(uuid)")
    op.execute("DROP TABLE public.creator_status_events")
    op.execute("DROP TRIGGER streamers_protect_kyc_columns ON public.streamers")
    op.execute("DROP FUNCTION public.streamers_protect_kyc_columns()")
    op.execute("""
        ALTER TABLE public.streamers
            DROP COLUMN kyc_baseline_owner_id,
            DROP COLUMN kyc_track,
            ALTER COLUMN status SET DEFAULT 'active',
            DROP CONSTRAINT streamers_status_check,
            ADD CONSTRAINT streamers_status_check CHECK (status IN ('pending', 'active', 'rejected'))
    """)
