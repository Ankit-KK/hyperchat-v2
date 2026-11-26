"""Security audit 2026-11-26: the Discord webhook stops reading an unauthenticated body at 64 KiB (no database)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bots.discord import MAX_BODY
from app.routers import bots


class SeenBot:
    def __init__(self):
        self.bodies = []

    async def handle(self, body, signature, timestamp):
        self.bodies.append(len(body))
        return 200, {"type": 1}


def client():
    app = FastAPI()
    app.include_router(bots.router)
    app.state.discord_bot = SeenBot()
    return TestClient(app), app.state.discord_bot


def test_body_at_the_cap_reaches_the_bot_and_one_byte_more_does_not():
    c, bot = client()
    assert c.post("/api/v1/webhooks/discord", content=b"x" * MAX_BODY).status_code == 200
    assert c.post("/api/v1/webhooks/discord", content=b"x" * (MAX_BODY + 1)).status_code == 413
    assert bot.bodies == [MAX_BODY]


def test_streamed_body_without_a_length_is_cut_at_the_cap():
    c, bot = client()

    def chunks():
        for _ in range(64):
            yield b"x" * 4096  # 256 KiB, chunked: no content-length header
    assert c.post("/api/v1/webhooks/discord", content=chunks()).status_code == 413
    assert c.post("/api/v1/webhooks/discord", content=b"{}", headers={"content-length": "abc"}).status_code == 413
    assert bot.bodies == []
