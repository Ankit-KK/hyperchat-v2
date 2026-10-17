"""Settings of the step-up verifier (deploy/stepup.env.example). Deliberately NOT app.core.config.Settings: the verifier
holds no application secret (no auth secret, Razorpay, Didit, bots, storage or email keys), only where its database
is and which relying party / origins a passkey ceremony must name. Unknown variables are refused, so a copied
api.env cannot start it."""

from functools import lru_cache
from urllib.parse import urlsplit

from psycopg.conninfo import make_conninfo
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_KEYS = {"HYPERCHAT_APP_ENV", "HYPERCHAT_LOG_LEVEL", "HYPERCHAT_DB_HOST", "HYPERCHAT_DB_PORT",
                "HYPERCHAT_DB_NAME", "HYPERCHAT_DB_STEPUP_USER", "HYPERCHAT_DB_LABEL", "HYPERCHAT_WEBAUTHN_RP_ID",
                "HYPERCHAT_WEBAUTHN_RP_NAME", "HYPERCHAT_WEBAUTHN_ORIGINS"}


class StepupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HYPERCHAT_", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    db_host: str = "/var/run/postgresql"
    db_port: int = 5433
    db_name: str = "hyperchat_dev"
    db_stepup_user: str = "hyperchat_stepup"
    db_label: str = "DEV"
    webauthn_rp_id: str
    webauthn_rp_name: str = "HyperChat"
    webauthn_origins: str  # comma-separated exact origins (scheme://host[:port]), e.g. https://hyperchat.space

    @field_validator("webauthn_rp_id")
    @classmethod
    def _rp_id(cls, value: str) -> str:
        value = value.strip().lower()
        if not value or "/" in value or ":" in value or value.startswith(".") or value.endswith("."):
            raise ValueError("HYPERCHAT_WEBAUTHN_RP_ID must be a bare domain")
        return value

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.webauthn_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _origins_match_rp(self):
        """WebAuthn binds a credential to the RP ID: every origin must be that domain or a subdomain, over https
        (http only for localhost in development)."""
        if not self.origins:
            raise ValueError("HYPERCHAT_WEBAUTHN_ORIGINS is empty")
        for origin in self.origins:
            parts = urlsplit(origin)
            host = (parts.hostname or "").lower()
            secure = parts.scheme == "https" or (parts.scheme == "http" and host == "localhost"
                                                 and self.app_env != "production")
            if (not secure or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username
                    or not (host == self.webauthn_rp_id or host.endswith("." + self.webauthn_rp_id))
                    or origin.rstrip("/") != origin):
                raise ValueError(f"origin {origin!r} is not an https origin within {self.webauthn_rp_id}")
        return self

    def conninfo(self) -> str:
        return make_conninfo(host=self.db_host, port=self.db_port, dbname=self.db_name, user=self.db_stepup_user,
                             connect_timeout=5, application_name="hyperchat-stepup")


def unexpected_keys(environ: dict[str, str]) -> list[str]:
    """HYPERCHAT_* variables the verifier must never be given (a secret in its environment defeats the boundary)."""
    return sorted(k for k in environ if k.startswith("HYPERCHAT_") and k not in ALLOWED_KEYS)


@lru_cache
def get_stepup_settings() -> StepupSettings:
    return StepupSettings()
