"""Canonical step-up actions: deterministic bytes, refusals, and the shared vectors the frontend must reproduce
(canonical_vectors.json; regenerate with HYPERCHAT_REGEN_STEPUP_VECTORS=1 only for a deliberate format change)."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.stepup import canonical
from app.stepup.canonical import CanonicalError, b64url, build, canonical_json

VECTORS = Path(__file__).with_name("canonical_vectors.json")
ADMIN = "6f1c2d3e-4a5b-4c6d-8e7f-001122334455"
SESSION = "0a0b0c0d-1e1f-4a2b-8c3d-445566778899"
CASES = [
    {"name": "session ceremony", "action": "session.aal2", "target": "session", "params": {}},
    {"name": "register", "action": "mfa.register", "target": "self", "params": {}},
    {"name": "creator activation", "action": "creator.activate", "target": "creator:3f2b9a10-0c1d-4e5f-9a8b-7c6d5e4f3a2b",
     "params": {"expected_status": "pending", "reason": "KYC approved"}},
    {"name": "unicode and nesting", "action": "admin.user.change", "target": "user:9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a",
     "params": {"changes": {"role": "streamer", "is_active": False}, "note": "Grüße — 日本 ✓", "count": 9007199254740991,
                "list": [1, None, True, "a\"b\\c"]}},
]


def vector(case: dict) -> dict:
    built = build(rp_id="hyperchat.space", admin_id=ADMIN, session_id=SESSION, action=case["action"],
                  target=case["target"], params=case["params"], now=1_800_000_000, nonce="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8")
    return {**case, "rp_id": "hyperchat.space", "admin": ADMIN, "session_id": SESSION, "now": 1_800_000_000,
            "nonce": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", "canonical": built.canonical,
            "params_hash": built.params_hash, "challenge_b64url": b64url(built.challenge),
            "session_ref": canonical.session_ref(SESSION)}


def test_shared_vectors_are_current():
    current = [vector(case) for case in CASES]
    if os.environ.get("HYPERCHAT_REGEN_STEPUP_VECTORS") == "1":
        VECTORS.write_text(json.dumps({"domain": canonical.DOMAIN.decode(), "vectors": current}, indent=2,
                                      ensure_ascii=False) + "\n", encoding="utf-8")
    stored = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert stored == {"domain": "hyperchat-stepup-v1\n", "vectors": current}


def test_the_challenge_is_the_hash_of_the_exact_canonical_bytes():
    for v in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]:
        assert b64url(hashlib.sha256(b"hyperchat-stepup-v1\n" + v["canonical"].encode()).digest()) == v["challenge_b64url"]
        body = json.loads(v["canonical"])
        assert list(body) == sorted(body) and canonical_json(body) == v["canonical"]  # a fixed point: no other form
        assert body["params_hash"] == hashlib.sha256(canonical_json(body["params"]).encode()).hexdigest()
        assert body["session"] == hashlib.sha256(SESSION.encode()).hexdigest() and SESSION not in v["canonical"]
        assert body["expires_at"] - body["issued_at"] == canonical.CHALLENGE_TTL


def test_any_change_to_who_what_or_when_changes_the_challenge():
    base = dict(rp_id="hyperchat.space", admin_id=ADMIN, session_id=SESSION, action="creator.activate",
                target="creator:a", params={"expected_status": "pending"}, now=1, nonce="n")
    reference = build(**base).challenge
    for change in ({"admin_id": SESSION}, {"session_id": ADMIN}, {"action": "creator.reactivate"},
                   {"target": "creator:b"}, {"params": {"expected_status": "suspended"}}, {"params": {}},
                   {"now": 2}, {"nonce": "m"}, {"rp_id": "evil.example"}):
        assert build(**{**base, **change}).challenge != reference, change


@pytest.mark.parametrize("params", [
    {"x": 1.5}, {"x": float("nan")}, {"x": 2**53}, {"Bad": 1}, {"x" * 33: 1}, {"x": "\n"}, {"x": "\ud800"},
    {"x": b"bytes"}, {"x": {"a": {"b": {"c": {"d": {"e": 1}}}}}}, {"x": "y" * 513}, {1: 2},
])
def test_values_a_browser_could_serialize_differently_are_refused(params):
    with pytest.raises(CanonicalError):
        build(rp_id="r", admin_id=ADMIN, session_id=SESSION, action="creator.activate", target="t", params=params)


@pytest.mark.parametrize("action,target", [("creator.delete", "t"), ("", "t"), ("creator.activate", ""),
                                           ("creator.activate", "a b"), ("creator.activate", "x" * 201)])
def test_unknown_actions_and_malformed_targets_are_refused(action, target):
    with pytest.raises(CanonicalError):
        build(rp_id="r", admin_id=ADMIN, session_id=SESSION, action=action, target=target, params={})


def test_every_action_has_a_purpose_known_to_the_database():
    from importlib import util
    spec = util.spec_from_file_location("m0025", Path(__file__).parents[3] / "database/migrations/versions/0025_stepup_core.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.ACTIONS) == set(canonical.ACTIONS)
    assert set(canonical.ACTIONS.values()) == {"session", "register", "action"}
