"""Creator signup interest (legacy SignupDialog, which inserted into public.user_signups directly).

Field rules are the legacy table policy (Supabase migration 20260120004900): name 1-100, email 5-255 and the
same pattern, mobile 8-20 of digits / + - ( ) / spaces, YouTube <= 500, Instagram <= 100. Values are trimmed;
the email is lowercased. Nothing is sent to anyone: rows are read by admins from the database.
"""

import re

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
MOBILE = r"^[0-9+\-\s()]+$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=5, max_length=255, pattern=EMAIL)
    mobile_number: str = Field(min_length=8, max_length=20, pattern=MOBILE)
    youtube_channel: str | None = Field(None, max_length=500)
    instagram_handle: str | None = Field(None, max_length=100)

    @field_validator("name", "youtube_channel", "instagram_handle")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        if value is not None and _CONTROL.search(value):
            raise ValueError("must not contain control characters")
        return value or None  # an empty optional field is stored as NULL, as legacy did

    @field_validator("email")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class SignupRepository:
    def __init__(self, connection_factory) -> None:
        self._conn = connection_factory

    async def add(self, signup: SignupRequest) -> bool:
        """True when stored; False when the email is already registered (in any letter case)."""
        # shortcut: lower(email) scans the table (232 rows on DEV); add an index on lower(email) if it grows large
        async with self._conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                INSERT INTO public.user_signups (name, email, mobile_number, youtube_channel, instagram_handle)
                SELECT %(name)s, %(email)s, %(mobile_number)s, %(youtube_channel)s, %(instagram_handle)s
                WHERE NOT EXISTS (SELECT 1 FROM public.user_signups WHERE lower(email) = %(email)s)
                ON CONFLICT (email) DO NOTHING
            """, signup.model_dump())
            return cur.rowcount == 1
