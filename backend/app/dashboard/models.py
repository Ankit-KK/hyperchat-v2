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


# ── G3 moderation ───────────────────────────────────────────────────────────────

class QueueItem(DonationItem):  # D-R5 row and the D-M2 result
    approved_at: datetime | None
    approved_by_label: str | None
    is_donor_banned: bool


class QueuePage(_Model):  # D-R5
    donations: list[QueueItem]
    pending_count: int
    next_cursor: str | None
    has_more: bool


class ModerationRequest(_Model):  # D-M2 body
    action: Literal["approve", "reject", "hide_message", "unhide_message", "ban_donor"]
    reason: Annotated[StrictStr, Field(max_length=500)] | None = None  # ban_donor only (stored as the ban reason)


class BannedDonor(_Model):  # D-R8 row and the D-M6 result (the donor name is what the creator already sees)
    ban_id: str
    donor_name: str
    banned_by_name: str | None
    reason: str | None
    is_active: bool
    created_at: datetime
    unbanned_at: datetime | None
    unbanned_by_label: str | None


class BannedDonorList(_Model):  # D-R8
    banned_donors: list[BannedDonor]
    total: int
    truncated: bool  # more active bans exist than were returned


class UnbanResult(_Model):  # D-M6
    result: Literal["applied", "unchanged"]  # unchanged: the ban was already lifted
    bans_lifted: int  # active bans of this donor name now inactive (legacy data may hold duplicates)
    ban: BannedDonor


class ModerationResult(_Model):  # D-M2
    result: Literal["applied", "unchanged"]
    action: str
    previous_status: str
    new_status: str
    donation: QueueItem


# ── G4 moderators ───────────────────────────────────────────────────────────────

class Moderator(_Model):  # D-R6 row (Telegram / Discord identities; no web access in Phase 5)
    moderator_id: str
    mod_name: str
    telegram_user_id: str | None
    discord_user_id: str | None
    role: str
    can_approve: bool
    can_reject: bool
    can_hide_message: bool
    can_ban: bool
    can_replay: bool
    is_active: bool
    created_at: datetime
    last_action_at: datetime | None


class ModeratorList(_Model):  # D-R6
    moderators: list[Moderator]


class ModeratorCreate(_Model):  # D-M3 body (permissions keep their column defaults; editing them is D-M7)
    mod_name: Annotated[StrictStr, AfterValidator(v.mod_name)]
    telegram_user_id: Annotated[StrictStr | None, AfterValidator(v.mod_identifier)] = None
    discord_user_id: Annotated[StrictStr | None, AfterValidator(v.mod_identifier)] = None

    @model_validator(mode="after")
    def _one_identifier(self):
        if self.telegram_user_id is None and self.discord_user_id is None:
            raise ValueError("telegram_user_id or discord_user_id is required")
        return self


class ModeratorRemoval(_Model):  # D-M4
    result: Literal["removed", "unchanged"]
    moderator: Moderator


# ── G5 terms ────────────────────────────────────────────────────────────────────

class TermsStatus(_Model):  # D-R7
    current_version: str
    accepted: bool
    is_admin: bool


class TermsAccept(_Model):  # D-M5 body: must name the current version (the client cannot pick another)
    version: Annotated[StrictStr, Field(max_length=40)]


class TermsAccepted(_Model):  # D-M5
    accepted: bool
    current_version: str
