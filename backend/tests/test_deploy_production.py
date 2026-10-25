"""Production infrastructure files (deploy/): separated from DEV, no secrets, same hardening.

Static checks only; nginx syntax and the production rate-limit behaviour are exercised by
tests/e2e/nginx_ratelimit_check.sh (CI job "Deployment configuration").
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
PROD_FILES = ["systemd/hyperchat-prod-api.service", "systemd/hyperchat-prod-worker.service",
              "systemd/hyperchat-prod-cleanup.service",
              "systemd/hyperchat-prod-cleanup.timer", "nginx/api.hyperchat.space.conf",
              "nginx/api.hyperchat.space.acme.conf", "nginx/snippets/hyperchat-prod-public-limit-zone.conf",
              "nginx/snippets/hyperchat-prod-public-limit.conf", "postgres/pg_ident.production.conf",
              "api.env.production.example"]
DEV_FILES = ["systemd/hyperchat-api.service", "systemd/hyperchat-worker.service", "systemd/hyperchat-cleanup.service",
             "systemd/hyperchat-cleanup.timer",
             "nginx/api.quicktestspace.site.conf", "nginx/snippets/hyperchat-public-limit-zone.conf",
             "nginx/snippets/hyperchat-public-limit.conf", "postgres/pg_ident.conf", "api.env.example"]


def read(rel: str) -> str:
    return (DEPLOY / rel).read_text()


def unit(rel: str) -> dict[str, str]:
    """[Service] key -> value of a systemd unit (continuation lines joined)."""
    text = re.sub(r"\\\n\s*", " ", read(rel))
    section, out = None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[key] = value
    return out


# ── systemd ─────────────────────────────────────────────────────────────────────

CHANGED = {"User", "Group", "WorkingDirectory", "EnvironmentFile", "ExecStart"}


@pytest.mark.parametrize("prod,dev", [("systemd/hyperchat-prod-api.service", "systemd/hyperchat-api.service"),
                                      ("systemd/hyperchat-prod-worker.service", "systemd/hyperchat-worker.service"),
                                      ("systemd/hyperchat-prod-cleanup.service", "systemd/hyperchat-cleanup.service")])
def test_production_units_differ_from_dev_only_in_user_paths_env_and_port(prod, dev):
    p, d = unit(prod), unit(dev)
    assert set(p) == set(d)
    assert {k: v for k, v in p.items() if k not in CHANGED} == {k: v for k, v in d.items() if k not in CHANGED}
    assert (p["User"], p["Group"]) == ("hyperchat-prod", "hyperchat-prod")
    assert (d["User"], d["Group"]) == ("hyperchat", "hyperchat")
    assert p["WorkingDirectory"] == "/opt/hyperchat-prod/current/backend"
    assert p["EnvironmentFile"] == "/etc/hyperchat-prod/api.env"
    assert d["WorkingDirectory"] == "/opt/hyperchat/current/backend" and d["EnvironmentFile"] == "/etc/hyperchat/api.env"
    assert p["ExecStart"].startswith("/opt/hyperchat-prod/current/venv/bin/")
    assert p["ExecStart"].replace("/opt/hyperchat-prod/", "/opt/hyperchat/").replace("--port 8001", "--port 8000") \
        == d["ExecStart"]


@pytest.mark.parametrize("rel", ["systemd/hyperchat-prod-api.service", "systemd/hyperchat-prod-worker.service"])
def test_hardening_is_kept(rel):
    p = unit(rel)
    for key, value in {"NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes",
                       "PrivateDevices": "yes", "CapabilityBoundingSet": "", "AmbientCapabilities": "",
                       "RestrictNamespaces": "yes", "RestrictSUIDSGID": "yes", "ProtectKernelModules": "yes",
                       "ProtectKernelTunables": "yes", "SystemCallArchitectures": "native", "UMask": "0077",
                       "MemoryMax": "1G"}.items():
        assert p[key] == value, (rel, key)
    assert set(p["RestrictAddressFamilies"].split()) == {"AF_INET", "AF_INET6", "AF_UNIX"}, rel


def test_production_worker_runs_the_api_release_as_the_api_user_and_listens_nowhere():
    from app.jobs.runner import Worker
    import inspect
    w, api = unit("systemd/hyperchat-prod-worker.service"), unit("systemd/hyperchat-prod-api.service")
    assert w["ExecStart"] == "/opt/hyperchat-prod/current/venv/bin/python -m app.worker"
    # the same release (current), environment file, OS user and working directory as the production API
    for key in ("User", "Group", "WorkingDirectory", "EnvironmentFile"):
        assert w[key] == api[key], key
    assert w["User"] not in ("root", "hyperchat", "hyperchat-prod-stepup")
    assert w["ExecStart"].split()[0].startswith("/opt/hyperchat-prod/current/venv/")
    assert api["ExecStart"].split()[0].startswith("/opt/hyperchat-prod/current/venv/")
    # no port, no socket activation, no extra privileges
    text = _directives("systemd/hyperchat-prod-worker.service")
    for forbidden in ("--port", "--host", "ListenStream", "Sockets=", "CAP_", "User=root", "PermissionsStartOnly",
                      "stepup", "migrator", "ExecStartPre=+", "ExecStart=+", "ExecStart=!"):
        assert forbidden not in text, forbidden
    # graceful stop: running jobs get the runner's grace period before systemd kills the process
    grace = inspect.signature(Worker.__init__).parameters["shutdown_grace"].default
    assert w["KillSignal"] == "SIGTERM" and int(w["TimeoutStopSec"]) > grace
    assert (w["Restart"], w["RestartSec"]) == ("always", "3")
    assert (DEPLOY / "systemd/hyperchat-prod-worker.service").read_text().count("[Install]") == 1
    assert "WantedBy=multi-user.target" in read("systemd/hyperchat-prod-worker.service")


def test_ports_and_workers():
    prod = unit("systemd/hyperchat-prod-api.service")["ExecStart"]
    dev = unit("systemd/hyperchat-api.service")["ExecStart"]
    assert "--host 127.0.0.1 --port 8001 --workers 2" in prod and "--port 8000" not in prod
    assert "--host 127.0.0.1 --port 8000 --workers 2" in dev and "8001" not in dev


def test_reset_email_work_is_bounded_for_the_deployed_topology():
    """Security audit F11 follow-up: the reset-email cap is per process, so it is only a bound because the worker count
    is fixed here. Raising --workers or the cap must be a reviewed change (this test fails), and each send's deadline
    must end before systemd kills the process on a restart."""
    from app.auth import service

    for rel in ("systemd/hyperchat-api.service", "systemd/hyperchat-prod-api.service"):
        u = unit(rel)
        workers = int(re.search(r"--workers (\d+)", u["ExecStart"]).group(1))
        assert workers * service.RESET_EMAIL_SLOTS <= 8, rel  # at most 8 reset emails in flight per environment
        assert service.RESET_EMAIL_DEADLINE_SECONDS < int(u["TimeoutStopSec"]), rel


def test_cleanup_timer_is_separate():
    prod, dev = read("systemd/hyperchat-prod-cleanup.timer"), read("systemd/hyperchat-cleanup.timer")
    assert "OnCalendar=*-*-* 03:45:00 UTC" in prod and "OnCalendar=*-*-* 03:15:00 UTC" in dev
    assert "hyperchat-prod-cleanup" not in dev
    assert unit("systemd/hyperchat-prod-cleanup.service")["ExecStart"] == \
        "/opt/hyperchat-prod/current/venv/bin/python -m app.cli cleanup"


def _directives(rel: str) -> str:
    """The file without comment lines (comments may name the other environment on purpose)."""
    return "\n".join(line for line in read(rel).splitlines() if not line.lstrip().startswith("#"))


def test_production_and_dev_paths_never_cross():
    for rel in PROD_FILES:
        text = _directives(rel)
        assert "/opt/hyperchat/" not in text and "/etc/hyperchat/" not in text, rel
        assert "quicktestspace" not in text and "127.0.0.1:8000" not in text and "hyperchat_dev" not in text, rel
        assert "zone=hyperchat_public:" not in text and "zone=hyperchat_public " not in text, rel
    for rel in DEV_FILES:
        text = _directives(rel)
        assert "hyperchat-prod" not in text and "api.hyperchat.space" not in text, rel
        assert "8001" not in text and "hyperchat_public_prod" not in text, rel


# ── nginx ───────────────────────────────────────────────────────────────────────

def _proxy_targets(text: str) -> set[str]:
    return set(re.findall(r"proxy_pass\s+([^;]+);", text))


def test_production_site():
    site = read("nginx/api.hyperchat.space.conf")
    assert set(re.findall(r"server_name\s+([^;]+);", site)) == {"api.hyperchat.space"}
    assert _proxy_targets(site) == {"http://127.0.0.1:8001"}
    assert "listen 80;" in site and "listen 443 ssl;" in site and "http2 on;" in site
    assert "location ^~ /.well-known/acme-challenge/ { root /var/www/acme;" in site
    assert "return 301 https://$host$request_uri;" in site
    assert "/etc/letsencrypt/live/api.hyperchat.space/fullchain.pem" in site
    assert "/etc/letsencrypt/live/api.hyperchat.space/privkey.pem" in site
    assert "location = /nginx-health" in site
    assert "include /etc/nginx/snippets/hyperchat-prod-public-limit-zone.conf;" in site
    block = site.split("location ~ ^/api/v1/(public|metrics)/ {", 1)[1].split("}", 1)[0]
    assert "include /etc/nginx/snippets/hyperchat-prod-public-limit.conf;" in block
    assert "hyperchat-public-limit" not in site.replace("hyperchat-prod-public-limit", "")  # never the DEV zone
    assert "default_server" not in site  # the catch-all keeps handling unknown names / bare IP


def test_production_site_mirrors_dev_headers_body_size_and_timeouts():
    prod, dev = read("nginx/api.hyperchat.space.conf"), read("nginx/api.quicktestspace.site.conf")
    for pattern in (r"add_header [^;]+;", r"client_max_body_size [^;]+;", r"proxy_(?:connect|read)_timeout [^;]+;",
                    r"proxy_set_header [^;]+;", r"ssl_(?:protocols|session_cache|session_timeout|session_tickets) [^;]+;",
                    r"return 429 [^;]+;"):
        assert re.findall(pattern, prod) == re.findall(pattern, dev), pattern


def test_bootstrap_site_needs_no_certificate():
    directives = _directives("nginx/api.hyperchat.space.acme.conf")
    assert "listen 80;" in directives and "443" not in directives and "ssl" not in directives
    assert "server_name api.hyperchat.space;" in directives and "/.well-known/acme-challenge/" in directives
    assert "proxy_pass" not in directives


def test_production_rate_limit_zone_is_separate_with_the_same_behaviour():
    pz, pl = read("nginx/snippets/hyperchat-prod-public-limit-zone.conf"), read("nginx/snippets/hyperchat-prod-public-limit.conf")
    dz, dl = read("nginx/snippets/hyperchat-public-limit-zone.conf"), read("nginx/snippets/hyperchat-public-limit.conf")
    assert "zone=hyperchat_public_prod:10m rate=10r/s;" in pz and "zone=hyperchat_public:10m rate=10r/s;" in dz
    assert "limit_req zone=hyperchat_public_prod burst=20 nodelay;" in pl
    assert "limit_req zone=hyperchat_public burst=20 nodelay;" in dl
    for line in ("limit_req_status 429;", "error_page 429 = @hyperchat_rate_limited;"):
        assert line in pl and line in dl


def test_dev_site_unchanged_in_meaning():
    dev = read("nginx/api.quicktestspace.site.conf")
    assert set(re.findall(r"server_name\s+([^;]+);", dev)) == {"api.quicktestspace.site"}
    assert _proxy_targets(dev) == {"http://127.0.0.1:8000"}


# ── PostgreSQL ident maps ───────────────────────────────────────────────────────

def _map(rel: str) -> set[tuple[str, ...]]:
    return {tuple(line.split()) for line in read(rel).splitlines() if line.strip() and not line.startswith("#")}


def test_production_ident_map_excludes_the_dev_user():
    prod = _map("postgres/pg_ident.production.conf")
    assert prod == {("hyperchat", "root", "hyperchat_migrator"), ("hyperchat", "root", "hyperchat_app"),
                    ("hyperchat", "root", "hyperchat_worker"), ("hyperchat", "root", "hyperchat_readonly"),
                    ("hyperchat", "hyperchat-prod", "hyperchat_app"), ("hyperchat", "hyperchat-prod", "hyperchat_worker"),
                    ("hyperchat", "hyperchat-prod", "hyperchat_lifecycle"),  # KYC PR4: creator status transitions
                    ("hyperchat", "root", "hyperchat_auth"), ("hyperchat", "hyperchat-prod", "hyperchat_auth"),  # 0024
                    ("hyperchat", "hyperchat-prod-stepup", "hyperchat_stepup")}  # 0025: the verifier's own user
    assert not any(system == "hyperchat" for _, system, _ in prod)
    dev = _map("postgres/pg_ident.conf")  # DEV cluster template: unchanged, no production user
    assert ("hyperchat", "hyperchat", "hyperchat_app") in dev and not any("hyperchat-prod" in e for e in dev)


# ── environment template ────────────────────────────────────────────────────────

def _env(rel: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in read(rel).splitlines() if line and not line.startswith("#"))


def test_production_env_template_values():
    env = _env("api.env.production.example")
    assert env == {
        "HYPERCHAT_APP_ENV": "production", "HYPERCHAT_LOG_LEVEL": "INFO", "HYPERCHAT_ENABLE_DOCS": "false",
        "HYPERCHAT_DB_ENABLED": "true", "HYPERCHAT_DB_HOST": "/var/run/postgresql", "HYPERCHAT_DB_PORT": "5432",
        "HYPERCHAT_DB_NAME": "hyperchat", "HYPERCHAT_DB_USER": "hyperchat_app",
        "HYPERCHAT_DB_LIFECYCLE_USER": "hyperchat_lifecycle", "HYPERCHAT_DB_AUTH_USER": "hyperchat_auth",
        "HYPERCHAT_DB_LABEL": "PROD hyperchat (HyperChat v2 production)", "HYPERCHAT_DB_POOL_MIN": "1",
        "HYPERCHAT_DB_POOL_MAX": "4", "HYPERCHAT_FRONTEND_URL": "https://hyperchat.space",
        "HYPERCHAT_ALLOWED_ORIGINS": "", "HYPERCHAT_SESSION_COOKIE_SAMESITE": "lax", "HYPERCHAT_SESSION_IDLE_DAYS": "7",
        "HYPERCHAT_SESSION_ABSOLUTE_DAYS": "30", "HYPERCHAT_ADMIN_SESSION_IDLE_HOURS": "2",
        "HYPERCHAT_ADMIN_SESSION_ABSOLUTE_HOURS": "12", "HYPERCHAT_AUTH_RETURN_INVITE_LINKS": "false",
        "HYPERCHAT_EMAIL_BACKEND": "disabled", "HYPERCHAT_EMAIL_ACTIVE_CATEGORIES": "",
        "HYPERCHAT_EMAIL_FROM": "HyperChat <no-reply@hyperchat.space>",
        # the worker (hyperchat-prod-worker.service): every integration explicitly off until approved
        "HYPERCHAT_WORKER_CONCURRENCY": "2", "HYPERCHAT_AUDIO_PROVIDERS_ENABLED": "false",
        "HYPERCHAT_REALTIME_ENABLED": "false", "HYPERCHAT_BOTS_ENABLED": "false"}


def test_production_env_template_passes_the_production_guards(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("HYPERCHAT_"):
            monkeypatch.delenv(key)
    s = Settings(_env_file=str(DEPLOY / "api.env.production.example"))
    assert s.app_env == "production" and s.db_name == "hyperchat" and s.db_port == 5432
    assert s.session_cookie_samesite == "lax" and s.browser_origins == frozenset({"https://hyperchat.space"})
    assert s.auth_secret is None and s.google_enabled is False and s.resend_api_key is None
    assert s.email_categories == ()  # no email category in production until its domains are approved
    # the worker's integrations: all off, nothing configured, until set on the server with approval
    assert s.worker_concurrency == 2 and s.audio_providers_enabled is False and s.realtime_enabled is False
    assert s.bots_enabled is False and not s.telegram_configured and not s.discord_configured
    assert s.audio_provider_names() == [] and not s.pusher_groups_configured
    assert s.r2_public_base is None and s.tts_moderation_hash_key is None
    assert s.db_user == "hyperchat_app"  # the worker's role: the API's DML-only role, never migrator / owner / stepup


def test_every_worker_setting_in_the_production_template_is_a_real_setting():
    """No invented keys: every HYPERCHAT_ key named in the template, set or commented, is a Settings field."""
    fields = {name.upper() for name in Settings.model_fields}
    named = set(re.findall(r"(?m)^#?\s*HYPERCHAT_([A-Z0-9_]+)=", read("api.env.production.example")))
    assert named and named <= fields, sorted(named - fields)
    for key in ("WORKER_CONCURRENCY", "AUDIO_PROVIDERS_ENABLED", "MINIMAX_API_KEY", "ELEVENLABS_API_KEY",
                "SARVAM_API_KEY", "TTS_MODERATION_HASH_KEY", "REALTIME_ENABLED", "PUSHER_APP_ID_1", "PUSHER_KEY_1",
                "PUSHER_SECRET_1", "PUSHER_CLUSTER_1", "REALTIME_PUBLISH_RATE", "R2_ACCOUNT_ID", "R2_BUCKET",
                "R2_PUBLIC_BASE_URL", "BOTS_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
                "DISCORD_BOT_TOKEN", "DISCORD_PUBLIC_KEY", "DISCORD_APPLICATION_ID"):
        assert key in named, key
    # the allow-lists are DEV-only (ignored in production): not offered in the production template
    assert not {"BOT_RECIPIENT_ALLOWLIST", "BOT_CREATOR_ALLOWLIST"} & named


SECRET_KEYS = ("SECRET", "PASSWORD", "API_KEY", "CLIENT_ID", "TOKEN")


@pytest.mark.parametrize("rel", ["api.env.production.example", "api.env.example"])
def test_templates_hold_no_secrets(rel):
    for key, value in _env(rel).items():
        if any(word in key for word in SECRET_KEYS):
            assert value == "", key
    assert not re.search(r"(?m)^HYPERCHAT_[A-Z_]*(SECRET|PASSWORD|API_KEY)=\S", read(rel))
    assert "localhost" not in "".join(v for k, v in _env("api.env.production.example").items())


def test_no_supabase_in_deployment_files():
    for path in DEPLOY.rglob("*"):
        if path.is_file():
            assert "supabase" not in path.read_text(errors="ignore").lower(), path
    for req in ("backend/requirements.txt", "backend/requirements-dev.txt", "database/requirements.txt"):
        assert "supabase" not in (REPO / req).read_text().lower(), req
