"""Request bodies. Unknown fields are refused (422), e.g. a client-chosen media_type or is_hyperemote."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=500)  # sanitised and cut to 100 characters
    amount: Decimal
    currency: str = Field(default="INR", max_length=8)
    message: str | None = Field(default=None, max_length=5000)  # sanitised and cut to 500 characters
    voice_message_url: str | None = Field(default=None, max_length=1000)
    hypersound_url: str | None = Field(default=None, max_length=1000)
    media_url: str | None = Field(default=None, max_length=1000)


class ConfirmRequest(BaseModel):
    """What Razorpay Checkout hands the page after a payment (its handler response)."""
    model_config = ConfigDict(extra="forbid")
    razorpay_payment_id: str = Field(max_length=64)
    razorpay_signature: str = Field(max_length=128)
