"""Password hashing.

New hashes: Argon2id. Existing hashes: bcrypt ($2a$/$2b$/$2y$ - Supabase Auth and the old custom
login both used bcrypt), verified and transparently upgraded to Argon2id on the next successful login.
"""

import bcrypt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.auth.bcrypt_format import BCRYPT_PREFIXES, is_supported_bcrypt

_argon2 = PasswordHasher()  # library defaults: Argon2id, RFC 9106 low-memory profile
# Hash of a random value, verified against when the account does not exist, so a login for an
# unknown email costs the same time as a real one.
_DUMMY_HASH = _argon2.hash("hyperchat-timing-equalizer")


def hash_password(password: str) -> str:
    return _argon2.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        _argon2_verify(_DUMMY_HASH, password)
        return False
    # Only a complete, supported bcrypt hash (app.auth.bcrypt_format: cost 04-12) is handed to bcrypt; a higher cost or
    # malformed string falls through to the unknown-format path, so no stored hash can make a sign-in unboundedly slow.
    if is_supported_bcrypt(stored_hash):
        # bcrypt only uses the first 72 bytes; bcrypt>=5 raises instead of truncating.
        try:
            return bcrypt.checkpw(password.encode("utf-8")[:72], stored_hash.encode("utf-8"))
        except ValueError:
            return False
    if stored_hash.startswith("$argon2"):
        return _argon2_verify(stored_hash, password)
    _argon2_verify(_DUMMY_HASH, password)  # unknown format: never matches, same cost
    return False


def needs_rehash(stored_hash: str) -> bool:
    if stored_hash.startswith(BCRYPT_PREFIXES):
        return True
    try:
        return _argon2.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def _argon2_verify(stored_hash: str, password: str) -> bool:
    try:
        return _argon2.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_new_password(password: str, min_length: int, max_length: int) -> str | None:
    """Return an error message, or None when acceptable."""
    if not isinstance(password, str) or len(password) < min_length:
        return f"Password must be at least {min_length} characters"
    if len(password) > max_length:
        return f"Password must be at most {max_length} characters"
    if len(password.encode("utf-8")) > 1024:
        return "Password is too long"
    return None
