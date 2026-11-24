"""PAY-15 (D17) stage 6a-4: the accounting switch's startup precondition and the L4 deploy check.

Evaluation runs only when HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is true AND the database state is 'active', read inside
each evaluation transaction (repository._settle_accounting under payment_accounting_lock_state, L-1). 'frozen' or
'off' stops evaluation at once, with no restart, and leaves applied reversals as they are (F12).

With the setting true, the API (and payment-reconcile, which evaluates too) refuses to start unless the database
carries migration 0035's hardening (F-7, F-11). Runtime roles cannot read alembic_version (0007), so this checks
the four 0035 objects themselves in the catalog: the scope check function, the enabled acknowledgement guard and
both validated foreign keys.
"""

import re
from pathlib import Path

# The reversal-aware credit writer (migration 0032). Code below it debits the full amount_inr on a reject and would
# over-debit a reversed donation (accounting plan L4): a release carries the rule iff its tree has this migration.
ACCOUNTING_WRITER_REVISION = "0032"
WRITER_MIGRATION = "database/migrations/versions/0032_donation_reversal_accounting.py"
RELEASES_ROOT = "/opt/hyperchat/releases"
RELEASE_ID = re.compile(r"[0-9a-f]{12}|[0-9a-f]{40}")  # the host's 12-hex release id, or a full commit

HARDENING = """SELECT
    to_regprocedure('public.payment_accounting_scope_covers(jsonb, timestamp with time zone, text, uuid)')
        IS NOT NULL AS scope_covers,
    EXISTS (SELECT 1 FROM pg_catalog.pg_trigger
            WHERE tgname = 'donation_accounting_evaluations_ack_guard' AND NOT tgisinternal
              AND tgrelid = to_regclass('public.donation_accounting_evaluations') AND tgenabled IN ('O', 'A'))
        AS ack_guard,
    (SELECT count(*) FROM pg_catalog.pg_constraint
     WHERE contype = 'f' AND convalidated AND (conname, conrelid) IN (
         ('donation_accounting_evaluations_ack_run', to_regclass('public.donation_accounting_evaluations')),
         ('donation_credit_events_run_id', to_regclass('public.donation_credit_events')))) = 2 AS foreign_keys"""
APPLIED = "SELECT count(*) FROM public.donation_reversals WHERE applied_reversed_paise > 0"


class PreconditionError(RuntimeError):
    """The accounting setting is true but the database lacks 0035's hardening: refuse to start."""


def hardening_missing(row: dict) -> list[str]:
    return [name for name in ("scope_covers", "ack_guard", "foreign_keys") if row.get(name) is not True]


async def require_hardening(connect) -> None:
    """Refuse (PreconditionError) unless every 0035 object is present; an unreachable database refuses too."""
    try:
        async with connect() as conn:
            cur = await conn.execute(HARDENING)
            row = dict(zip([c.name for c in cur.description], await cur.fetchone(), strict=True))
    except PreconditionError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed: no proof of 0035, no accounting
        raise PreconditionError("Refusing to start: HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is true but the database "
                                f"could not be checked for migration 0035 ({type(exc).__name__})") from None
    if missing := hardening_missing(row):
        raise PreconditionError("Refusing to start: HYPERCHAT_PAYMENT_ACCOUNTING_ENABLED is true but the database "
                                f"lacks migration 0035's hardening (missing: {', '.join(missing)})")


def release_dir(releases_root: str, target: str) -> Path:
    """The ONE release directory `target` names: a release id exactly as the host names its directory (12 hex, e.g.
    0963113493c7), or a 40-hex commit matching a directory named by the full commit or by its 12-hex prefix. Refuses
    (ValueError) a malformed target, no match, an ambiguous match or an unreadable root."""
    if not RELEASE_ID.fullmatch(target or ""):
        raise ValueError(f"not a 12-hex release id or a 40-hex commit: {target!r}")
    try:
        names = [p.name for p in Path(releases_root).iterdir() if p.is_dir() and RELEASE_ID.fullmatch(p.name)]
    except OSError as exc:
        raise ValueError(f"cannot read the releases root {releases_root} ({type(exc).__name__})") from None
    matches = [n for n in names if n == target or (len(target) == 40 and len(n) == 12 and target.startswith(n))]
    if len(matches) != 1:
        raise ValueError(f"{'no' if not matches else 'an ambiguous'} release directory for {target} in {releases_root}"
                         + (f" ({', '.join(sorted(matches))})" if matches else ""))
    return Path(releases_root) / matches[0]


def carries_writer_rule(releases_root: str, target: str) -> bool:
    """L4: whether the release tree `target` names (release_dir) has the writer migration. Refuses (ValueError) a
    target that does not name exactly one existing release tree. An operator guard against an accidental rollback, not
    a tamper control: whoever can edit the release trees can defeat it, so file presence is sufficient."""
    release = release_dir(releases_root, target)
    if not (release / "database" / "migrations" / "versions").is_dir():
        raise ValueError(f"no release tree with migrations at {release}")
    return (release / WRITER_MIGRATION).is_file()
