"""Migration 0045 renders from 0023's and 0040's own text with exactly its three insertions (no database)."""
from importlib import util
from pathlib import Path

VERSIONS = Path(__file__).parents[3] / "database/migrations/versions"


def _module():
    spec = util.spec_from_file_location("m_0045", VERSIONS / "0045_kyc_witness_suspension_account_revoke.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_function_is_its_previous_body_plus_one_insertion():
    m = _module()
    before, after = m.originals(), m.functions()
    assert set(before) == set(after) == {"kyc_change_creator_status", "kyc_account_revoke", "kyc_receive_webhook"}
    for name, sql in after.items():
        assert sql.startswith(before[name][:before[name].index("AS $$")])  # same signature, definer, search_path
        assert "SET search_path = pg_catalog, public, pg_temp" in sql
        assert len(sql) > len(before[name])
    started = "generation > 0 OR state <> 'draft'"  # never on an untouched pre-rollout database (no witness yet)
    assert started in after["kyc_change_creator_status"] and "'creator_suspended', s.id" in after[
        "kyc_change_creator_status"]
    assert started in after["kyc_account_revoke"] and "kyc_enforcement_log('account_revoked'" in after[
        "kyc_account_revoke"]
    hook = after["kyc_receive_webhook"]
    assert hook.index("RETURN 'bad_signature'") < hook.index("RETURN 'account_revoked'") < hook.index(
        "RETURN 'malformed'")  # only a genuine signature learns the account is revoked


def test_the_new_actions_widen_the_audit_check_and_the_service_maps_the_refusal():
    m = _module()
    assert m.NEW_ACTIONS.startswith(m.OLD_ACTIONS) and m.NEW_ACTIONS.endswith("'creator_suspended', 'account_revoked'")
    from app.kyc.service import REFUSED
    assert REFUSED["account_revoked"][0] == 403  # Didit retries only 5xx and 404: a revoked account is not retried
