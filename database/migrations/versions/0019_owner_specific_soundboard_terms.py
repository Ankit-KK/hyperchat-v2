"""Custom Soundboard terms acceptance is owner-specific (owner decision D13, 2026-10-15).

Revision ID: 0019
Revises: 0018

D3 requires the creator's OWNER to accept the current terms. With UNIQUE (streamer_id, terms_version) a creator could
hold only one acceptance per version, so after an ownership transfer (D13) the new owner could never record their own.
The key becomes (streamer_id, terms_version, user_id): each owner accepts for themselves, and the app counts only the
acceptance of the creator's current owner (app.soundboard.terms). Every existing row is kept as it is; nothing is
rewritten. Small table (legacy: a handful of rows), changed under a short lock bounded by lock_timeout in env.py.

The previous release's ON CONFLICT (streamer_id, terms_version) has no matching unique key afterwards, so its terms
acceptance would fail: migrate and switch the release in the same deploy (the normal order). Everything else in the
previous release works unchanged.

Downgrade restores (streamer_id, terms_version) only when no creator holds acceptances of one version by different
users; otherwise it stops with an error and changes nothing (it never deletes or merges an acceptance).
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

OLD = "soundboard_tnc_acceptances_streamer_id_terms_version_key"
NEW = "soundboard_tnc_acceptances_streamer_version_user_key"


def upgrade() -> None:
    op.execute(f"ALTER TABLE public.soundboard_tnc_acceptances DROP CONSTRAINT {OLD}, "
               f"ADD CONSTRAINT {NEW} UNIQUE (streamer_id, terms_version, user_id)")


def downgrade() -> None:
    op.execute("""
        DO $$
        DECLARE n bigint;
        BEGIN
            SELECT count(*) INTO n FROM (SELECT 1 FROM public.soundboard_tnc_acceptances
                                         GROUP BY streamer_id, terms_version HAVING count(*) > 1) d;
            IF n > 0 THEN
                RAISE EXCEPTION 'downgrade of 0019 stopped: % creator/version pair(s) have acceptances by more than '
                                'one owner; the old key allows one (nothing was changed)', n;
            END IF;
        END $$
    """)
    op.execute(f"ALTER TABLE public.soundboard_tnc_acceptances DROP CONSTRAINT {NEW}, "
               f"ADD CONSTRAINT {OLD} UNIQUE (streamer_id, terms_version)")
