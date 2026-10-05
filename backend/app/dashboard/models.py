"""Request and response models for the creator dashboard API.

Fixed field sets (extra fields are forbidden) and snake_case names, as in the public API. Request
models use strict types: booleans must be JSON booleans and integers JSON integers.
"""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")
