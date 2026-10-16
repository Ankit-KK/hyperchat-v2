"""Didit v3 contract pieces without a database: the X-Signature-V2 canonical JSON (independent vectors; the database
verifies the signature over it, tests/kyc/test_kyc_db.py), hosted-URL validation, the minimal check statuses kept, and
the HTTP client (mock transport)."""

import asyncio
import json

import httpx
import pytest

from app.kyc import didit

# Hand-written canonical forms (independent of didit.canonical): sorted keys at every depth, arrays in order,
# whole floats as integers, other floats kept, Unicode not escaped, compact separators.
VECTORS = [
    ({"b": 1.0, "a": "é", "c": [2.5, {"y": 1, "x": None}]}, '{"a":"é","b":1,"c":[2.5,{"x":null,"y":1}]}'),
    ({"amount": 100.0, "nested": {"z": [1.0, 0.5, -3.0], "a": True}}, '{"amount":100,"nested":{"a":true,"z":[1,0.5,-3]}}'),
    ({"name": "Ravi शर्मा 🎉", "list": ["b", "a"]}, '{"list":["b","a"],"name":"Ravi शर्मा 🎉"}'),
    ({"big": 12345678901234567890, "neg": -0.0, "e": 1e2}, '{"big":12345678901234567890,"e":100,"neg":0}'),
]


@pytest.mark.parametrize("body, text", VECTORS)
def test_canonical_json_matches_the_v2_rules(body, text):
    assert didit.canonical(body) == text.encode("utf-8")


def test_canonical_form_of_a_parsed_delivery_equals_its_signed_form():
    """What the server sees: Didit signs its object; the delivery's bytes may be ordered or spaced differently."""
    body = {"status": "Approved", "timestamp": 1774970000, "decision": {"score": 99.0, "név": "x"}}
    raw = json.dumps(body, indent=2, ensure_ascii=True).encode()  # escaped Unicode, spaces, other key order
    assert didit.canonical(json.loads(raw)) == didit.canonical(body)


@pytest.mark.parametrize("change", [
    lambda b: b.update(status="Approved"), lambda b: b["decision"].update(score=98),
    lambda b: b.update(extra=1), lambda b: b.pop("vendor_data"), lambda b: b.update(timestamp=1774970001),
])
def test_any_change_to_the_body_changes_the_signed_text(change):
    body = {"status": "Declined", "vendor_data": "u", "timestamp": 1774970000, "decision": {"score": 1}}
    before = didit.canonical(body)
    change(body)
    assert didit.canonical(body) != before


@pytest.mark.parametrize("broken", [
    lambda b: json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=True),      # escapes Unicode
    lambda b: json.dumps(b, sort_keys=True, separators=(",", ":"), ensure_ascii=False).replace(":1,", ":1.0,"),
    lambda b: json.dumps(b, separators=(",", ":"), ensure_ascii=False),                     # keys not sorted
    lambda b: json.dumps(b, sort_keys=True, ensure_ascii=False),                            # spaced separators
])
def test_broken_canonicalizations_differ(broken):
    """A server that canonicalized differently would sign different text and refuse Didit's genuine deliveries."""
    body = {"z": "é", "a": 1.0, "m": [1.0, "x"]}
    assert broken(didit._shorten(body)).encode("utf-8") != didit.canonical(body)


@pytest.mark.parametrize("url, ok", [
    ("https://verify.didit.me/session/abc", True), ("https://didit.me/x", True),
    ("http://verify.didit.me/session/abc", False), ("https://evil.example/didit.me", False),
    ("https://verify.didit.me.evil.example/x", False), ("https://user:pw@verify.didit.me/x", False),
    ("https://verify.didit.me:8443/x", False), ("javascript:alert(1)", False), (None, False),
])
def test_only_https_didit_hosted_urls_are_returned(url, ok):
    assert didit.hosted_url_ok(url) is ok


def test_only_check_statuses_are_kept_from_a_decision():
    decision = {"id_verifications": [{"status": "Approved", "document_number": "X123", "first_name": "Asha",
                                      "date_of_birth": "1990-01-01", "portrait_image": "https://x"}],
                "liveness_checks": [{"status": "Approved", "reference_image": "https://x"}],
                "face_matches": "not-a-list", "aml_screenings": [{"status": "In Review", "hits": [{"name": "A"}]}],
                "ip_analyses": [{"status": "Approved", "ip_address": "1.2.3.4"}]}
    kept = didit.check_statuses(decision)
    assert kept == {"id_verifications": ["Approved"], "liveness_checks": ["Approved"],
                    "aml_screenings": ["In Review"]}
    assert not {"X123", "Asha", "1990", "1.2.3.4", "https://x"} & set(json.dumps(kept).split('"'))


# ── the HTTP client (mock transport: no real call) ────────────────────────────────

WF = "6ff16570-fe12-4212-9647-b12b8898cc09"
USER = "11111111-2222-4333-8444-555555555555"
SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _client(handler) -> didit.DiditClient:
    client = didit.DiditClient(base="https://verification.didit.me", api_key="test-key", workflow_id=WF)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_create_session_request_and_response_contract():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), key=request.headers.get("x-api-key"), body=json.loads(request.content))
        return httpx.Response(201, json={"session_id": SID, "url": "https://verify.didit.me/session/tok",
                                         "status": "Not Started", "vendor_data": USER, "workflow_id": WF,
                                         "workflow_version": 3, "session_token": "secret-token"})

    created = asyncio.run(_client(handler).create_session(USER, "https://app.example.test/kyc/return"))
    assert seen == {"url": "https://verification.didit.me/v3/session/", "key": "test-key",
                    "body": {"workflow_id": WF, "vendor_data": USER, "callback": "https://app.example.test/kyc/return"}}
    assert created == didit.CreatedSession(SID, "https://verify.didit.me/session/tok", "Not Started", 3)


@pytest.mark.parametrize("override", [{"url": "https://evil.example/s"}, {"vendor_data": "someone-else"},
                                      {"workflow_id": "00000000-0000-4000-8000-000000000000"},
                                      {"status": "Bogus"}, {"session_id": "not-a-uuid"}])
def test_unexpected_session_responses_are_refused(override):
    good = {"session_id": SID, "url": "https://verify.didit.me/session/t", "status": "Not Started",
            "vendor_data": USER, "workflow_id": WF}
    client = _client(lambda request: httpx.Response(201, json={**good, **override}))
    with pytest.raises(didit.DiditError, match="unexpected_response"):
        asyncio.run(client.create_session(USER, "https://app.example.test/kyc/return"))


@pytest.mark.parametrize("response, reason", [
    (httpx.Response(401), "credentials"), (httpx.Response(403), "credentials"), (httpx.Response(429), "rate_limited"),
    (httpx.Response(500), "http_500"), (httpx.Response(200, content=b"not json"), "bad_response"),
])
def test_provider_failures_are_reason_codes_only(response, reason):
    with pytest.raises(didit.DiditError) as exc:
        asyncio.run(_client(lambda request: response).create_session(USER, "https://x.example/k"))
    assert exc.value.reason == reason and "test-key" not in str(exc.value)


def test_a_timeout_is_a_reason_code():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(didit.DiditError, match="timeout"):
        asyncio.run(_client(handler).decision(SID))
