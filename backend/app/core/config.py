"""Environment-based settings.

Every value comes from environment variables prefixed with ``HYPERCHAT_`` (on the VPS:
``/etc/hyperchat/api.env``, loaded by systemd). Nothing secret is stored in the repository.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from psycopg.conninfo import make_conninfo
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.origins import OriginError, is_development_origin, normalize_origin, parse_origins

# Written by the deploy step next to the code (see deploy/README in backend/README.md).
_RELEASE_FILE = Path(__file__).resolve().parents[2] / "RELEASE"
_R2_ACCOUNT = re.compile(r"^[A-Za-z0-9]{1,64}$")  # becomes part of a host name: no dots, slashes or ports
_R2_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
RAZORPAY_ACCOUNTS = ("primary", "secondary")  # values of streamers.razorpay_account
PUSHER_GROUPS = (1, 2, 3, 4)  # configurable Pusher apps (streamers.pusher_group 5..10 use group 1)
_PUSHER_APP_ID = re.compile(r"^[0-9]{1,20}$")
_PUSHER_KEY = re.compile(r"^[A-Za-z0-9]{1,64}$")
_PUSHER_CLUSTER = re.compile(r"^[a-z0-9]{1,20}$")  # becomes part of a host name (api-<cluster>.pusher.com)


def _secret_value(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value is not None else ""


def _https_url(value: str, *, allow_path: bool) -> str | None:
    """Normalized https URL without credentials, query or fragment (and without path unless allowed)."""
    try:
        parts = urlsplit(value)
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or not port_ok or "\\" in value or any(c.isspace() for c in value)):
        return None
    path = parts.path.rstrip("/")
    if path and not allow_path:
        return None
    return f"https://{parts.netloc}{path}"


class Settings(BaseSettings):
    # hide_input_in_errors: a refused configuration must never echo the environment (secrets) into logs.
    model_config = SettingsConfigDict(env_prefix="HYPERCHAT_", extra="ignore", hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    enable_docs: bool = False

    db_enabled: bool = True
    # host may be a Unix-socket directory (e.g. /var/run/postgresql) for peer authentication.
    db_host: str = "/var/run/postgresql"
    db_port: int = 5433
    db_name: str = "hyperchat_dev"
    db_user: str = "hyperchat_app"  # runtime role: DML only (see deploy/postgres/roles.sql)
    db_password: SecretStr | None = None
    # Human-readable label shown in logs and /health so nobody mistakes dev data for production.
    db_label: str = "DEV"
    db_connect_timeout: int = 5
    db_pool_min: int = 1
    db_pool_max: int = 4

    # ── Server-to-server security (app.security). Optional so the app still boots without
    #    them; every mechanism fails closed when its secret is missing. ──────────────────
    upload_grant_secret: SecretStr | None = None  # signs single-use donor upload grants
    internal_service_secret: SecretStr | None = None  # internal services / workers: HMAC signing key
    internal_service_secret_previous: SecretStr | None = None  # still accepted during key rotation
    cron_secret: SecretStr | None = None  # scheduled jobs calling the API

    # ── FastAPI-managed authentication (app.auth) ───────────────────────────────────
    # Signs CSRF tokens and the short-lived OAuth state cookie. Without it, no sessions are issued.
    auth_secret: SecretStr | None = None
    frontend_url: str = "https://hyperchat.space"  # reset/invite links and OAuth redirects
    # Extra browser origins trusted for credentialed CORS and the Origin check, besides the frontend's
    # own origin: comma-separated exact origins, e.g. "http://localhost:5173" for local development.
    allowed_origins: str = ""
    session_cookie_name: str = "__Host-hc_session"  # __Host-: Secure, Path=/, no Domain
    # "lax" whenever frontend and API are same-site (production). "none" only for cross-site development
    # and never in production.
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_idle_days: int = 7
    session_absolute_days: int = 30
    login_lockout_threshold: int = 5
    login_lockout_minutes: int = 15
    password_min_length: int = 8  # new passwords only; existing ones (min 6 before) still log in
    password_max_length: int = 128
    reset_token_ttl_minutes: int = 60
    invite_ttl_days: int = 7
    auth_return_invite_links: bool = False  # dev convenience: include the accept URL in the admin response

    email_backend: Literal["disabled", "resend"] = "disabled"
    resend_api_key: SecretStr | None = None
    email_from: str = "HyperChat <no-reply@hyperchat.space>"

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None  # e.g. https://api.hyperchat.space/api/v1/auth/google/callback

    # ── Cloudflare R2 object storage (app.storage). Uploads answer 503 until all of these are set.
    #    One bucket per environment; the key must be scoped to that bucket only. ──────────────
    r2_account_id: SecretStr | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str | None = None
    # Public read base of the bucket (r2.dev URL or a custom domain); object URLs are built from it.
    r2_public_base_url: str | None = None
    # S3 API endpoint; default https://<account_id>.r2.cloudflarestorage.com
    r2_endpoint: str | None = None

    # ── Razorpay (app.payments): up to two accounts; each creator uses the one named in
    #    streamers.razorpay_account. Checkout for an account is "on hold" until its key id and key secret
    #    are set; its webhooks are refused (401) until its webhook secret is set. Live keys (rzp_live_)
    #    are refused outside production: DEV uses Razorpay test mode only. ─────────────────────────
    razorpay_primary_key_id: SecretStr | None = None
    razorpay_primary_key_secret: SecretStr | None = None
    razorpay_primary_webhook_secret: SecretStr | None = None
    razorpay_secondary_key_id: SecretStr | None = None
    razorpay_secondary_key_secret: SecretStr | None = None
    razorpay_secondary_webhook_secret: SecretStr | None = None

    # ── Background jobs and audio (Phase 8: app.jobs, app.audio). Paid providers are only ever called by
    #    the worker, and only when HYPERCHAT_AUDIO_PROVIDERS_ENABLED=true AND the provider's key is set;
    #    otherwise every donation gets the silent fallback and transcription answers 503. ─────────
    audio_providers_enabled: bool = False
    minimax_api_key: SecretStr | None = None
    elevenlabs_api_key: SecretStr | None = None
    sarvam_api_key: SecretStr | None = None
    # HMAC key pseudonymising supporter names in tts_moderation_audit. Without it moderation fails closed
    # (no provider call: silent audio).
    tts_moderation_hash_key: SecretStr | None = None
    minimax_default_voice_id: str = "moss_audio_3e9334b7-e32a-11f0-ba34-ee3bcee0a7c9"  # legacy default voice
    worker_concurrency: int = 2

    # ── Realtime (Phase 9: app.realtime). Events are always written to public.realtime_outbox with the change
    #    that caused them; the worker publishes them to Pusher only when HYPERCHAT_REALTIME_ENABLED=true and the
    #    creator's Pusher group (streamers.pusher_group) is configured (unconfigured groups use group 1).
    #    Otherwise events are marked 'skipped' and the channel-auth/config endpoints answer 503. ──────────
    realtime_enabled: bool = False
    realtime_publish_rate: float = 50.0  # Pusher messages per second per worker process
    pusher_app_id_1: str | None = None
    pusher_key_1: str | None = None
    pusher_secret_1: SecretStr | None = None
    pusher_cluster_1: str | None = None
    pusher_app_id_2: str | None = None
    pusher_key_2: str | None = None
    pusher_secret_2: SecretStr | None = None
    pusher_cluster_2: str | None = None
    pusher_app_id_3: str | None = None
    pusher_key_3: str | None = None
    pusher_secret_3: SecretStr | None = None
    pusher_cluster_3: str | None = None
    pusher_app_id_4: str | None = None
    pusher_key_4: str | None = None
    pusher_secret_4: SecretStr | None = None
    pusher_cluster_4: str | None = None

    @field_validator("realtime_publish_rate")
    @classmethod
    def _publish_rate_range(cls, value: float) -> float:
        if not 1 <= value <= 1000:
            raise ValueError("HYPERCHAT_REALTIME_PUBLISH_RATE must be between 1 and 1000")
        return value

    def pusher_group(self, group: int) -> tuple[str, str, str, str] | None:
        """(app id, key, secret, cluster) of a configured and valid Pusher group 1..4, else None. Never log it."""
        if group not in PUSHER_GROUPS:
            return None
        app_id = (getattr(self, f"pusher_app_id_{group}") or "").strip()
        key = (getattr(self, f"pusher_key_{group}") or "").strip()
        secret = _secret_value(getattr(self, f"pusher_secret_{group}"))
        cluster = (getattr(self, f"pusher_cluster_{group}") or "").strip()
        if not (_PUSHER_APP_ID.match(app_id) and _PUSHER_KEY.match(key) and secret and _PUSHER_CLUSTER.match(cluster)):
            return None
        return app_id, key, secret, cluster

    def pusher_config_problems(self) -> list[str]:
        """Names (never values) of the partly set or invalid Pusher groups' variables."""
        problems = []
        for g in PUSHER_GROUPS:
            names = (f"app_id_{g}", f"key_{g}", f"secret_{g}", f"cluster_{g}")
            values = [getattr(self, f"pusher_{n}") for n in names]
            if any(v is not None and (_secret_value(v) if isinstance(v, SecretStr) else v.strip()) for v in values) \
                    and self.pusher_group(g) is None:
                problems.append(f"HYPERCHAT_PUSHER_*_{g}")
        return problems

    @property
    def pusher_groups_configured(self) -> list[int]:
        return [g for g in PUSHER_GROUPS if self.pusher_group(g) is not None]

    @field_validator("worker_concurrency")
    @classmethod
    def _worker_concurrency_range(cls, value: int) -> int:
        if not 1 <= value <= 16:
            raise ValueError("HYPERCHAT_WORKER_CONCURRENCY must be between 1 and 16")
        return value

    def provider_key(self, name: str) -> str:
        """The API key of 'minimax', 'elevenlabs' or 'sarvam' ("" when unset). Never log it."""
        return _secret_value({"minimax": self.minimax_api_key, "elevenlabs": self.elevenlabs_api_key,
                              "sarvam": self.sarvam_api_key}[name])

    def audio_provider_names(self) -> list[str]:
        """Providers the worker may call (names only, for logs)."""
        if not self.audio_providers_enabled:
            return []
        return [n for n in ("minimax", "elevenlabs", "sarvam") if self.provider_key(n)]

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_redirect_uri)

    @field_validator("r2_bucket", "r2_public_base_url", "r2_endpoint", mode="before")
    @classmethod
    def _blank_is_unset(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None  # an empty line in the env file means "not set"
        return value

    @property
    def r2_endpoint_url(self) -> str | None:
        """S3 API endpoint (explicit, or derived from the account id); None when invalid or unset."""
        if self.r2_endpoint:
            return _https_url(self.r2_endpoint, allow_path=False)
        account = _secret_value(self.r2_account_id)
        return f"https://{account}.r2.cloudflarestorage.com" if account and _R2_ACCOUNT.match(account) else None

    @property
    def r2_public_base(self) -> str | None:
        """Public read base for object URLs, without trailing slash; None when invalid or unset."""
        return _https_url(self.r2_public_base_url, allow_path=True) if self.r2_public_base_url else None

    def r2_config_problems(self) -> list[str]:
        """Names (never values) of the R2 variables that are missing or invalid."""
        problems = []
        if not self.r2_endpoint_url:
            problems.append("HYPERCHAT_R2_ENDPOINT" if self.r2_endpoint else "HYPERCHAT_R2_ACCOUNT_ID")
        if not _secret_value(self.r2_access_key_id):
            problems.append("HYPERCHAT_R2_ACCESS_KEY_ID")
        if not _secret_value(self.r2_secret_access_key):
            problems.append("HYPERCHAT_R2_SECRET_ACCESS_KEY")
        if not (self.r2_bucket and _R2_BUCKET.match(self.r2_bucket)):
            problems.append("HYPERCHAT_R2_BUCKET")
        if not self.r2_public_base:
            problems.append("HYPERCHAT_R2_PUBLIC_BASE_URL")
        return problems

    @property
    def r2_configured(self) -> bool:
        return not self.r2_config_problems()

    @property
    def upload_grants_configured(self) -> bool:
        return bool(_secret_value(self.upload_grant_secret))

    def razorpay_credentials(self, account: str) -> tuple[str, str, str]:
        """(key id, key secret, webhook secret) of 'primary' or 'secondary'; "" for unset values."""
        if account not in RAZORPAY_ACCOUNTS:
            raise ValueError("unknown Razorpay account")
        return (_secret_value(getattr(self, f"razorpay_{account}_key_id")),
                _secret_value(getattr(self, f"razorpay_{account}_key_secret")),
                _secret_value(getattr(self, f"razorpay_{account}_webhook_secret")))

    @model_validator(mode="after")
    def _refuse_live_razorpay_keys_outside_production(self) -> "Settings":
        if self.app_env != "production":
            live = [f"HYPERCHAT_RAZORPAY_{a.upper()}_KEY_ID" for a in RAZORPAY_ACCOUNTS
                    if self.razorpay_credentials(a)[0].startswith("rzp_live_")]
            if live:  # variable names only, never the values
                raise ValueError(f"Refusing to start: live Razorpay keys outside production: {live}")
        return self

    @model_validator(mode="after")
    def _refuse_test_data_in_production(self) -> "Settings":
        label = self.db_label.upper()
        if self.app_env == "production" and ("TEST" in label or "DEV" in label):
            raise ValueError("Refusing to start: production environment pointed at a TEST/DEV database.")
        return self

    @model_validator(mode="after")
    def _validate_browser_origins(self) -> "Settings":
        try:
            origins = self.browser_origins
        except OriginError as exc:
            raise ValueError(str(exc)) from None
        if self.app_env == "production":
            dev = [o for o in origins if is_development_origin(o)]
            if dev:
                raise ValueError(f"Refusing to start: development origins in production: {dev}")
            if self.session_cookie_samesite == "none":
                raise ValueError("Refusing to start: SameSite=None is not allowed in production.")
        return self

    @property
    def browser_origins(self) -> frozenset[str]:
        """Every origin trusted for credentialed browser requests (frontend + HYPERCHAT_ALLOWED_ORIGINS)."""
        front = urlsplit(self.frontend_url)
        return frozenset((normalize_origin(f"{front.scheme}://{front.netloc}"), *parse_origins(self.allowed_origins)))

    @property
    def release(self) -> str:
        try:
            return _RELEASE_FILE.read_text().strip() or "unknown"
        except OSError:
            return "dev"

    def db_conninfo(self) -> str:
        """libpq connection string. Contains the password if one is configured: never log it."""
        params = {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name,
            "user": self.db_user,
            "connect_timeout": self.db_connect_timeout,
            "application_name": "hyperchat-api",
        }
        if self.db_password is not None:
            params["password"] = self.db_password.get_secret_value()
        return make_conninfo(**params)


@lru_cache
def get_settings() -> Settings:
    return Settings()
