"""Partner API (ClipWave, D4) end to end on PostgreSQL: x-api-key auth, signups, the creator check, admin key
management, and that no key ever reaches a log or a response. Synthetic partners are "authtest-*"."""

import logging
import secrets

import pytest

from app.security.crypto import sha256_hex
from tests.integration import DOMAIN, ORIGIN, db, make_streamer, new_browser, requires_db, signed_in, update_streamer

pytestmark = requires_db
SIGNUP, CHECK, KEYS = "/api/v1/partner/signups", "/api/v1/partner/streamers/check", "/api/v1/admin/partner-keys"
BAD_KEY = {"error": "Invalid or missing API key"}


def partner_key(slug: str = "authtest-cw", revoked: bool = False) -> str:
    key = f"hc_{slug}_{secrets.token_hex(32)}"
    with db() as conn:
        conn.execute("INSERT INTO public.partner_api_keys (partner_slug, key_hash, key_prefix, revoked) "
                     "VALUES (%s, %s, %s, %s)", (slug, sha256_hex(key), key[:12], revoked))
    return key


def body(**over) -> dict:
    return {"channel_name": "Clip Wave Tester", "mobile_number": "9876543210",
            "youtube_channel": "https://youtube.com/@clipwavetest", "email": f"cw-{secrets.token_hex(3)}{DOMAIN}",
            "instagram_handle": "@clipwave", **over}


def signups(slug: str = "authtest-cw") -> list[dict]:
    with db() as conn:
        cur = conn.execute("SELECT partner_slug, channel_name, mobile_number, youtube_channel, email, instagram_handle, "
                           "status, source_ip, user_agent FROM public.partner_signups WHERE partner_slug LIKE %s "
                           "ORDER BY created_at", (slug,))
        names = [c.name for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def exhaust(bucket: str, limit: int, partner: str = "authtest-cw") -> None:
    with db() as conn:
        conn.execute("INSERT INTO public.rate_limits (ip_address, endpoint, request_count, window_start) "
                     "VALUES (%s, %s, %s, now()) ON CONFLICT (ip_address, endpoint) DO UPDATE SET "
                     "request_count = EXCLUDED.request_count, window_start = now()", ("partner:" + partner, bucket, limit))


def counts() -> tuple:
    with db() as conn:
        return conn.execute("SELECT (SELECT count(*) FROM public.users), (SELECT count(*) FROM public.streamers), "
                            "(SELECT count(*) FROM public.user_signups)").fetchone()


# ── authentication ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path, payload", [(SIGNUP, None), (CHECK, {"channel_name": "x1"})])
def test_a_valid_partner_key_is_required(app, path, payload):
    revoked = partner_key("authtest-old", revoked=True)
    payload = payload or body()
    for headers in ({}, {"x-api-key": ""}, {"x-api-key": "short"}, {"x-api-key": "hc_" + "0" * 200},
                    {"x-api-key": "hc_authtest-cw_" + "0" * 64}, {"x-api-key": revoked},
                    {"authorization": "Bearer " + partner_key()}):
        r = app.post(path, json=payload, headers=headers)
        assert (r.status_code, r.json()) == (401, BAD_KEY), headers
    assert signups("authtest-%") == []


def test_a_key_works_for_both_routes_and_records_its_last_use(app):
    key = partner_key()
    assert app.post(CHECK, json={"channel_name": "nobody-here"}, headers={"x-api-key": key}).status_code == 200
    with db() as conn:
        assert conn.execute("SELECT last_used_at IS NOT NULL FROM public.partner_api_keys WHERE key_hash = %s",
                            (sha256_hex(key),)).fetchone()[0]


# ── signup ──────────────────────────────────────────────────────────────────────

def test_signup_is_recorded_for_the_key_partner_only(app):
    key = partner_key()
    before = counts()
    b = body(email=f"Mixed-Case{DOMAIN}")
    r = app.post(SIGNUP, json=b, headers={"x-api-key": key, "user-agent": "ClipWave/1.0"})
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    assert set(r.json()) == {"success", "id", "already_on_hyperchat"} and r.json()["success"] is True
    assert r.json()["already_on_hyperchat"] is False
    [row] = signups()
    assert row == {"partner_slug": "authtest-cw", "channel_name": "Clip Wave Tester", "mobile_number": "9876543210",
                   "youtube_channel": "https://youtube.com/@clipwavetest", "email": f"mixed-case{DOMAIN}",
                   "instagram_handle": "@clipwave", "status": "new", "source_ip": "testclient",
                   "user_agent": "ClipWave/1.0"}
    assert counts() == before  # no user, creator or organic signup is created


def test_duplicates_are_per_partner_and_case_insensitive(app):
    key, other = partner_key(), partner_key("authtest-other")
    b = body()
    assert app.post(SIGNUP, json=b, headers={"x-api-key": key}).status_code == 201
    r = app.post(SIGNUP, json={**b, "email": b["email"].upper(), "channel_name": "Changed"},
                 headers={"x-api-key": key})
    assert (r.status_code, r.json()) == (200, {"success": True, "duplicate": True, "already_on_hyperchat": False})
    assert app.post(SIGNUP, json=b, headers={"x-api-key": other}).status_code == 201  # another partner: its own row
    assert [r["channel_name"] for r in signups("authtest-%")] == ["Clip Wave Tester", "Clip Wave Tester"]


def test_the_partner_cannot_be_chosen_by_the_caller(app):
    key = partner_key()
    r = app.post(SIGNUP, json=body(partner_slug="authtest-other"), headers={"x-api-key": key})
    assert (r.status_code, r.json()) == (400, {"error": "Validation failed",
                                               "fields": {"partner_slug": "Unknown field."}})
    r = app.post(f"{SIGNUP}?partner_slug=authtest-other", json=body(), headers={"x-api-key": key})
    assert r.status_code == 201
    assert {row["partner_slug"] for row in signups("authtest-%")} == {"authtest-cw"}


def test_signup_validation_answers_like_legacy(app):
    key = {"x-api-key": partner_key()}
    r = app.post(SIGNUP, json=body(channel_name="x", mobile_number="12", email="nope", youtube_channel="https://x.y/z",
                                   instagram_handle="i" * 101, status="approved"), headers=key)
    assert r.status_code == 400 and r.json()["error"] == "Validation failed"
    assert set(r.json()["fields"]) == {"channel_name", "mobile_number", "email", "youtube_channel",
                                       "instagram_handle", "status"}
    r = app.post(SIGNUP, content=b"{not json", headers={**key, "content-type": "application/json"})
    assert (r.status_code, r.json()) == (400, {"error": "Invalid JSON body"})
    r = app.post(SIGNUP, content=b"[]", headers={**key, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["fields"] == {"body": "The body must be a JSON object."}
    r = app.post(SIGNUP, json=body(instagram_handle="i" * 11000), headers=key)
    assert (r.status_code, r.json()) == (413, {"error": "Request too large"})
    assert signups() == []


def test_signup_reports_an_existing_creator(app):
    make_streamer("authtest-cw-creator")
    r = app.post(SIGNUP, json=body(channel_name="AuthTest-CW-Creator"), headers={"x-api-key": partner_key()})
    assert r.status_code == 201 and r.json()["already_on_hyperchat"] is True  # "authtest-cw-creator" (dashed form)


def test_signups_are_rate_limited_per_partner(app):
    key, other = partner_key(), partner_key("authtest-other")
    exhaust("partner:signup", 60)
    r = app.post(SIGNUP, json=body(), headers={"x-api-key": key})
    assert (r.status_code, r.json()) == (429, {"error": "Too many requests. Please try again later."})
    assert app.post(SIGNUP, json=body(), headers={"x-api-key": other}).status_code == 201


# ── creator check ───────────────────────────────────────────────────────────────

def test_check_answers_only_a_boolean(app):
    key = {"x-api-key": partner_key()}
    sid = make_streamer("authtest-cw_exists", name="Secret Display Name")
    update_streamer(sid, status="pending")  # every status counts, as legacy
    for name in ("authtest-cw_exists", "  AuthTest-CW Exists ", "AUTHTEST-CW_EXISTS", "authtest-cw   exists!"):
        r = app.post(CHECK, json={"channel_name": name}, headers=key)
        assert r.json() == {"success": True, "exists": True,
                            "message": "This creator is already registered on HyperChat."}, name
        assert sid not in r.text and "Secret" not in r.text and "pending" not in r.text
    for name in ("authtest-cw_exist", "authtest-cw_exists_2", "authtestcwexists", "authtest_cw_exists"):
        r = app.post(CHECK, json={"channel_name": name}, headers=key)
        assert r.json() == {"success": True, "exists": False,
                            "message": "This creator is not registered on HyperChat yet."}, name
    make_streamer("authtest-cw-dashed")
    assert app.post(CHECK, json={"channel_name": "AuthTest-CW-Dashed"}, headers=key).json()["exists"] is True
    assert app.post(CHECK, json={"channel_name": "authtest%"}, headers=key).json()["exists"] is False  # no wildcards


@pytest.mark.parametrize("payload, status", [
    ({}, 400), ({"channel_name": ""}, 400), ({"channel_name": "x" * 101}, 400), ({"channel_name": "😀"}, 400),
    ({"channel_name": 7}, 400), ({"channel_name": "ok", "email": "x"}, 400),
])
def test_check_validation(app, payload, status):
    r = app.post(CHECK, json=payload, headers={"x-api-key": partner_key()})
    assert r.status_code == status and r.json()["error"] == "Validation failed"


def test_check_body_limit_and_rate_limit(app):
    key = {"x-api-key": partner_key()}
    r = app.post(CHECK, json={"channel_name": "x" * 3000}, headers=key)
    assert (r.status_code, r.json()) == (413, {"error": "Request too large"})
    exhaust("partner:check", 30)
    assert app.post(CHECK, json={"channel_name": "abc"}, headers=key).status_code == 429


# ── keys never leak ─────────────────────────────────────────────────────────────

def test_no_key_or_key_hash_in_logs_or_responses(app, caplog):
    caplog.set_level(logging.DEBUG)
    key = partner_key()
    responses = [app.post(SIGNUP, json=body(), headers={"x-api-key": key}),
                 app.post(CHECK, json={"channel_name": "abc"}, headers={"x-api-key": key}),
                 app.post(CHECK, json={"channel_name": "abc"}, headers={"x-api-key": key + "x"}),
                 app.post(SIGNUP, json={"x": 1}, headers={"x-api-key": key})]
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (key, sha256_hex(key), key + "x", sha256_hex(key + "x")):
        assert secret not in logged
        assert all(secret not in r.text for r in responses)
    assert "partner_signup_recorded" in logged and "authtest-cw" in logged  # audited by partner, not by key


# ── admin key management ────────────────────────────────────────────────────────

def test_admin_creates_lists_and_revokes_keys(app, caplog):
    caplog.set_level(logging.DEBUG)
    headers = signed_in(app, "partner-admin" + DOMAIN, role="admin")
    r = app.post(KEYS, json={"partner_slug": " AuthTest-New "}, headers=headers)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    api_key, key = r.json()["api_key"], r.json()["key"]
    assert api_key.startswith("hc_authtest-new_") and len(api_key) == len("hc_authtest-new_") + 64
    assert key["key_prefix"] == api_key[:12] and key["revoked"] is False and set(key) == {
        "id", "partner_slug", "key_prefix", "revoked", "last_used_at", "created_at"}
    with db() as conn:
        assert conn.execute("SELECT key_hash FROM public.partner_api_keys WHERE id = %s",
                            (key["id"],)).fetchone()[0] == sha256_hex(api_key)
    assert app.post(CHECK, json={"channel_name": "abc"}, headers={"x-api-key": api_key}).status_code == 200
    listed = app.get(KEYS)
    assert listed.status_code == 200 and key["id"] in listed.text
    assert api_key not in listed.text and sha256_hex(api_key) not in listed.text
    r = app.post(f"{KEYS}/{key['id']}/revoke", headers=headers)
    assert r.json()["changed"] is True and r.json()["key"]["revoked"] is True
    assert app.post(f"{KEYS}/{key['id']}/revoke", headers=headers).json()["changed"] is False
    assert app.post(CHECK, json={"channel_name": "abc"}, headers={"x-api-key": api_key}).status_code == 401
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert api_key not in logged and sha256_hex(api_key) not in logged
    assert "partner_key_created" in logged and "partner_key_revoked" in logged


def test_creating_a_key_keeps_the_partners_other_keys_valid(app):
    existing = partner_key("authtest-rot")
    headers = signed_in(app, "partner-admin2" + DOMAIN, role="admin")
    assert app.post(KEYS, json={"partner_slug": "authtest-rot"}, headers=headers).status_code == 201
    assert app.post(CHECK, json={"channel_name": "abc"}, headers={"x-api-key": existing}).status_code == 200


def test_key_management_is_admin_only_with_csrf_and_strict_input(app):
    assert new_browser(app).get(KEYS).status_code == 401
    owner_headers = signed_in(app, "partner-user" + DOMAIN)
    assert app.get(KEYS).status_code == 403
    assert app.post(KEYS, json={"partner_slug": "authtest-x"}, headers=owner_headers).status_code == 403
    headers = signed_in(app, "partner-admin3" + DOMAIN, role="admin")
    assert app.post(KEYS, json={"partner_slug": "authtest-x"}, headers=ORIGIN).status_code == 403  # no CSRF token
    for bad in ({"partner_slug": "x"}, {"partner_slug": "Bad Slug!"}, {"partner_slug": "a" * 51}):
        assert app.post(KEYS, json=bad, headers=headers).status_code in (400, 422), bad
    assert app.post(KEYS, json={"partner_slug": "authtest-x", "key_hash": "0" * 64}, headers=headers).status_code == 422
    for bad_id in ("not-a-uuid", "00000000-0000-4000-8000-000000000000"):
        assert app.post(f"{KEYS}/{bad_id}/revoke", headers=headers).status_code == 404
