"""Dashboard moderation: the donation state machine and its use-cases.

Moderation changes database state only (no alerts, TTS, Telegram/Discord or OBS calls).
"""


class ModerationService:
    def __init__(self, repo) -> None:
        self.repo = repo
