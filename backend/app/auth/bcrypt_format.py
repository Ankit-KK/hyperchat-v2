"""The one definition of a bcrypt hash HyperChat accepts, shared by the sign-in verifier (app.auth.passwords) and the
Supabase hash importer (database/etl/auth_hash_import.py). Standard library only, so the importer can load it.

Accepted: `$2a$` / `$2b$` / `$2y$`, a two-digit cost from 04 to MAX_BCRYPT_COST, `$`, then exactly 53 characters of
bcrypt's base64 alphabet (22 salt + 31 hash): 60 ASCII characters in total, nothing before or after.

MAX_BCRYPT_COST = 12: Supabase Auth (GoTrue) writes cost 10. Measured on the 2-vCPU VPS: cost 10 ~52 ms, 11 ~106 ms,
12 ~213 ms, 13 ~423 ms per verification (Argon2id ~90 ms); every step doubles, so cost 31 would take ~30 hours.
12 leaves two steps of headroom at about 2.4x a normal sign-in. Anything else is never handed to bcrypt.
"""

import re

MAX_BCRYPT_COST = 12
BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
_BCRYPT = re.compile(r"\$2[aby]\$(?:0[4-9]|1[0-2])\$[./A-Za-z0-9]{53}")


def is_supported_bcrypt(value: object) -> bool:
    return isinstance(value, str) and _BCRYPT.fullmatch(value) is not None
