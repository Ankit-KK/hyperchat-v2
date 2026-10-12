"""Record what each D1 password-hash import run wrote, so it can be verified and undone precisely.

Revision ID: 0017
Revises: 0016

The importer (database/etl/auth_hash_import.py) writes one row per imported account, in the SAME transaction as the
password hashes, so a run's rows exist if and only if its hashes were committed:

  run_id              printed by the importer before it writes (import_run_id)
  user_id             the V2 account that received the hash
  hash_sha256         SHA-256 (hex) of the exact hash written: a later check can tell whether the account still holds
                      it, without storing the hash a second time. A bcrypt string is salted and high-entropy, so its
                      digest reveals nothing usable.
  source_fingerprint  the reviewed source fingerprint the run was applied against
  imported_at         when the run wrote it

Why: after an ambiguous COMMIT (the acknowledgement was lost) the ledger tells whether the run committed; after exit
code 4 or a fingerprint mismatch, a rollback clears ONLY the hashes this run wrote that are still unchanged, and lists
the accounts whose hash changed since (e.g. upgraded to Argon2id at sign-in, which does not set password_changed_at),
instead of clearing every bcrypt-looking hash.

Append-only for the API role: hyperchat_app may SELECT and INSERT, not UPDATE or DELETE (the default privileges from
0007 would grant all four); the worker gets nothing; hyperchat_readonly keeps SELECT. A deleted user takes its rows
with it (ON DELETE CASCADE). New, empty table: no rewrite, no lock on existing data beyond the foreign key's brief
SHARE ROW EXCLUSIVE on users. The previous release ignores it.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.d1_hash_import_ledger (
            run_id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            hash_sha256 text NOT NULL CHECK (hash_sha256 ~ '^[0-9a-f]{64}$'),
            source_fingerprint text NOT NULL CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
            imported_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, user_id)
        )
    """)
    op.execute("CREATE INDEX d1_hash_import_ledger_user_idx ON public.d1_hash_import_ledger (user_id)")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON public.d1_hash_import_ledger FROM hyperchat_app")
    op.execute("REVOKE ALL ON public.d1_hash_import_ledger FROM hyperchat_worker")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.d1_hash_import_ledger")
