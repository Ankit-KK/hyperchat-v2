"""Security audit run-4: a new creator starts unclaimed and without soundboard eligibility (step-up INSERT gap).

The creator INSERT anchor (stepup_guard_streamer_insert, 0027 as re-created by 0037) binds a creator.create or
application.approve grant to the slug, name and Razorpay account only. The columns whose change is its own anchored
action - streamers.user_id (creator.owner.assign / transfer, 0027) and streamers.soundboard_enabled
(creator.soundboard, 0037) - are guarded on UPDATE only. So under 'active' enforcement a compromised API process could
ride one genuine creator.create ceremony to insert the creator already owned (by an account of its choice) and
soundboard-enabled, without those two ceremonies (finding stepup-creator-insert-binds-only-slug-name-account).

No API path inserts either: AdminRepository.insert_creator writes LAUNCH_DEFAULTS plus the bound fields, both
unclaimed, and soundboard_enabled defaults to false. This adds a BEFORE INSERT guard on public.streamers that refuses,
for every API login role and in every enforcement state, a new creator with user_id set or soundboard_enabled true,
the same rule stepup_guard_users already applies to creator-linked users. Operators, migrations and the legacy ETL
log in as non-API roles and are unaffected. Ownership and eligibility then always arrive through their own
UPDATE-time anchors. No data is read or written; downgrade drops the trigger and the function.
"""

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

API_ROLES = "('hyperchat_app', 'hyperchat_worker', 'hyperchat_auth', 'hyperchat_lifecycle', 'hyperchat_stepup')"
EVERYONE = ("hyperchat_app, hyperchat_worker, hyperchat_auth, hyperchat_lifecycle, hyperchat_readonly, "
            "hyperchat_stepup, PUBLIC")


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION public.stepup_guard_streamer_insert_columns() RETURNS trigger LANGUAGE plpgsql
        SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
        BEGIN
            IF session_user IN {API_ROLES} AND (NEW.user_id IS NOT NULL OR NEW.soundboard_enabled IS TRUE) THEN
                RAISE EXCEPTION 'a new creator starts unclaimed and without soundboard eligibility'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("CREATE TRIGGER aad_stepup_guard_streamer_insert_columns BEFORE INSERT ON public.streamers "
               "FOR EACH ROW EXECUTE FUNCTION public.stepup_guard_streamer_insert_columns()")
    op.execute(f"REVOKE ALL ON FUNCTION public.stepup_guard_streamer_insert_columns() FROM {EVERYONE}")


def downgrade() -> None:
    op.execute("DROP TRIGGER aad_stepup_guard_streamer_insert_columns ON public.streamers")
    op.execute("DROP FUNCTION public.stepup_guard_streamer_insert_columns()")
