"""Creator applications: admin review of the public signup interest form (KYC PR2, owner-approved manual creator
approval, 2026-10-15).

Revision ID: 0021
Revises: 0020

* user_signups (the public form's rows, POST /api/v1/public/signups) become applications, all review columns
  server-controlled:
    review_status   'legacy_unreviewed' (every row that exists when this runs: signups collected before manual
                    approval, not new submissions) | 'submitted' (the default for new signups) | 'approved' | 'rejected'
    reviewed_by / reviewed_at, rejection_reason (required while rejected),
    streamer_id     the pending creator an approval created (required while approved; RESTRICT: never lose the link),
    invite_id       the latest account invitation sent for it.
  Existing rows take 'legacy_unreviewed' from the column default (no rewrite); new rows default to 'submitted'.
* application_events: append-only review history (approve / reject / reopen / note / invite); the runtime roles may
  insert and read, never update or delete. Never holds a token.
* user_invites.signup_id: an invitation issued for an approved application. Only such an invite may make its invitee
  the owner of its (still unclaimed) creator on acceptance (app.auth.repository.accept_invite).

The public signup endpoint is unchanged. No creator, user or donation is modified.

Downgrade refuses (and changes nothing) once any application was reviewed, any review event exists or any invite was
issued for an application: reverting would silently drop decisions and the invite-to-creator binding.
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.user_signups
            ADD COLUMN review_status text NOT NULL DEFAULT 'legacy_unreviewed'
                CHECK (review_status IN ('legacy_unreviewed', 'submitted', 'approved', 'rejected')),
            ADD COLUMN reviewed_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
            ADD COLUMN reviewed_at timestamptz,
            ADD COLUMN rejection_reason text CHECK (char_length(rejection_reason) <= 500),
            ADD COLUMN streamer_id uuid UNIQUE REFERENCES public.streamers(id) ON DELETE RESTRICT,
            ADD COLUMN invite_id uuid,
            ADD CONSTRAINT user_signups_approved_has_creator
                CHECK (review_status <> 'approved' OR streamer_id IS NOT NULL),
            ADD CONSTRAINT user_signups_creator_only_when_approved
                CHECK (streamer_id IS NULL OR review_status = 'approved'),
            ADD CONSTRAINT user_signups_rejected_has_reason
                CHECK ((review_status = 'rejected') = (rejection_reason IS NOT NULL))
    """)
    op.execute("ALTER TABLE public.user_signups ALTER COLUMN review_status SET DEFAULT 'submitted'")
    op.execute("CREATE INDEX user_signups_review_idx ON public.user_signups (review_status, created_at)")

    op.execute("ALTER TABLE public.user_invites ADD COLUMN signup_id uuid "
               "REFERENCES public.user_signups(id) ON DELETE SET NULL")
    op.execute("CREATE INDEX user_invites_signup_idx ON public.user_invites (signup_id) WHERE signup_id IS NOT NULL")
    op.execute("ALTER TABLE public.user_signups ADD CONSTRAINT user_signups_invite_fk "
               "FOREIGN KEY (invite_id) REFERENCES public.user_invites(id) ON DELETE SET NULL")

    op.execute("""
        CREATE TABLE public.application_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            signup_id uuid NOT NULL REFERENCES public.user_signups(id) ON DELETE CASCADE,
            action text NOT NULL CHECK (action IN ('approve', 'reject', 'reopen', 'note', 'invite')),
            actor_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
            note text CHECK (char_length(note) <= 2000),
            streamer_id uuid,
            invite_id uuid,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX application_events_signup_idx ON public.application_events (signup_id, created_at)")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON public.application_events FROM hyperchat_app")
    op.execute("REVOKE ALL ON public.application_events FROM hyperchat_worker")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE reviewed bigint; events bigint; invites bigint;
        BEGIN
            SELECT count(*) INTO reviewed FROM public.user_signups
                WHERE review_status NOT IN ('legacy_unreviewed', 'submitted');
            SELECT count(*) INTO events FROM public.application_events;
            SELECT count(*) INTO invites FROM public.user_invites WHERE signup_id IS NOT NULL;
            IF reviewed > 0 OR events > 0 OR invites > 0 THEN
                RAISE EXCEPTION 'downgrade of 0021 stopped: % reviewed application(s), % review event(s), % application '
                                'invite(s) would be lost (nothing was changed)', reviewed, events, invites;
            END IF;
        END $$
    """)
    op.execute("DROP TABLE public.application_events")
    op.execute("ALTER TABLE public.user_signups DROP CONSTRAINT user_signups_invite_fk")
    op.execute("ALTER TABLE public.user_invites DROP COLUMN signup_id")
    op.execute("DROP INDEX public.user_signups_review_idx")
    op.execute("""
        ALTER TABLE public.user_signups
            DROP COLUMN invite_id, DROP COLUMN streamer_id, DROP COLUMN rejection_reason, DROP COLUMN reviewed_at,
            DROP COLUMN reviewed_by, DROP COLUMN review_status
    """)
