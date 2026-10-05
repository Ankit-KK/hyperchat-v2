"""Response models for the public read API. Fixed field sets: extra fields are forbidden."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicStreamer(_Model):  # E1
    slug: str
    name: str
    brand_color: str | None
    brand_logo_url: str | None
    background_url: str | None
    tts_enabled: bool
    hyperemotes_enabled: bool
    media_upload_enabled: bool
    media_max_file_size_mb: float | None
    soundboard_enabled: bool
    leaderboard_widget_enabled: bool


class AmountTier(_Model):
    min_amount: float
    max_chars: int


class VoiceTier(_Model):
    min_amount: float
    max_duration_seconds: int


class Pricing(_Model):  # E2
    currency: str
    min_text: float
    min_tts: float
    min_voice: float
    min_hypersound: float
    min_media: float
    tts_enabled: bool
    message_char_tiers: list[AmountTier] | None
    voice_duration_tiers: list[VoiceTier]


class OverlayConfig(_Model):  # E3
    slug: str
    name: str
    brand_color: str | None
    brand_logo_url: str | None
    alert_box_scale: float | None
    alert_overlay_id: int
    leaderboard_overlay_id: int
    goal_overlay_id: int
    overlay_adjust_mode: bool
    leaderboard_widget_enabled: bool
    leaderboard_bg_enabled: bool
    hyperemotes_enabled: bool
    media_upload_enabled: bool
    tts_enabled: bool
    goal_name: str | None
    goal_target_amount: float | None
    goal_activated_at: datetime | None
    goal_is_active: bool


class Sound(_Model):
    id: str  # soundboard slot id (opaque; needed later to pick a sound when donating)
    display_name: str
    audio_url: str
    thumb_url: str | None


class Soundboard(_Model):  # E4
    sounds: list[Sound]


class TopDonor(_Model):
    name: str
    total_amount_inr: float


class Leaderboard(_Model):  # E5
    top_donor: TopDonor | None


class Goal(_Model):  # E6
    active: bool
    name: str | None
    target_amount_inr: float | None
    progress_inr: float


class PlatformMetrics(_Model):  # E7
    total_interactions: int
    total_alerts: int
    active_streams: int


class ShowcaseMetrics(_Model):  # E8
    streamers_live: int
    alerts_last_24h: int
    last_sync: str
