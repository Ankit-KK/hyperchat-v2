"""Decision rules and fixtures of the DEV smoke suite (smoke_dev_backend.py), importable without side effects so
that backend/tests/test_smoke_harness.py can test them with fakes and a disposable database.

Strict rehearsal mode (HYPERCHAT_SMOKE_EXPECT_HEAD, e.g. 0028; docs/operations/dev-rehearsal-0020-0028.md):
  * the database must be at exactly that head (a known one), otherwise FAIL;
  * BLOCKED is allowed only for external integrations or approvals that are deliberately unavailable on DEV
    (blocked(..., external=True)); any other BLOCKED, e.g. "not deployed on DEV yet", becomes a FAIL.
Without it the suite keeps its earlier behaviour: any known head passes, undeployed features report BLOCKED.
"""

import hashlib
import json
import re
from datetime import datetime

# Every Alembic revision a DEV database may legitimately be at; anything else FAILs.
KNOWN_HEADS = ("0017", "0018", "0019", "0020", "0021", "0022", "0023", "0024", "0025", "0026", "0027", "0028",
               "0029")
# 0018 D6, 0019 D13, 0020-0023 KYC PR1-PR4, 0024 admin auth boundary, 0025-0028 admin step-up (S1-S3, A5),
# 0029 payment integrity evidence (C1)
PRODUCTION_DATABASES = ("hyperchat", "hyperchat_restore_test")
PRODUCTION_HOSTS = ("api.hyperchat.space", "hyperchat.space")


class SmokeError(Exception):
    """A refused configuration (exit 2)."""


def parse_expect_head(value: str | None) -> str | None:
    """HYPERCHAT_SMOKE_EXPECT_HEAD: unset/empty = no rehearsal; otherwise it must name a known head."""
    if not value:
        return None
    if value not in KNOWN_HEADS:
        raise SmokeError(f"HYPERCHAT_SMOKE_EXPECT_HEAD={value!r} is not a known migration head")
    return value


def refusal(db_name: str, api: str, local_health: dict, local: str = "") -> str | None:
    """Why the suite must not run here (None: it may). Never production, never a non-development API."""
    if db_name in PRODUCTION_DATABASES:
        return "never against the production or restore database"
    for url in (api, local):
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        if host in PRODUCTION_HOSTS:
            return "never against the production API"
    if local_health.get("environment") != "development":
        return "the API is not the development environment"
    return None


# Sections gated on the schema: the revision that brought each feature (database/migrations/versions). A gate is
# always "at or after" that revision, so the features stay tested at every later head (0028 included).
FEATURE_HEADS = {
    "payments-account": "0018",  # D6: 0018_retire_secondary_razorpay
    "owner-transfer": "0019",    # D13: 0019_owner_specific_soundboard_terms
    "mfa": "0025",               # admin step-up: 0025_stepup_core .. 0028
}


def head_at_least(head: str, revision: str) -> bool:
    """True when the database is at a known head at or after `revision` (Alembic order, not string order). A feature
    that arrived with a revision is present at every later one: never test for an exact older head."""
    return head in KNOWN_HEADS and KNOWN_HEADS.index(head) >= KNOWN_HEADS.index(revision)


def head_check(head: str, expect: str | None) -> tuple[bool, str]:
    """(ok, label). Unknown heads always fail; in a rehearsal only the expected head passes."""
    if head not in KNOWN_HEADS:
        return False, f"database at an unknown migration head ({head!r})"
    if expect is not None and head != expect:
        return False, f"database at {head}, the rehearsal expects {expect}"
    return True, f"database at a known migration head ({head})" + (" = rehearsal target" if expect else "")


class Results:
    """PASS / FAIL / BLOCKED rows, the printed lines, and the exit code (1 when any FAIL)."""

    def __init__(self, strict: bool, out=print):
        self.strict, self.rows, self.out = strict, [], out

    def check(self, section: str, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.rows.append((section, name, "PASS" if ok else "FAIL", "" if ok else detail[:300]))
        self.out(f"{'PASS' if ok else 'FAIL'}  [{section}] {name}" + ("" if ok else f"  -- {detail[:300]}"))
        return ok

    def blocked(self, section: str, name: str, why: str, external: bool = False) -> None:
        if self.strict and not external:  # a rehearsal never accepts a missing core feature as BLOCKED
            self.check(section, name, False, f"BLOCKED is not allowed in the rehearsal: {why}")
            return
        self.rows.append((section, name, "BLOCKED", why))
        self.out(f"BLOCKED [{section}] {name}  -- {why}")

    def counts(self) -> tuple[int, int, int]:
        return tuple(sum(1 for r in self.rows if r[2] == k) for k in ("PASS", "FAIL", "BLOCKED"))

    def failed(self) -> list[tuple[str, str, str, str]]:
        return [r for r in self.rows if r[2] == "FAIL"]

    def exit_code(self) -> int:
        return 1 if self.failed() or not self.rows else 0


# ── admin MFA / step-up (migrations 0025-0028) without a verifier ───────────────────────────────────────────

def mfa_status_ok(status: int, body) -> tuple[bool, str]:
    """GET /api/v1/auth/mfa/status as an admin, verifier not installed, enforcement draft (app.stepup.service.status):
    200 {"enforcement": "draft", "aal": <2, "mfa_required": false, "verifier": "not_configured", "credentials": null}."""
    want = {"enforcement": "draft", "mfa_required": False, "verifier": "not_configured", "credentials": None}
    if status != 200 or not isinstance(body, dict):
        return False, f"{status} {str(body)[:200]}"
    aal = body.get("aal")
    ok = set(body) == {*want, "aal"} and all(body[k] == v for k, v in want.items()) \
        and isinstance(aal, int) and not isinstance(aal, bool) and aal < 2
    return ok, str(body)


def ceremony_refused_safely(status: int, body) -> tuple[bool, str]:
    """A passkey ceremony without a verifier: exactly 503 with reason stepup_not_configured (app.routers.mfa)."""
    ok = status == 503 and isinstance(body, dict) and body.get("reason") == "stepup_not_configured"
    return ok, f"{status} {str(body)[:200]}"


def enforcement_draft(kyc_row: str, mfa_row: str) -> tuple[bool, str]:
    """Rows read with psql -At: kyc_enforcement 'state|generation', stepup_enforcement 'state'."""
    return kyc_row == "draft|0" and mfa_row == "draft", f"kyc={kyc_row!r} mfa={mfa_row!r}"


# ── the DEV email and Google configuration the suites must expect (read from api.env, names listed here only) ──
# Only these non-secret settings are ever read; client secrets and API keys are never parsed or printed.
CONFIG_NAMES = ("HYPERCHAT_EMAIL_BACKEND", "HYPERCHAT_EMAIL_ACTIVE_CATEGORIES", "HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST",
                "HYPERCHAT_GOOGLE_CLIENT_ID", "HYPERCHAT_GOOGLE_REDIRECT_URI")


def config_values(env_text: str) -> dict[str, str]:
    """The CONFIG_NAMES settings of an api.env text (exact names; the last assignment wins, as dotenv does)."""
    found: dict[str, str] = {}
    for line in env_text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name in CONFIG_NAMES:
            found[name] = value.strip()
    return found


def email_mode(cfg: dict[str, str], synthetic: list[str]) -> str:
    """"disabled": nothing can be sent. "guarded": Resend, and every synthetic address is off the recipient allowlist,
    so an invitation to one is refused before any provider call and withdrawn (502 invite_delivery_unconfirmed).
    Raises when a synthetic address would be deliverable or the backend is unknown: the suite must never send."""
    backend = (cfg.get("HYPERCHAT_EMAIL_BACKEND") or "disabled").lower()
    if backend == "disabled":
        return "disabled"
    if backend != "resend":
        raise ValueError(f"unknown email backend {backend!r}")
    allowed = {a.strip().lower() for a in (cfg.get("HYPERCHAT_EMAIL_RECIPIENT_ALLOWLIST") or "").split(",") if a.strip()}
    if any(s.strip().lower() in allowed for s in synthetic):
        raise ValueError("a synthetic address is on the email recipient allowlist; the suite would send email")
    return "guarded"


# app.auth.email logs these only after a request to the provider (Resend): a delivered and a failed attempt alike
PROVIDER_CONTACT = ("email sent", "email send failed")


def email_provider_contacts(journal: str) -> dict[str, int]:
    """How often the JSON journal shows a provider request; any non-zero count means an email left (or tried to leave)
    the server. A failed attempt counts too: it still handed an address to the provider."""
    return {m: journal.count(f'"{m}"') for m in PROVIDER_CONTACT}


# app.auth.service.create_invite when the email cannot be confirmed: the API answers only this generic message
# (app.main AuthError handler: {"error": ...}); the reason code is recorded in the security log, never in the response.
INVITE_UNCONFIRMED_ERROR = "Invitation delivery could not be confirmed. Please retry."


def invite_refusal_response_ok(status: int, text: str) -> tuple[bool, str]:
    """POST /api/v1/auth/invites to a non-allow-listed address: exactly 502 {"error": <the generic message>}. The exact
    body excludes everything else: an invitation (other status or invite id), a link or token, the reason code, an
    address. The detail names the status and the body's keys only."""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    ok = status == 502 and body == {"error": INVITE_UNCONFIRMED_ERROR}
    keys = sorted(body) if isinstance(body, dict) else type(body).__name__
    return ok, f"{status} body keys {keys}"


def journal_events(journal: str) -> list[dict]:
    """The JSON lines of a journal read with -o cat (app.core.logging); anything else is skipped."""
    events = []
    for line in journal.splitlines():
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def invite_refusal_logged(journal: str, admin_id: str | None, invite_id: str | None) -> tuple[bool, str]:
    """The security log proves why the invitation was refused (app.auth.service.create_invite, app.auth.email): for THIS
    admin and THIS invitation exactly one invite_delivery_unconfirmed with reason EmailRecipientNotAllowed (the
    allowlist refused before any provider request; any other reason means the provider was tried) and one
    invite_withdrawn; no invite_created and no invite_withdraw_failed by this admin; the 'email refused' line for the
    invite template; and the public 502 recorded as auth_unavailable with the reason code. Detail: counts only."""
    events = journal_events(journal)
    mine = [e for e in events if admin_id and e.get("user_id") == admin_id]
    count = lambda name, **want: sum(1 for e in mine if e.get("security_event") == name
                                     and all(e.get(k) == v for k, v in want.items()))
    unconfirmed = [e for e in mine if e.get("security_event") == "invite_delivery_unconfirmed"]
    refused = sum(1 for e in events if e.get("msg") == "email refused: recipient not on the allowlist"
                  and e.get("template") == "invite")
    public = sum(1 for e in events if e.get("security_event") == "auth_unavailable"
                 and e.get("reason") == "invite_delivery_unconfirmed" and e.get("status") == 502
                 and e.get("path") == "/api/v1/auth/invites")
    seen = {
        "unconfirmed": len(unconfirmed),
        "unconfirmed_allowlist_this_invite": count("invite_delivery_unconfirmed", reason="EmailRecipientNotAllowed",
                                                   invite_id=invite_id),
        "withdrawn_this_invite": count("invite_withdrawn", invite_id=invite_id),
        "created": count("invite_created"), "withdraw_failed": count("invite_withdraw_failed"),
        "email_refused": refused, "public_502": public,
    }
    ok = (bool(admin_id) and bool(invite_id) and seen["unconfirmed"] == 1 and seen["unconfirmed_allowlist_this_invite"] == 1
          and seen["withdrawn_this_invite"] == 1 and seen["created"] == 0 and seen["withdraw_failed"] == 0
          and refused >= 1 and public >= 1)
    return ok, str(seen)


def google_enabled(cfg: dict[str, str]) -> bool:
    """The API enables Google sign-in when the client id, secret and redirect URI are all set (Settings.google_enabled);
    the secret is never read here, so a set client id AND redirect URI stand for the three."""
    return bool(cfg.get("HYPERCHAT_GOOGLE_CLIENT_ID")) and bool(cfg.get("HYPERCHAT_GOOGLE_REDIRECT_URI"))


def google_start_ok(status: int, location: str, set_cookie: str, cfg: dict[str, str]) -> tuple[bool, str]:
    """GET /api/v1/auth/google/start, logged out. Google off: 404. Google on: 302 to Google's authorization endpoint for
    THIS client and redirect URI, code flow, openid email profile, PKCE S256, state and nonce, account chooser, and the
    state cookie __Host-hc_oauth (Secure, HttpOnly, SameSite=Lax, Path=/, no Domain). The detail never holds a value."""
    if not google_enabled(cfg):
        return status == 404, f"{status} (Google off: expected 404)"
    from urllib.parse import parse_qsl, urlsplit
    p = urlsplit(location or "")
    q = dict(parse_qsl(p.query))
    attrs = [a.strip().lower() for a in (set_cookie or "").split(";")]
    ok = (status == 302 and (p.scheme, p.netloc, p.path) == ("https", "accounts.google.com", "/o/oauth2/v2/auth")
          and q.get("client_id") == cfg["HYPERCHAT_GOOGLE_CLIENT_ID"] and q.get("redirect_uri") == cfg["HYPERCHAT_GOOGLE_REDIRECT_URI"]
          and q.get("response_type") == "code" and q.get("scope") == "openid email profile"
          and q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) == 43
          and len(q.get("state", "")) >= 30 and len(q.get("nonce", "")) >= 30 and q.get("prompt") == "select_account"
          and attrs[:1] and attrs[0].startswith("__host-hc_oauth=") and len(attrs[0]) > len("__host-hc_oauth=")
          and {"secure", "httponly", "samesite=lax", "path=/"} <= set(attrs) and not any(a.startswith("domain") for a in attrs))
    return bool(ok), f"{status} (Google on: expected 302 to Google with this client, PKCE and a __Host- state cookie)"


def goal_stamped_by_server(value: object, started: datetime, now: datetime, slack_s: int = 120) -> tuple[bool, str]:
    """D-G (#117): after this run activates its goal, goal_activated_at must be the server's activation time: set,
    and between this run's start and now (minus/plus a small clock slack; the suite runs on the API's host). A backdated
    or client-chosen time, a missing one or garbage fails. The detail names only the verdict."""
    try:
        stamped = datetime.fromisoformat(str(value))
    except ValueError:
        return False, "goal_activated_at missing or not a timestamp"
    if stamped.tzinfo is None:
        return False, "goal_activated_at has no time zone"
    lo, hi = started.timestamp() - slack_s, now.timestamp() + slack_s
    return lo <= stamped.timestamp() <= hi, "inside this run's window" if lo <= stamped.timestamp() <= hi else "outside this run's window"


# ── this run's own rate-limit state ───────────────────────────────────────────────────────────────────────
# public.rate_limits is keyed by (ip_address, endpoint), where ip_address holds the limiter key (app.auth.service,
# app.obs, app.realtime, app.kyc, app.stepup, app.payments, app.uploads, app.routers): ip:<addr>,
# acct:<sha256(email)>:ip:<addr>, email:<sha256(email)>, user:<id>, admin:<id>, streamer:<id>, obs:<sha256(token)[:32]>,
# order:<order id>, partner:<slug>. The suite only ever deletes keys that belong to this run: its synthetic emails,
# users, creators, OBS tokens, orders and partners (all named by its run tag), and ip:<the suite host's address>, which it learns from its own
# probe row (acct:<sha256(probe email)>:ip:<addr>). Real users' counters are never cleared.

def _sha(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()  # as app.auth.service hashes emails


def rate_limit_keys(emails, suite_ip: str | None) -> dict:
    """Parameters for OWNED_RATE_LIMITS / reset_login_counters: exact keys and LIKE prefixes of this run."""
    hashes = sorted({_sha(e) for e in emails})
    exact = [f"email:{h}" for h in hashes] + ([f"ip:{suite_ip}"] if suite_ip else [])
    return {"exact": exact, "prefixes": [f"acct:{h}:ip:%" for h in hashes]}


def suite_ip(conn, probe_email: str) -> str | None:
    """The address the API saw for this run: from the account-at-IP row of the run's own failed probe login."""
    row = conn.execute("SELECT ip_address FROM public.rate_limits WHERE ip_address LIKE %s ORDER BY created_at DESC "
                       "LIMIT 1", (f"acct:{_sha(probe_email)}:ip:%",)).fetchone()
    return row[0].split(":ip:", 1)[1] if row and ":ip:" in row[0] else None


def reset_login_counters(conn, keys: dict) -> int:
    """Clear the sign-in counters this run consumed (its host's IP and its synthetic accounts), nothing else."""
    return conn.execute("DELETE FROM public.rate_limits WHERE endpoint LIKE 'auth:login%%' "
                        "AND (ip_address = ANY(%(exact)s) OR ip_address LIKE ANY(%(prefixes)s))", keys).rowcount


OWNED_RATE_LIMITS = """(
    rl.ip_address = ANY(%(exact)s) OR rl.ip_address LIKE ANY(%(prefixes)s)
    OR rl.ip_address LIKE 'order:' || %(c)s OR rl.ip_address LIKE 'partner:' || %(c)s
    OR rl.ip_address IN (SELECT 'user:' || u.id FROM public.users u WHERE u.email::text LIKE %(u)s
                         UNION ALL SELECT 'admin:' || u.id FROM public.users u WHERE u.email::text LIKE %(u)s
                         UNION ALL SELECT 'streamer:' || s.id FROM public.streamers s
                                   WHERE s.streamer_slug LIKE %(c)s
                         UNION ALL SELECT 'obs:' || left(o.token_hash, 32) FROM public.obs_tokens o
                                   JOIN public.streamers s ON s.id = o.streamer_id
                                   WHERE s.streamer_slug LIKE %(c)s))"""


# ── synthetic fixtures and cleanup ─────────────────────────────────────────────────────────────────────────

ADMIN_INSERT = ("INSERT INTO public.users (email, password_hash, role, is_active) VALUES (%s, %s, 'admin', true) "
                "RETURNING id::text")
# Since migration 0024 the runtime roles may not create, change or delete an administrator: synthetic admins are
# created and removed through the schema owner (smoke_dev_backend.fixture_db), like the integration tests do.
USER_INSERT = ("INSERT INTO public.users (email, password_hash, role, streamer_id, is_active) VALUES (%s, %s, %s, %s, %s) "
               "RETURNING id::text")


def owner_creates_user(role: str, streamer_id: str | None) -> bool:
    """Whether a synthetic account must be written by the schema owner instead of the runtime role: an administrator
    (migration 0024) or a creator-linked account (0027: an API role creates one only by accepting its invitation,
    with the token, in the same transaction; the fixture is not that flow, so the owner writes it, as for admins)."""
    return role == "admin" or streamer_id is not None

# this run's rows only (u: its emails, c: its creators / orders / partners); another run's leftovers are not counted
LEFTOVERS = """
    SELECT (SELECT count(*) FROM public.users WHERE email::text LIKE %(u)s),
           (SELECT count(*) FROM public.sessions s JOIN public.users u ON u.id = s.user_id
             WHERE u.email::text LIKE %(u)s),
           (SELECT count(*) FROM public.password_reset_tokens t JOIN public.users u ON u.id = t.user_id
             WHERE u.email::text LIKE %(u)s),
           (SELECT count(*) FROM public.user_invites WHERE email::text LIKE %(u)s),
           (SELECT count(*) FROM public.user_signups WHERE email LIKE %(u)s),
           (SELECT count(*) FROM public.streamers WHERE streamer_slug LIKE %(c)s),
           (SELECT count(*) FROM public.donations WHERE order_id LIKE %(c)s),
           (SELECT count(*) FROM public.streamers_moderators WHERE mod_name = 'Smoke Mod' AND created_at >= %(t)s),
           (SELECT count(*) FROM public.banned_donors WHERE donor_name LIKE 'Smoke %%' AND created_at >= %(t)s),
           (SELECT count(*) FROM public.obs_tokens o LEFT JOIN public.streamers s ON s.id = o.streamer_id
             WHERE s.id IS NULL),
           (SELECT count(*) FROM public.jobs j LEFT JOIN public.donations d ON d.id::text = j.dedupe_key
             WHERE j.kind = 'donation_audio' AND d.id IS NULL AND j.created_at > %(t)s),
           (SELECT count(*) FROM public.soundboard_tnc_acceptances WHERE streamer_slug LIKE %(c)s),
           (SELECT count(*) FROM public.partner_api_keys WHERE partner_slug LIKE %(c)s),
           (SELECT count(*) FROM public.partner_signups WHERE partner_slug LIKE %(c)s
             OR email LIKE %(u)s),
           (SELECT count(*) FROM public.rate_limits rl WHERE rl.ip_address = ANY(%(exact)s)
             OR rl.ip_address LIKE ANY(%(prefixes)s) OR rl.ip_address LIKE 'order:' || %(c)s
             OR rl.ip_address LIKE 'partner:' || %(c)s)
"""
LEFTOVER_LABELS = ("users", "sessions", "reset tokens", "invites", "signups", "creators", "donations",
                   "moderators", "bans", "orphan OBS tokens", "jobs", "soundboard terms acceptances", "partner keys",
                   "partner signups", "rate limits (this run)")


def cleanup(app, owner, domain: str, started: datetime, keys: dict | None = None, *, tag: str) -> dict[str, int]:
    """Remove this run's synthetic rows and its own rate-limit rows (keys: rate_limit_keys()), and return what is
    left. tag is the run's prefix (smoke_dev_backend.TAG): its users are <tag>-*<domain>, its creators, orders and
    partners <tag>*. Every statement is scoped by it, so another run's rows (and real rows) are never deleted; a
    leftover of another run that blocks a delete makes it fail, which is reported. Each statement runs on its own
    (autocommit) so one failure cannot skip the rest; a failure is re-raised at the end after the leftover count,
    which then reports what stayed."""
    if not re.fullmatch(r"authtest-[a-z0-9]{3,}", tag):
        raise ValueError(f"not a synthetic run tag: {tag!r}")
    u, c = tag + "-%" + domain, tag + "%"
    rl = {"u": u, "c": c, "exact": [], "prefixes": [], **(keys or {})}
    steps = [
        # first, while the synthetic users / creators / tokens that identify the run's keys still exist
        (app, f"DELETE FROM public.rate_limits rl WHERE {OWNED_RATE_LIMITS}", rl),
        (app, "DELETE FROM public.user_invites WHERE email::text LIKE %s", (u,)),
        # review history is append-only for the runtime roles (migration 0021)
        (owner, "DELETE FROM public.application_events WHERE signup_id IN "
                "(SELECT id FROM public.user_signups WHERE email LIKE %s)", (u,)),
        (app, "DELETE FROM public.user_signups WHERE email LIKE %s", (u,)),
        (app, "DELETE FROM public.tts_moderation_audit WHERE donation_id IN (SELECT d.id FROM public.donations d "
              "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE %s)", (c,)),
        (app, "DELETE FROM public.jobs WHERE dedupe_key IN (SELECT d.id::text FROM public.donations d "
              "JOIN public.streamers s ON s.id = d.streamer_id WHERE s.streamer_slug LIKE %s)", (c,)),
        (app, "DELETE FROM public.donations WHERE streamer_id IN "
              "(SELECT id FROM public.streamers WHERE streamer_slug LIKE %s)", (c,)),
        # the status audit is append-only for the runtime roles (0020) and blocks deleting its creator (RESTRICT, 0023)
        (owner, "DELETE FROM public.creator_status_events WHERE streamer_id IN "
                "(SELECT id FROM public.streamers WHERE streamer_slug LIKE %s)", (c,)),
        (app, "DELETE FROM public.streamers WHERE streamer_slug LIKE %s", (c,)),  # cascades its rows
        # users through the owner: since 0024 the runtime roles may not delete an administrator (cascades sessions,
        # tokens, identities and step-up rows)
        (owner, "DELETE FROM public.users WHERE email::text LIKE %s", (u,)),
        (app, "DELETE FROM public.partner_signups WHERE partner_slug LIKE %s OR email LIKE %s", (c, u)),
        (app, "DELETE FROM public.partner_api_keys WHERE partner_slug LIKE %s", (c,)),
    ]
    errors = []
    for conn, sql, args in steps:
        try:
            conn.execute(sql, args)
        except Exception as exc:  # noqa: BLE001 - keep cleaning; reported below
            errors.append(f"{sql.split(' WHERE')[0]}: {type(exc).__name__}")
    left = dict(zip(LEFTOVER_LABELS, app.execute(LEFTOVERS, {**rl, "t": started}).fetchone()))
    if errors:
        raise RuntimeError("cleanup statements failed: " + "; ".join(errors) + f"; left {left}")
    return left
