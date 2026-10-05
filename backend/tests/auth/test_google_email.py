"""Google id_token verification (TEST-ONLY RSA keys, mocked Google endpoints) and email rendering."""

import logging
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.email import DisabledSender, ResendSender, render
from app.auth.google import GoogleAuthError, GoogleOAuth, pkce_challenge

CLIENT_ID = "test-client.apps.googleusercontent.invalid"
NONCE = "test-nonce"


def _keypair(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


PRIVATE, JWK = _keypair("g-kid-1")
OTHER_PRIVATE, _ = _keypair("g-kid-1")


def _id_token(private=PRIVATE, kid="g-kid-1", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567890",
              "email": "streamer@example.invalid", "email_verified": True, "nonce": NONCE,
              "iat": now, "exp": now + 300}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, private, algorithm="RS256",
                      headers={"kid": kid})


def _google(token_response=None, jwks=None, calls=None) -> GoogleOAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json=jwks or {"keys": [JWK]})
        if request.url.host == "oauth2.googleapis.com":
            return token_response or httpx.Response(200, json={"id_token": _id_token()})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleOAuth(CLIENT_ID, "TEST-ONLY-client-secret", "https://api.example.invalid/cb", client=client)


def test_authorization_url_uses_pkce_state_and_nonce():
    url = _google().authorization_url("st", "nn", "verifier-123")
    q = parse_qs(urlparse(url).query)
    assert q["state"] == ["st"] and q["nonce"] == ["nn"] and q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [pkce_challenge("verifier-123")]
    assert q["scope"] == ["openid email profile"] and "client_secret" not in q


@pytest.mark.anyio
async def test_exchange_returns_verified_identity_and_sends_verifier():
    calls = []
    identity = await _google(calls=calls).exchange("auth-code", "verifier-123", NONCE)
    assert identity.subject == "1234567890" and identity.email == "streamer@example.invalid"
    token_call = next(c for c in calls if c.url.host == "oauth2.googleapis.com")
    form = parse_qs(token_call.content.decode())
    assert form["code_verifier"] == ["verifier-123"] and form["grant_type"] == ["authorization_code"]


@pytest.mark.anyio
@pytest.mark.parametrize("overrides,reason", [
    ({"nonce": "other"}, "nonce_mismatch"),
    ({"aud": "someone-else"}, "invalid_id_token"),
    ({"iss": "https://evil.invalid"}, "bad_issuer"),
    ({"exp": int(time.time()) - 10}, "id_token_expired"),
    ({"email_verified": False}, "email_not_verified"),
    ({"email": None}, "email_not_verified"),
])
async def test_bad_claims_rejected(overrides, reason):
    with pytest.raises(GoogleAuthError) as err:
        await _google().verify_id_token(_id_token(**overrides), NONCE)
    assert err.value.reason == reason


@pytest.mark.anyio
async def test_wrong_key_and_unknown_kid_rejected():
    with pytest.raises(GoogleAuthError) as err:
        await _google().verify_id_token(_id_token(private=OTHER_PRIVATE), NONCE)
    assert err.value.reason == "invalid_id_token"
    with pytest.raises(GoogleAuthError) as err:
        await _google().verify_id_token(_id_token(kid="g-kid-unknown"), NONCE)
    assert err.value.reason == "unknown_signing_key"
    with pytest.raises(GoogleAuthError) as err:
        await _google().verify_id_token("not-a-jwt", NONCE)
    assert err.value.reason == "malformed_id_token"


@pytest.mark.anyio
async def test_token_endpoint_failure_is_reported_without_details():
    with pytest.raises(GoogleAuthError) as err:
        await _google(token_response=httpx.Response(400, json={"error": "invalid_grant"})).exchange("c", "v", NONCE)
    assert err.value.reason == "token_endpoint_status_400"


def test_render_escapes_link_into_existing_templates():
    url = "https://hyperchat.example.invalid/reset-password?token=abc&x=<y>"
    for template in ("reset-password", "invite"):
        email = render(template, "a@example.invalid", url)
        assert "{{ .ConfirmationURL }}" not in email.html
        assert "token=abc&amp;x=&lt;y&gt;" in email.html and url in email.text


@pytest.mark.anyio
async def test_disabled_sender_never_logs_the_link(caplog):
    caplog.set_level(logging.DEBUG)
    await DisabledSender().send(render("reset-password", "a@example.invalid", "https://x.invalid/?token=SECRET123"))
    assert "SECRET123" not in caplog.text
    assert all("SECRET123" not in str(r.__dict__) for r in caplog.records)


@pytest.mark.anyio
async def test_resend_sender_posts_and_fails_closed():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200 if len(seen) == 1 else 500, json={})

    sender = ResendSender("TEST-ONLY-resend-key", "HyperChat <no-reply@example.invalid>",
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await sender.send(render("invite", "a@example.invalid", "https://x.invalid/invite?token=t"))
    assert seen[0].headers["authorization"] == "Bearer TEST-ONLY-resend-key"
    with pytest.raises(RuntimeError):
        await sender.send(render("invite", "a@example.invalid", "https://x.invalid/invite?token=t"))
