"""Cleanup tooling against a scratch database only (it deletes rows it did not create)."""

import asyncio
import json

from app.cli import connection_factory, main
from app.core.config import Settings
from app.maintenance import cleanup
from tests.integration import DOMAIN, db, make_user, requires_db

pytestmark = requires_db


def _seed() -> dict[str, list[str]]:
    """One expired and one live row per table; returns {table: [expired_marker, live_marker]}."""
    uid = make_user("cleanup" + DOMAIN)
    with db() as conn:
        def ins(sql, *params):
            return conn.execute(sql, params).fetchone()[0]

        sessions = [
            ins("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at, "
                "revoked_at) VALUES (%s, 'authtest-s1', 'x', now() + interval '1 day', now() + interval '20 days', "
                "now() - interval '8 days') RETURNING token_hash", uid),
            ins("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at) "
                "VALUES (%s, 'authtest-s2', 'x', now() - interval '9 days', now() + interval '20 days') "
                "RETURNING token_hash", uid),
            ins("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at, "
                "revoked_at) VALUES (%s, 'authtest-s3', 'x', now() + interval '1 day', now() + interval '20 days', "
                "now() - interval '1 day') RETURNING token_hash", uid),  # recently revoked: kept (grace period)
            ins("INSERT INTO public.sessions (user_id, token_hash, csrf_token_hash, idle_expires_at, absolute_expires_at) "
                "VALUES (%s, 'authtest-s4', 'x', now() + interval '1 day', now() + interval '20 days') "
                "RETURNING token_hash", uid),
        ]
        resets = [
            ins("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) VALUES "
                "(%s, 'authtest-r1', now() - interval '2 days') RETURNING token_hash", uid),
            ins("INSERT INTO public.password_reset_tokens (user_id, token_hash, expires_at) VALUES "
                "(%s, 'authtest-r2', now() + interval '1 hour') RETURNING token_hash", uid),
        ]
        nonces = [
            ins("INSERT INTO public.request_nonces (nonce, expires_at) VALUES ('authtest-n1', now() - interval '1 second') "
                "RETURNING nonce"),
            ins("INSERT INTO public.request_nonces (nonce, expires_at) VALUES ('authtest-n2', now() + interval '1 hour') "
                "RETURNING nonce"),
        ]
        limits = [
            ins("INSERT INTO public.rate_limits (ip_address, endpoint, window_start) VALUES "
                "('authtest-ip', 'auth:cleanup-old', now() - interval '2 days') RETURNING endpoint"),
            ins("INSERT INTO public.rate_limits (ip_address, endpoint, window_start) VALUES "
                "('authtest-ip', 'auth:cleanup-new', now()) RETURNING endpoint"),
        ]
        invites = [
            ins("INSERT INTO public.user_invites (email, token_hash, expires_at) VALUES "
                "('old-invite' || %s, 'authtest-i1', now() - interval '31 days') RETURNING token_hash", DOMAIN),
            ins("INSERT INTO public.user_invites (email, token_hash, expires_at, revoked_at) VALUES "
                "('revoked-invite' || %s, 'authtest-i2', now() + interval '1 day', now() - interval '31 days') "
                "RETURNING token_hash", DOMAIN),
            ins("INSERT INTO public.user_invites (email, token_hash, expires_at, accepted_at) VALUES "
                "('accepted-invite' || %s, 'authtest-i3', now() - interval '90 days', now() - interval '91 days') "
                "RETURNING token_hash", DOMAIN),  # accepted: kept as an audit record
            ins("INSERT INTO public.user_invites (email, token_hash, expires_at) VALUES "
                "('recent-invite' || %s, 'authtest-i4', now() - interval '1 day') RETURNING token_hash", DOMAIN),
        ]
        # Phase 8: finished jobs older than 30 days go; recent or still active jobs stay. The jobs' dedupe keys
        # start with "authtest-" (removed by the shared cleanup too).
        for key, status, age in (("authtest-j1", "succeeded", 31), ("authtest-j2", "failed", 40),
                                 ("authtest-j3", "succeeded", 1), ("authtest-j4", "queued", 60)):
            conn.execute("INSERT INTO public.jobs (kind, dedupe_key, payload, status, max_attempts, completed_at, "
                         "created_at) VALUES ('donation_audio', %s, '{}'::jsonb, %s, 4, "
                         "CASE WHEN %s <> 'queued' THEN now() - make_interval(days => %s) END, "
                         "now() - make_interval(days => %s))", (key, status, status, age, age))
        # TTS moderation audit: 90 days.
        for marker, age in (("authtest-a1", 91), ("authtest-a2", 89)):
            conn.execute("INSERT INTO public.tts_moderation_audit (supporter_identity_hash, blocked, rule_id, "
                         "moderation_version, normalization_version, created_at) VALUES (%s, true, 'x', 'v', 'v', "
                         "now() - make_interval(days => %s))", (marker, age))
    return {"sessions": sessions, "resets": resets, "nonces": nonces, "limits": limits, "invites": invites}


def _left() -> dict[str, set[str]]:
    with db() as conn:
        q = lambda sql: {r[0] for r in conn.execute(sql)}  # noqa: E731
        return {
            "sessions": q("SELECT token_hash FROM public.sessions WHERE token_hash LIKE 'authtest-%'"),
            "resets": q("SELECT token_hash FROM public.password_reset_tokens WHERE token_hash LIKE 'authtest-%'"),
            "nonces": q("SELECT nonce FROM public.request_nonces WHERE nonce LIKE 'authtest-%'"),
            "limits": q("SELECT endpoint FROM public.rate_limits WHERE endpoint LIKE 'auth:cleanup-%'"),
            "invites": q("SELECT token_hash FROM public.user_invites WHERE token_hash LIKE 'authtest-%'"),
            "jobs": q("SELECT dedupe_key FROM public.jobs WHERE dedupe_key LIKE 'authtest-j%'"),
            "audit": q("SELECT supporter_identity_hash FROM public.tts_moderation_audit "
                       "WHERE supporter_identity_hash LIKE 'authtest-a%'"),
        }


def test_cleanup_dry_run_then_delete_only_expired(strict_scratch_db, env):
    factory = connection_factory(Settings())
    asyncio.run(cleanup(factory, dry_run=False))  # start from a clean slate
    _seed()
    counts = asyncio.run(cleanup(factory, dry_run=True))
    assert counts == {"sessions": 2, "password_reset_tokens": 1, "request_nonces": 1, "rate_limits": 1,
                      "user_invites": 2, "jobs": 2, "tts_moderation_audit": 1}
    assert len(_left()["sessions"]) == 4  # dry run deleted nothing
    assert asyncio.run(cleanup(factory, dry_run=False)) == counts
    assert _left() == {
        "sessions": {"authtest-s3", "authtest-s4"}, "resets": {"authtest-r2"}, "nonces": {"authtest-n2"},
        "limits": {"auth:cleanup-new"}, "invites": {"authtest-i3", "authtest-i4"},
        "jobs": {"authtest-j3", "authtest-j4"}, "audit": {"authtest-a2"},
    }
    assert set(asyncio.run(cleanup(factory, dry_run=True)).values()) == {0}


def test_cleanup_command_prints_counts_only(strict_scratch_db, env, capsys):
    _seed()
    assert main(["cleanup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would delete" in out and "sessions" in out and "authtest" not in out and DOMAIN not in out
    assert main(["cleanup"]) == 0
    out = capsys.readouterr().out
    assert "deleted" in out and "authtest" not in out and DOMAIN not in out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    audit = [e for e in events if e.get("security_event") == "maintenance_cleanup"]
    assert len(audit) == 1 and audit[0]["logger"] == "hyperchat.security" and audit[0]["count"] >= 1
