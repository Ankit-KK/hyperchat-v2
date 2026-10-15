"""Environment-based settings.

Every value comes from environment variables prefixed with ``HYPERCHAT_`` (on the VPS:
``/etc/hyperchat/api.env``, loaded by systemd). Nothing secret is stored in the repository.
"""

import re
import unicodedata
import uuid
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
RAZORPAY_ACCOUNTS = ("primary",)  # values of streamers.razorpay_account (D6: secondary retired)
PUSHER_GROUPS = (1, 2, 3, 4)  # configurable Pusher apps (streamers.pusher_group 5..10 use group 1)
_PUSHER_APP_ID = re.compile(r"^[0-9]{1,20}$")
_PUSHER_KEY = re.compile(r"^[A-Za-z0-9]{1,64}$")
_PUSHER_CLUSTER = re.compile(r"^[a-z0-9]{1,20}$")  # becomes part of a host name (api-<cluster>.pusher.com)
# D5 public contact details (GET /api/v1/public/config).
_DISCORD_INVITE = re.compile(r"^https://(?i:discord\.gg|(?:www\.)?discord\.com/invite)/[A-Za-z0-9-]{2,32}$")
_CONTACT_EMAIL = re.compile(r"^[a-z0-9._%+-]{1,64}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")
_PHONE = re.compile(r"^\+?[0-9 ()-]{4,24}$")
MAX_CONTACT_PHONES = 5


def _secret_value(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value is not None else ""


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(value)) if value else None
    except ValueError:
        return None


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

    # ── FastAPI-managed authentication (app.auth) ───────────────────────────────────
    # Signs CSRF tokens and the short-lived OAuth state cookie. Without it, no sessions are issued.
    auth_secret: SecretStr | None = None
    frontend_url: str = "https://hyperchat.space"  # reset/invite links and OAuth redirects
    # Extra browser origins trusted for credentialed CORS and the Origin check, besides the frontend's
    # own origin: comma-separated exact origins, e.g. "http://localhost:5173" for local development.
    allowed_origins: str = ""
    # D5: contact details the frontend shows (GET /api/v1/public/config). Intentionally PUBLIC; unset = null / [].
    # Checked at startup: a malformed value refuses to start rather than being shown.
    public_contact_name: str = ""
    public_contact_email: str = ""
    public_contact_phones: str = ""  # comma-separated, at most 5
    public_discord_url: str = ""  # a Discord invite: https://discord.gg/<code> or https://discord.com/invite/<code>
    session_cookie_name: str = "__Host-hc_session"  # __Host-: Secure, Path=/, no Domain
    # "lax" whenever frontend and API are same-site (production). "none" only for cross-site development
    # and never in production.
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_idle_days: int = 7
    session_absolute_days: int = 30
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

    # ── Razorpay (app.payments): one account, 'primary' (D6); test vs live is the environment's job.
    #    Checkout is "on hold" until its key id and key secret are set; webhooks are refused (401) until its
    #    webhook secret is set. Startup refuses live keys (rzp_live_) outside production and test keys
    #    (rzp_test_) in production, so a test key can never take real donations and DEV can never charge. ──
    razorpay_primary_key_id: SecretStr | None = None
    razorpay_primary_key_secret: SecretStr | None = None
    razorpay_primary_webhook_secret: SecretStr | None = None

    # ── Didit identity verification (KYC PR3, app.kyc). Creator OWNERS verify their identity; KYC is unavailable
    #    (503) until the API key, webhook secret and workflow id are set. DEV uses a Didit SANDBOX application,
    #    production a LIVE one (refused otherwise, below), so a sandbox result can never count in production.
    didit_api_key: SecretStr | None = None
    didit_webhook_secret: SecretStr | None = None
    didit_workflow_id: str | None = None   # not secret; the "Free KYC" workflow of this environment's application
    didit_application_id: str | None = None  # when set, webhooks from any other Didit application are refused
    didit_environment: Literal["sandbox", "live"] | None = None
    didit_api_base: str = "https://verification.didit.me"

    @property
    def didit_configured(self) -> bool:
        return bool(_secret_value(self.didit_api_key) and _secret_value(self.didit_webhook_secret)
                    and _uuid(self.didit_workflow_id) and self.didit_environment)

    @model_validator(mode="after")
    def _refuse_the_wrong_didit_environment(self) -> "Settings":
        expected = "live" if self.app_env == "production" else "sandbox"
        if self.didit_environment is not None and self.didit_environment != expected:
            raise ValueError(f"Refusing to start: HYPERCHAT_DIDIT_ENVIRONMENT must be {expected!r} here")
        if self.didit_workflow_id and not _uuid(self.didit_workflow_id):
            raise ValueError("Refusing to start: HYPERCHAT_DIDIT_WORKFLOW_ID must be a UUID")
        if self.didit_application_id and not _uuid(self.didit_application_id):
            raise ValueError("Refusing to start: HYPERCHAT_DIDIT_APPLICATION_ID must be a UUID")
        if _https_url(self.didit_api_base, allow_path=False) != self.didit_api_base.rstrip("/"):
            raise ValueError("Refusing to start: HYPERCHAT_DIDIT_API_BASE must be an https origin")
        return self

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

    # Phase 10 (P10-D13): creators whose "!vaani" donations play the celebration instead of an alert (comma-separated
    # slugs; legacy: ankit and captain_vaani). Empty = nobody.
    vaani_creators: str = "ankit,captain_vaani"

    @property
    def vaani_slugs(self) -> frozenset[str]:
        return frozenset(s.strip().lower() for s in self.vaani_creators.split(",") if s.strip())

    # ── Moderation bots (Phase 11: app.bots). OFF by default. Outside production, messages go ONLY to the recipient
    #    ids in HYPERCHAT_BOT_RECIPIENT_ALLOWLIST (Telegram chat ids, Discord user / channel ids): the DEV database
    #    holds real moderators' ids. DEV uses its own test bots: never the legacy production bot or application. ──
    bots_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None  # X-Telegram-Bot-Api-Secret-Token (setWebhook secret_token)
    discord_bot_token: SecretStr | None = None
    discord_public_key: str | None = None  # the application's Ed25519 public key (hex)
    discord_application_id: str | None = None
    bot_recipient_allowlist: str = ""
    # Outside production the bots also act ONLY for these creator slugs (notifications, commands, button presses):
    # an allow-listed tester is often a moderator of real DEV creators too, and /pending would hand them live buttons.
    bot_creator_allowlist: str = ""

    @property
    def bot_recipients(self) -> frozenset[str] | None:
        """None = no restriction (production only); otherwise the allow-listed recipient ids (possibly empty)."""
        if self.app_env == "production":
            return None
        return frozenset(i.strip() for i in self.bot_recipient_allowlist.split(",") if i.strip())

    @property
    def bot_creators(self) -> frozenset[str] | None:
        """None = no restriction (production only); otherwise the allow-listed creator slugs (possibly empty)."""
        if self.app_env == "production":
            return None
        return frozenset(s.strip().lower() for s in self.bot_creator_allowlist.split(",") if s.strip())

    @property
    def telegram_configured(self) -> bool:
        return bool(_secret_value(self.telegram_bot_token) and _secret_value(self.telegram_webhook_secret))

    @property
    def discord_configured(self) -> bool:
        key = (self.discord_public_key or "").strip()
        return bool(_secret_value(self.discord_bot_token) and re.fullmatch(r"[0-9a-fA-F]{64}", key))

    @field_validator("public_contact_name")
    @classmethod
    def _contact_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 80 or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("HYPERCHAT_PUBLIC_CONTACT_NAME: at most 80 characters, no control characters")
        return value

    @field_validator("public_contact_email")
    @classmethod
    def _contact_email(cls, value: str) -> str:
        value = value.strip().lower()
        if value and (len(value) > 254 or not _CONTACT_EMAIL.match(value)):
            raise ValueError("HYPERCHAT_PUBLIC_CONTACT_EMAIL is not a valid email address")
        return value

    @field_validator("public_contact_phones")
    @classmethod
    def _contact_phones(cls, value: str) -> str:
        phones = [" ".join(p.split()) for p in value.split(",") if p.strip()]
        for phone in phones:
            if not (_PHONE.match(phone) and 6 <= sum(c.isdigit() for c in phone) <= 15):
                raise ValueError("HYPERCHAT_PUBLIC_CONTACT_PHONES: comma-separated numbers of 6-15 digits "
                                 "(+, spaces, parentheses and hyphens allowed)")
        if len(phones) > MAX_CONTACT_PHONES:
            raise ValueError(f"HYPERCHAT_PUBLIC_CONTACT_PHONES: at most {MAX_CONTACT_PHONES} numbers")
        return ",".join(phones)

    @field_validator("public_discord_url")
    @classmethod
    def _discord_url(cls, value: str) -> str:
        value = value.strip()
        url = _https_url(value, allow_path=True) if value else ""
        if value and not (url and _DISCORD_INVITE.match(url)):
            raise ValueError("HYPERCHAT_PUBLIC_DISCORD_URL must be https://discord.gg/<code> or "
                             "https://discord.com/invite/<code> (no query, fragment or credentials)")
        return url

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
        """(key id, key secret, webhook secret) of an account in RAZORPAY_ACCOUNTS; "" for unset values."""
        if account not in RAZORPAY_ACCOUNTS:
            raise ValueError("unknown Razorpay account")
        return (_secret_value(getattr(self, f"razorpay_{account}_key_id")),
                _secret_value(getattr(self, f"razorpay_{account}_key_secret")),
                _secret_value(getattr(self, f"razorpay_{account}_webhook_secret")))

    @model_validator(mode="after")
    def _refuse_razorpay_keys_of_the_wrong_mode(self) -> "Settings":
        """Live keys only in production, test keys never in production (D6). Variable names only, never values."""
        production = self.app_env == "production"
        wrong = "rzp_test_" if production else "rzp_live_"
        found = [f"HYPERCHAT_RAZORPAY_{a.upper()}_KEY_ID" for a in RAZORPAY_ACCOUNTS
                 if self.razorpay_credentials(a)[0].startswith(wrong)]
        if found and production:
            raise ValueError(f"Refusing to start: test Razorpay keys in production: {found}")
        if found:
            raise ValueError(f"Refusing to start: live Razorpay keys outside production: {found}")
        return self

    @model_validator(mode="after")
    def _refuse_test_data_in_production(self) -> "Settings":
        label = self.db_label.upper()
        if self.app_env == "production" and ("TEST" in label or "DEV" in label):
            raise ValueError("Refusing to start: production environment pointed at a TEST/DEV database.")
        return self

    @model_validator(mode="after")
    def _refuse_invite_links_in_production(self) -> "Settings":
        # Security audit F6: the accept URL would let the inviting admin set the password of any password-less
        # (e.g. migrated) account without access to its mailbox. A development convenience only.
        if self.app_env == "production" and self.auth_return_invite_links:
            raise ValueError("Refusing to start: HYPERCHAT_AUTH_RETURN_INVITE_LINKS is not allowed in production.")
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
