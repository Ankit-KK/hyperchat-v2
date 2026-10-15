"""Structured security events on the `hyperchat.security` logger.

Only log-safe fields are accepted: never tokens, signatures, nonces, secrets, keys or emails.
"""

import logging

log = logging.getLogger("hyperchat.security")

# Field names that may be attached to a security event. Anything else is dropped.
_ALLOWED_FIELDS = frozenset({
    "reason", "status", "path", "method", "request_id", "client_ip", "user_id",
    "streamer_id", "streamer_slug", "partner_slug", "mechanism", "target_user_id", "invite_id", "count",
    "order_id", "account",  # payments: our order id and the Razorpay account NAME (never a key)
    "previous_user_id",  # D13: the creator's owner before a transfer (an id)
})


def security_event(event: str, *, level: int = logging.WARNING, **fields) -> None:
    safe = {k: v for k, v in fields.items() if k in _ALLOWED_FIELDS and v is not None}
    log.log(level, event, extra={"security_event": event, **safe})
