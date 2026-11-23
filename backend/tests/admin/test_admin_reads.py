"""Admin read endpoints (creators, donations, payment integrity, jobs) through the real app (scratch database)."""

import secrets
import uuid
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from tests.integration import DOMAIN, db, make_donation, make_streamer, new_browser, requires_db, signed_in
from tests.payments.test_integrity_db import dispute, paid
from tests.payments.test_payments_db import client, rz_env  # noqa: F401 (fixtures)

pytestmark = requires_db
A = "/api/v1/admin"
ENDPOINTS = ("/creators", "/donations", "/payments/integrity", "/jobs")
SECRET_WORDS = ("token", "secret", "hash", "key", "password")


def _admin(app) -> dict:
    return signed_in(app, "boss" + DOMAIN, role="admin")


def _names(value):
    """Every key name in a JSON document, at any depth."""
    if isinstance(value, dict):
        for k, child in value.items():
            yield k
            yield from _names(child)
    elif isinstance(value, list):
        for child in value:
            yield from _names(child)


def ok(app, path: str, **params) -> dict:
    """A 200 answer, `no-store`, with no field named like a secret (dedupe_key is a job's subject id)."""
    r = app.get(A + path, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    bad = [n for n in _names(body) if n != "dedupe_key" and any(w in n.lower() for w in SECRET_WORDS)]
    assert not bad, bad
    return body


def test_reads_need_an_admin(app):
    for path in ENDPOINTS:
        assert new_browser(app).get(A + path).status_code == 401, path
    plain = new_browser(app)
    signed_in(plain, "plain" + DOMAIN)
    for path in ENDPOINTS:
        assert plain.get(A + path).status_code == 403, path
    _admin(app)
    for path, params, code in (("/creators", {"limit": 201}, 422), ("/donations", {"limit": 201}, 422),
                               ("/jobs", {"limit": 201}, 422), ("/creators", {"status": "weird"}, 422),
                               ("/jobs", {"status": "weird"}, 422), ("/jobs", {"kind": "weird"}, 400),
                               ("/payments/integrity", {"since": "yesterday"}, 422),
                               ("/donations", {"cursor": "garbage"}, 400)):
        assert app.get(A + path, params=params).status_code == code, (path, params)


def test_creators_search_filter_and_paging(app):
    headers = _admin(app)
    make_streamer("authtest-ar-c1", name="Alpha")
    make_streamer("authtest-ar-c2", name="Beta")
    app.app_state.admin.payment_accounts = frozenset({"primary"})
    r = app.post(f"{A}/creators", json={"slug": "authtest-ar-c3", "name": "Gamma", "razorpay_account": "primary"},
                 headers=headers)
    assert r.status_code == 201, r.text
    body = ok(app, "/creators", q="authtest-ar-c")
    assert body["total"] == 3 and [c["slug"] for c in body["creators"]] == ["authtest-ar-c1", "authtest-ar-c2",
                                                                           "authtest-ar-c3"]
    assert {c["status"] for c in body["creators"]} == {"active", "pending"}
    assert set(body["creators"][0]) == {"id", "slug", "name", "status", "razorpay_account", "pusher_group",
                                        "order_prefix", "brand_color", "moderation_mode", "claimed", "created_at"}
    assert [c["slug"] for c in ok(app, "/creators", q="BETA")["creators"]] == ["authtest-ar-c2"]  # name, any case
    pending = ok(app, "/creators", q="authtest-ar-c", status="pending")
    assert pending["total"] == 1 and pending["creators"][0]["slug"] == "authtest-ar-c3"
    page = ok(app, "/creators", q="authtest-ar-c", limit=1, offset=1)
    assert (page["total"], [c["slug"] for c in page["creators"]]) == (3, ["authtest-ar-c2"])
    assert ok(app, "/creators", q="%")["total"] == 0  # LIKE wildcards are escaped


def test_donations_across_creators_keyset_paged(app):
    _admin(app)
    one, two = make_streamer("authtest-ar-d1"), make_streamer("authtest-ar-d2")
    at = [datetime(2099, 1, d, tzinfo=UTC) for d in (1, 2, 3, 4)]  # newer than any other row: first in the list
    first = make_donation(one, 100, payment="success", at=at[0])
    second = make_donation(two, 200, payment="success", at=at[1])
    third = make_donation(one, 300, payment="success", at=at[2])
    make_donation(two, 400, payment="pending", at=at[3])  # unpaid: never listed
    body = ok(app, "/donations", limit=3)
    assert [(d["donation_id"], d["streamer_slug"]) for d in body["donations"]] == [
        (third, "authtest-ar-d1"), (second, "authtest-ar-d2"), (first, "authtest-ar-d1")]
    assert body["donations"][0]["net_inr"] == 300 and body["donations"][0]["reversal_flag"] is None
    seen, cursor = [], None
    while True:
        page = ok(app, "/donations", streamer="authtest-ar-d1", limit=1, **({"cursor": cursor} if cursor else {}))
        seen += [d["donation_id"] for d in page["donations"]]
        cursor = page["next_cursor"]
        assert page["has_more"] is (cursor is not None)
        if cursor is None:
            break
    assert seen == [third, first]
    assert ok(app, "/donations", streamer="authtest-ar-none")["donations"] == []


def test_payment_integrity_report_flags_attention_and_leaves_the_pool_writable(client):
    paid_sid, out, payment = paid(client, "authtest-ar-int")
    dispute(client, "payment.dispute.lost", out, payment.id, "authtest-ar-int-1", ref="disp_TESTar1",
            amount_deducted=10000)
    headers = _admin(client)
    body = ok(client, "/payments/integrity")
    item = next(i for i in body["items"] if i["provider_ref"] == "disp_TESTar1")
    assert (item["status"], item["streamer_slug"], item["match_type"], item["attention"]) == (
        "lost", "authtest-ar-int", "payment", True)
    assert body["read_only"] is True and body["needs_attention"] is True and body["attention"] >= 1
    assert set(body["accounting"]["counts"]) == {"held", "review_both", "imported_with_evidence"}
    assert "missing_refund_evidence" in body["refund_snapshots"]["counts"]
    assert ok(client, "/payments/integrity", since="2099-01-01")["items"] == []
    # The report's READ ONLY session never reaches the pool: pooled writes still work afterwards.
    for i in range(10):
        ok(client, "/payments/integrity")
        r = client.put(f"{A}/creators/{paid_sid}/soundboard", json={"enabled": i % 2 == 0}, headers=headers)
        assert r.status_code == 200, r.text


def _job(dedupe: str, status: str, completed: datetime | None, kind: str = "donation_audio",
         marker: str = "") -> None:
    with db() as conn:
        conn.execute("INSERT INTO public.jobs (kind, dedupe_key, payload, status, attempts, max_attempts, last_error, "
                     "result, completed_at) VALUES (%s, %s, %s, %s, 4, 4, 'provider_error', %s, %s)",
                     (kind, dedupe, Jsonb({"donation_id": str(uuid.uuid4())}), status, Jsonb({"text": marker}),
                      completed))


def test_jobs_filters_paging_and_no_bodies(app):
    _admin(app)
    tag, marker = "authtest-ar-" + secrets.token_hex(4), "private donor text " + secrets.token_hex(4)
    for day in (1, 2, 3):
        _job(f"{tag}-f{day}", "failed", datetime(2099, 1, day, tzinfo=UTC), marker=marker)
    _job(f"{tag}-v", "failed", datetime(2099, 1, 4, tzinfo=UTC), kind="voice_transcription", marker=marker)
    _job(f"{tag}-s", "succeeded", datetime(2099, 1, 5, tzinfo=UTC), marker=marker)
    body = ok(app, "/jobs", kind="donation_audio", limit=2)  # default status: failed
    assert [j["dedupe_key"] for j in body["jobs"]] == [f"{tag}-f3", f"{tag}-f2"]
    assert set(body["jobs"][0]) == {"id", "kind", "status", "attempts", "max_attempts", "last_error", "dedupe_key",
                                    "created_at", "started_at", "completed_at"}
    assert [j["dedupe_key"] for j in ok(app, "/jobs", kind="donation_audio", limit=1, offset=2)["jobs"]] == [
        f"{tag}-f1"]
    assert ok(app, "/jobs", limit=1)["jobs"][0]["dedupe_key"] == f"{tag}-v"
    assert ok(app, "/jobs", status="succeeded", limit=1)["jobs"][0]["dedupe_key"] == f"{tag}-s"
    for params in ({}, {"status": "succeeded"}, {"kind": "voice_transcription"}):
        r = app.get(f"{A}/jobs", params=params)
        assert marker not in r.text and "payload" not in r.text and '"result"' not in r.text
