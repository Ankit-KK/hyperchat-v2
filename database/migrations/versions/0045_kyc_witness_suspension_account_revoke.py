"""Security audit run-4 (NV4-2, owner decision 2026-11-28): suspensions and Didit account revocations enter the KYC
restore witness, and a revoked account's webhooks are ignored.

The restore witness (0023, app.kyc.witness) anchors the kyc_enforcement generation and hash chain outside PostgreSQL,
so a database restored to before a restriction or a grace revocation reads restore_suspected and new donations fail
closed until the operator reconciles it. Two other writers stop a creator's money through the same rule but never moved
the generation:
  * kyc_change_creator_status 'suspend' (0023) logged only through kyc_revoke_grace, i.e. only when the creator had
    open grace, so restoring an older set silently re-activated a suspended creator with a consistent witness;
  * kyc_account_revoke (0040) wrote only kyc_account_events, so a restore silently counted a revoked Didit account's
    approvals again (kyc_effective filters accounts on revoked_at IS NULL).
Both now also append a kyc_enforcement_events row ('creator_suspended', 'account_revoked', with the account id as the
detail) through kyc_enforcement_log, so the change moves the generation and the next witness line anchors it.

Only once the witness history has started (generation > 0 or the state left draft). KycWitness.problem() already
fails every new donation closed once the generation is above 0 without a configured witness, and an untouched
pre-rollout database (production today) has none: logging there would turn the first ordinary suspension into a
platform-wide stop. Before the first enforcement change the witness protects nothing, as before.

kyc_receive_webhook (0040) chose a further account by the body's application_id without looking at revoked_at, so a
revoked account's (possibly leaked) secret could still record statuses on its sessions, which a restore would make
count again. A receipt signed for a revoked account, the primary included, now returns 'account_revoked' after the
signature check and records nothing.

The three functions are re-created from 0023's and 0040's own text (rendered as 0037 renders 0027), each with one
asserted insertion: bodies otherwise identical, same owner, grants and search_path. No table data is read or written.
Downgrade restores the 0023/0040 bodies and the action CHECK; it refuses while a 'creator_suspended' or
'account_revoked' event exists (the audit is append-only).
"""

import importlib.util
import pathlib
import types

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

OLD_ACTIONS = ("'enforcement_activated', 'enforcement_closed', 'grace_granted', 'grace_revoked', 'grace_expired', "
               "'restriction_added', 'restriction_lifted', 'restore_acknowledged'")
NEW_ACTIONS = OLD_ACTIONS + ", 'creator_suspended', 'account_revoked'"
STARTED = "EXISTS (SELECT 1 FROM public.kyc_enforcement WHERE generation > 0 OR state <> 'draft')"


def _render(filename: str, name: str) -> list[str]:
    """A migration's upgrade() statements, rendered without a database."""
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module.op = types.SimpleNamespace(execute=statements.append)
    module.upgrade()
    return statements


def _function(statements: list[str], name: str) -> str:
    (sql,) = [s for s in statements if f"CREATE FUNCTION public.{name}(" in s
              or f"CREATE OR REPLACE FUNCTION public.{name}(" in s]
    return sql.replace(f"CREATE FUNCTION public.{name}(", f"CREATE OR REPLACE FUNCTION public.{name}(", 1)


def _swap(sql: str, old: str, new: str) -> str:
    assert sql.count(old) == 1, f"source text changed: {old!r}"
    return sql.replace(old, new)


def originals() -> dict[str, str]:
    """The current bodies: kyc_change_creator_status (0023), kyc_account_revoke and kyc_receive_webhook (0040)."""
    s23 = _render("0023_kyc_enforcement.py", "_hyperchat_0023")
    s40 = _render("0040_kyc_provider_accounts.py", "_hyperchat_0040")
    return {"kyc_change_creator_status": _function(s23, "kyc_change_creator_status"),
            "kyc_account_revoke": _function(s40, "kyc_account_revoke"),
            "kyc_receive_webhook": _function(s40, "kyc_receive_webhook")}


def functions() -> dict[str, str]:
    f = originals()
    f["kyc_change_creator_status"] = _swap(
        f["kyc_change_creator_status"],
        "                PERFORM public.kyc_revoke_grace(s.id, 'creator_suspended', p_actor);\n",
        "                PERFORM public.kyc_revoke_grace(s.id, 'creator_suspended', p_actor);\n"
        f"                IF {STARTED} THEN  -- 0045: witnessed once the witness history has started\n"
        "                    PERFORM public.kyc_enforcement_log('creator_suspended', s.id, NULL, NULL, p_actor, NULL,\n"
        "                                                       NULL);\n"
        "                END IF;\n")
    f["kyc_account_revoke"] = _swap(
        f["kyc_account_revoke"],
        "            VALUES ('account_revoked', p_account, btrim(p_operator), btrim(p_reason));\n",
        "            VALUES ('account_revoked', p_account, btrim(p_operator), btrim(p_reason));\n"
        f"            IF {STARTED} THEN  -- 0045: witnessed once the witness history has started\n"
        "                PERFORM public.kyc_enforcement_log('account_revoked', NULL, NULL, NULL, NULL, btrim(p_operator),\n"
        "                                                   p_account);\n"
        "            END IF;\n")
    f["kyc_receive_webhook"] = _swap(
        f["kyc_receive_webhook"],
        "                RETURN 'bad_signature';\n            END IF;\n",
        "                RETURN 'bad_signature';\n            END IF;\n"
        "            IF (v_account = 'primary' AND prim.revoked_at IS NOT NULL)\n"
        "               OR (v_account <> 'primary' AND extra.revoked_at IS NOT NULL) THEN\n"
        "                RETURN 'account_revoked';  -- 0045: a revoked account's receipts are never recorded\n"
        "            END IF;\n")
    return f


def _actions(actions: str) -> str:
    return f"""
        ALTER TABLE public.kyc_enforcement_events DROP CONSTRAINT kyc_enforcement_events_action_check,
            ADD CONSTRAINT kyc_enforcement_events_action_check CHECK (action IN ({actions}))
    """


def upgrade() -> None:
    op.execute(_actions(NEW_ACTIONS))
    for sql in functions().values():
        op.execute(sql)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.kyc_enforcement_events
                       WHERE action IN ('creator_suspended', 'account_revoked')) THEN
                RAISE EXCEPTION '0045 downgrade refused: suspensions or account revocations are in the KYC audit';
            END IF;
        END $$
    """)
    for sql in originals().values():
        op.execute(sql)
    op.execute(_actions(OLD_ACTIONS))
