"""Response models for the public read API. Fixed field sets: extra fields are forbidden.

Anonymous donor-page data only. OBS overlay settings and the leaderboard need an OBS token (app.obs); full creator
settings need a dashboard session (app.dashboard)."""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicContact(_Model):  # D5: the allow-list of public contact fields (from HYPERCHAT_PUBLIC_* settings)
    name: str | None
    email: str | None
    phones: list[str]
    discord_url: str | None


class PublicConfig(_Model):
    contact: PublicContact


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


class Sound(_Model):
    id: str  # soundboard slot id (opaque; needed later to pick a sound when donating)
    display_name: str
    audio_url: str
    thumb_url: str | None


class Soundboard(_Model):  # E4
    sounds: list[Sound]


class Goal(_Model):  # E6: everything the tokenless legacy goal overlay renders; all null while no goal is active
    active: bool
    name: str | None
    target_amount_inr: float | None
    progress_inr: float | None
    brand_color: str | None
    goal_overlay_id: int | None


def goal_view(streamer: dict, progress_inr: float | None) -> dict:
    """The public goal (GET .../goal and the public {slug}-goal channel). progress_inr is None when inactive."""
    if progress_inr is None:
        return {"active": False, "name": None, "target_amount_inr": None, "progress_inr": None, "brand_color": None,
                "goal_overlay_id": None}
    target = streamer["goal_target_amount"]
    return {"active": True, "name": streamer["goal_name"], "target_amount_inr": float(target) if target is not None
            else None, "progress_inr": float(progress_inr), "brand_color": streamer["brand_color"],
            "goal_overlay_id": streamer["goal_overlay_id"]}


def goal_active(streamer: dict) -> bool:
    return bool(streamer["goal_is_active"] and streamer["goal_activated_at"])


class PlatformMetrics(_Model):  # E7
    total_interactions: int
    total_alerts: int
    active_streams: int


class ShowcaseMetrics(_Model):  # E8
    streamers_live: int
    alerts_last_24h: int
    last_sync: str
