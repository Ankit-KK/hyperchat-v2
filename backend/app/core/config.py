"""Environment-based settings.

Every value comes from environment variables prefixed with ``HYPERCHAT_`` (on the VPS:
``/etc/hyperchat/api.env``, loaded by systemd). Nothing secret is stored in the repository.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from psycopg.conninfo import make_conninfo
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Written by the deploy step next to the code (see deploy/README in backend/README.md).
_RELEASE_FILE = Path(__file__).resolve().parents[2] / "RELEASE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HYPERCHAT_", extra="ignore")

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
    internal_service_secret: SecretStr | None = None  # internal services / workers shared secret
    cron_secret: SecretStr | None = None  # scheduled jobs calling the API

    # ── FastAPI-managed authentication (app.auth) ───────────────────────────────────
    # Signs CSRF tokens and the short-lived OAuth state cookie. Without it, no sessions are issued.
    auth_secret: SecretStr | None = None
    frontend_url: str = "https://hyperchat.space"  # reset/invite links and OAuth redirects
    session_cookie_name: str = "__Host-hc_session"  # __Host-: Secure, Path=/, no Domain
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"  # "none" only for cross-site dev
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

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_redirect_uri)

    @model_validator(mode="after")
    def _refuse_test_data_in_production(self) -> "Settings":
        label = self.db_label.upper()
        if self.app_env == "production" and ("TEST" in label or "DEV" in label):
            raise ValueError("Refusing to start: production environment pointed at a TEST/DEV database.")
        return self

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
