"""Request and response models for the creator dashboard API.

Fixed field sets (extra fields are forbidden) and snake_case names, as in the public API. Request
models use strict types: booleans must be JSON booleans and integers JSON integers.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.dashboard import validation as v


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── G1 settings ─────────────────────────────────────────────────────────────────

class DashboardSettings(_Model):  # D-R1
    slug: str
    name: str
    status: str  # pending / active / rejected (never the rejection reason)
    moderation_mode: str
    telegram_moderation_enabled: bool
    discord_moderation_enabled: bool
    media_moderation_enabled: bool
    tts_enabled: bool
    tts_language_boost: str | None
    el_voice_id: str | None
    hyperemotes_enabled: bool
    hyperemotes_min_amount: float | None  # read-only (A1)
    media_upload_enabled: bool
    media_min_amount: float | None  # read-only (A1)
    brand_color: str | None
    alert_box_scale: float | None
    alert_overlay_id: int
    leaderboard_overlay_id: int
    goal_overlay_id: int
    overlay_adjust_mode: bool
    leaderboard_bg_enabled: bool
    leaderboard_widget_enabled: bool
    goal_name: str | None
    goal_target_amount: float | None
    goal_is_active: bool
    goal_activated_at: datetime | None


def _overlay(maximum: int):
    return Annotated[StrictInt, Field(ge=0, le=maximum)]


class SettingsPatch(_Model):  # D-M1 body
    """Any subset of the writable settings; an absent field is left unchanged. JSON null is only
    accepted for the nullable settings (goal_activated_at, el_voice_id)."""

    moderation_mode: Literal["auto_approve", "manual"] | None = None
    telegram_moderation_enabled: StrictBool | None = None
    discord_moderation_enabled: StrictBool | None = None
    tts_enabled: StrictBool | None = None
    hyperemotes_enabled: StrictBool | None = None
    alert_box_scale: Annotated[float, Field(strict=True, allow_inf_nan=False),
                               AfterValidator(v.alert_box_scale)] | None = None
    goal_is_active: StrictBool | None = None
    goal_name: Annotated[StrictStr, AfterValidator(v.goal_name)] | None = None
    goal_target_amount: Annotated[float, Field(strict=True, gt=0, le=v.GOAL_TARGET_MAX,
                                               allow_inf_nan=False)] | None = None
    goal_activated_at: Annotated[datetime | None, BeforeValidator(v.aware_timestamp)] = None
    media_upload_enabled: StrictBool | None = None
    media_moderation_enabled: StrictBool | None = None
    tts_language_boost: Annotated[StrictStr, AfterValidator(v.tts_language)] | None = None
    alert_overlay_id: _overlay(v.ALERT_OVERLAY_MAX) | None = None
    leaderboard_overlay_id: _overlay(v.LEADERBOARD_OVERLAY_MAX) | None = None
    overlay_adjust_mode: StrictBool | None = None
    leaderboard_bg_enabled: StrictBool | None = None
    goal_overlay_id: _overlay(v.GOAL_OVERLAY_MAX) | None = None
    brand_color: Annotated[StrictStr, AfterValidator(v.brand_color)] | None = None
    leaderboard_widget_enabled: StrictBool | None = None
    el_voice_id: Annotated[StrictStr | None, AfterValidator(v.el_voice_id)] = None

    @model_validator(mode="before")
    @classmethod
    def _no_nulls(cls, data):
        if isinstance(data, dict):
            nulls = sorted(k for k, val in data.items()
                           if val is None and k in cls.model_fields and k not in v.NULLABLE_SETTINGS)
            if nulls:
                raise ValueError(f"{', '.join(nulls)} cannot be null")
        return data

    def changes(self) -> dict:
        """Only the fields the client sent, with their validated (canonical) values."""
        return {k: getattr(self, k) for k in v.WRITABLE_SETTINGS if k in self.model_fields_set}


class SettingsUpdate(_Model):  # D-M1 response
    settings: DashboardSettings
    changed: list[str]


# ── G2 donations ────────────────────────────────────────────────────────────────

class DonationItem(_Model):  # D-R2 row (hidden messages included: the creator moderates them, D8)
    donation_id: str
    donor_name: str
    amount: float
    currency: str
    amount_inr: float
    message: str | None
    message_visible: bool
    is_hyperemote: bool
    voice_message_url: str | None
    tts_audio_url: str | None
    hypersound_url: str | None
    media_url: str | None
    media_type: str | None
    moderation_status: str
    created_at: datetime


class DonationPage(_Model):  # D-R2
    donations: list[DonationItem]
    next_cursor: str | None
    has_more: bool


class DonationStats(_Model):  # D-R3: every paid donation, any moderation status
    total_revenue_inr: float
    total_donations: int
    today_revenue_inr: float
    month_revenue_inr: float
    timezone: str
    as_of: datetime
