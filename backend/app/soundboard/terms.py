"""Custom Soundboard terms (owner decision D3, 2026-10-13; text in docs/soundboard-terms.md).

A creator may use the Custom Soundboard only when BOTH separate controls hold:
  * eligibility: streamers.soundboard_enabled, granted / revoked by an admin;
  * acceptance: a row in soundboard_tnc_acceptances for VERSION, made by the creator's CURRENT owner, not revoked.
    Owner-specific (D13, migration 0019: UNIQUE (streamer_id, terms_version, user_id)): after an ownership
    transfer the previous owner's acceptance stays as history and the new owner must accept for themselves; an
    owner who accepted before counts again if the creator returns to them.
Acceptances of older versions (legacy soundboard-2026-07-13 / -07-14) never count. Legacy request-workflow columns
(access_granted, access_granted_at, granted_at) are not used: eligibility lives on streamers.
"""

import re

VERSION = "soundboard-2026-10-13"  # owned by the server; clients can only echo it
assert re.fullmatch(r"[a-z0-9-]+", VERSION)  # inlined into SQL below


def usable_sql(streamer: str) -> str:
    """SQL boolean: the creator row aliased `streamer` may use the Custom Soundboard right now."""
    return (f"({streamer}.soundboard_enabled AND EXISTS (SELECT 1 FROM public.soundboard_tnc_acceptances t "
            f"WHERE t.streamer_id = {streamer}.id AND t.user_id = {streamer}.user_id "
            f"AND t.terms_version = '{VERSION}' AND t.revoked_at IS NULL))")
