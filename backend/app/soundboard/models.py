"""Soundboard request/response models. Requests reject unknown fields; field sets of responses are fixed."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app.soundboard import rules


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Name = Annotated[StrictStr, Field(max_length=500), AfterValidator(rules.display_name)]
Key = Annotated[StrictStr, Field(max_length=200)]
SlotId = Annotated[StrictStr, Field(max_length=64)]
Order = Annotated[StrictInt, Field(ge=rules.ORDER_MIN, le=rules.ORDER_MAX)]


# ── requests ────────────────────────────────────────────────────────────────────

class SlotRef(_Model):
    id: SlotId


class SlotOrder(_Model):
    id: SlotId
    order_index: Order


class SlotName(_Model):
    id: SlotId
    display_name: Name


class SoundboardChanges(_Model):
    """PATCH body (legacy save-soundboard-changes). Applied in this order: hide, update, reorder, unhide."""
    hide: Annotated[list[SlotRef], Field(max_length=rules.MAX_CHANGES)] = []
    update: Annotated[list[SlotName], Field(max_length=rules.MAX_CHANGES)] = []
    reorder: Annotated[list[SlotOrder], Field(max_length=rules.MAX_CHANGES)] = []
    unhide: Annotated[list[SlotOrder], Field(max_length=rules.MAX_CHANGES)] = []


class UploadRequest(_Model):
    kind: Literal["audio", "image"]
    content_type: Annotated[StrictStr, Field(max_length=100)]
    content_length: StrictInt


class SoundRequest(_Model):
    """Create (POST /slots) and swap (PUT /swappable/{index}): both objects uploaded with presigned URLs.
    The duration is measured by the server; a client-reported one is not accepted."""
    display_name: Name
    audio_key: Key
    image_key: Key


class AudioReplace(_Model):
    audio_key: Key


class ImageReplace(_Model):
    image_key: Key


# ── responses ───────────────────────────────────────────────────────────────────

class Slot(_Model):
    id: str
    source: Literal["platform", "custom"]
    is_visible: bool
    order_index: int | None
    is_locked: bool
    swappable_index: int | None
    last_swapped_at: datetime | None
    display_name: str
    audio_url: str
    audio_duration_s: float
    image_url: str
    image_type: str
    default_order_index: int | None


class Budget(_Model):
    swaps_used: int
    swaps_limit: int
    window_days: int
    next_budget_slot_frees_at: datetime | None
    slot_cooldowns: dict[str, datetime]  # swappable position -> when it can be swapped again


class Board(_Model):
    active: list[Slot]
    hidden: list[Slot]
    revision: int  # latest updated_at of the creator's rows, epoch milliseconds
    budget: Budget


class ChangesApplied(_Model):
    visible_count: int


class Restored(_Model):
    restored: bool


class UploadTicket(_Model):
    upload_url: str
    key: str
    required_headers: dict[str, str]
    expires_in: int


class SlotCreated(_Model):
    slot_id: str


class Swapped(_Model):
    slot_id: str
    slot_cooldown_ends_at: datetime
    swaps_used: int
    swaps_limit: int


class SlotResult(_Model):
    result: Literal["replaced", "removed"]


class Hypersound(_Model):
    id: str
    display_name: str
    audio_url: str
    thumb_url: str | None


class Hypersounds(_Model):
    sounds: list[Hypersound]
