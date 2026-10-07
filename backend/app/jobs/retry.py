"""Job outcomes and the retry/backoff policy.

A handler returns Done (the job succeeded or was skipped) or raises Retryable / Permanent. Retryable errors
are retried with bounded exponential backoff and jitter until max_attempts; then, like a Permanent error,
the job fails and the handler's fallback runs. Reason codes are short, fixed strings (never provider bodies,
donor text or secrets): they are stored in jobs.last_error and logged.
"""

import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


def reason_code(value: str) -> str:
    return value if isinstance(value, str) and _REASON.match(value) else "error"


@dataclass(frozen=True)
class Backoff:
    """delay = min(cap, base * 2^(attempt-1)) * uniform(1 - jitter, 1 + jitter), never above cap."""
    base_seconds: float
    cap_seconds: float
    jitter: float = 0.2

    def delay(self, attempt: int, retry_after: float | None = None, rng: random.Random | None = None) -> float:
        raw = min(self.cap_seconds, self.base_seconds * 2 ** max(0, attempt - 1))
        jittered = raw * (rng or random).uniform(1 - self.jitter, 1 + self.jitter)
        if retry_after is not None and retry_after > 0:  # a provider's Retry-After wins, within the cap
            jittered = max(jittered, retry_after)
        return round(min(self.cap_seconds, jittered), 3)


class JobError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason_code(reason))
        self.reason = reason_code(reason)


class Retryable(JobError):
    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class Permanent(JobError):
    pass


Effects = Callable[[object], Awaitable[None]]  # runs inside the job's completion transaction


@dataclass
class Done:
    """The job finished. `effects(cursor)` (e.g. writing the donation's audio URL) runs in the same
    transaction that marks the job succeeded, so both happen or neither does."""
    result: dict = field(default_factory=dict)
    effects: Effects | None = None
