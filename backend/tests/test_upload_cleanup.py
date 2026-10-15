"""D9: abandoned donor uploads are deleted after 24 hours unless a donation references them (app.maintenance).
The object store is the in-memory one; donation references come from the scratch database."""

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.maintenance import abandoned_uploads, run_cleanup
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import StorageError
from tests.integration import db, make_donation, make_streamer, requires_db

pytestmark = requires_db
NOW = float(int(time.time()))  # the real clock: run_cleanup itself uses "now"
HOUR = 3600
BASE = "https://media.example.test"
SLUG = "authtest-d9"


def key(prefix: str, age_hours: float, ext: str = "webm") -> str:
    ms = int((NOW - age_hours * HOUR) * 1000)
    return f"{prefix}/{SLUG}_{ms}_{secrets.token_hex(16)}.{ext}"


def store_with(objects: dict[str, float], base: str = BASE) -> InMemoryObjectStore:
    store = InMemoryObjectStore(base, clock=lambda: NOW)
    for k, age_hours in objects.items():
        store.seed_object(k, age_seconds=age_hours * HOUR)
    return store


def reference(url: str, column: str = "voice_message_url") -> None:
    with db() as conn:
        sid = conn.execute("SELECT id::text FROM public.streamers WHERE streamer_slug = %s", (SLUG,)).fetchone()
        sid = sid[0] if sid else make_streamer(SLUG)
    donation = make_donation(sid, 100)
    with db() as conn:
        conn.execute(f"UPDATE public.donations SET {column} = %s WHERE id = %s", (url, donation))


def run(store, *, dry_run: bool = False, factory=None) -> dict:
    return asyncio.run(abandoned_uploads(store, factory or connection_factory(Settings()), dry_run=dry_run,
                                         now=datetime.fromtimestamp(NOW, UTC)))


def test_only_old_unreferenced_donor_uploads_are_deleted(env):
    old_voice, old_media = key("voice-messages", 25), key("donation-media", 30, "png")
    new_voice = key("voice-messages", 23)
    ref_voice, ref_media = key("voice-messages", 48), key("donation-media", 72, "gif")
    never = {  # outside the two donor prefixes: never listed, never deleted, whatever their age
        f"soundboards/{SLUG}/{'0' * 8}-0000-4000-8000-{'0' * 12}.mp3": 999,
        f"tts-audio/{'1' * 8}-0000-4000-8000-{'1' * 12}/{'2' * 8}-0000-4000-8000-{'2' * 12}.mp3": 999,
        "system/silence.mp3": 999, "hypersounds/boom.mp3": 999, "branding/logo.png": 999,
    }
    odd = {"voice-messages/not-an-upload.webm": 999, "donation-media/x_1_y.png": 999}  # unknown shape: kept
    store = store_with({old_voice: 25, old_media: 30, new_voice: 23, ref_voice: 48, ref_media: 72, **never, **odd})
    reference(store.public_url(ref_voice))
    reference(store.public_url(ref_media), "media_url")

    dry = run(store, dry_run=True)
    assert dry == {"scanned": 7, "kept_unrecognized": 2, "kept_too_new": 1, "kept_referenced": 2, "eligible": 2,
                   "deleted": 0, "delete_failed": 0, "listing_capped": 0}
    assert store.deleted == []

    done = run(store)
    assert {k: done[k] for k in ("eligible", "deleted", "delete_failed")} == {"eligible": 2, "deleted": 2,
                                                                             "delete_failed": 0}
    assert sorted(store.deleted) == sorted([old_voice, old_media])
    assert set(store.objects) == {new_voice, ref_voice, ref_media, *never, *odd}
    again = run(store)  # idempotent: nothing more to do
    assert (again["eligible"], again["deleted"], again["scanned"]) == (0, 0, 5)


def test_references_are_matched_by_key_whatever_the_host(env):
    exact, other_host, with_query, unrelated = (key("voice-messages", 30) for _ in range(4))
    store = store_with({exact: 30, other_host: 30, with_query: 30, unrelated: 30})
    reference(store.public_url(exact))
    reference("https://pub-legacy.r2.dev/" + other_host)  # a legacy / not-yet-rewritten URL still protects it
    reference(store.public_url(with_query) + "?v=1")
    reference(f"{BASE}/voice-messages/{SLUG}_1700000000000_{'f' * 32}.webm")  # another key: protects nothing else
    counts = run(store)
    assert (counts["kept_referenced"], counts["deleted"]) == (3, 1) and store.deleted == [unrelated]


def test_age_needs_both_the_listing_and_the_key_to_be_old(env):
    new_in_key = key("voice-messages", 1)    # written recently according to its key ...
    store = store_with({new_in_key: 48})     # ... but listed as old: kept
    old_in_key = key("voice-messages", 48)
    store.seed_object(old_in_key, age_seconds=HOUR)  # old key, recently written: kept
    counts = run(store)
    assert (counts["kept_too_new"], counts["deleted"]) == (2, 0)


@pytest.mark.parametrize("op", ["list"])
def test_a_failed_listing_deletes_nothing(env, op):
    store = store_with({key("voice-messages", 30): 30})
    store.fail_with, store.fail_ops = "r2_InternalError", {op}
    with pytest.raises(StorageError):
        run(store)
    assert store.deleted == []


def test_a_failed_reference_query_deletes_nothing(env):
    store = store_with({key("voice-messages", 30): 30, key("donation-media", 30, "png"): 30})

    @asynccontextmanager
    async def broken():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        run(store, factory=broken)
    assert store.deleted == [] and len(store.objects) == 2


@pytest.mark.parametrize("base", ["http://media.example.test", "not-a-url", ""])
def test_an_unusable_public_base_deletes_nothing(env, base):
    store = store_with({key("voice-messages", 30): 30}, base=base)
    with pytest.raises(StorageError, match="public_base_unusable"):
        run(store)
    assert store.deleted == []


def test_a_failed_delete_is_counted_and_the_rest_continue(env):
    store = store_with({key("voice-messages", 30): 30, key("donation-media", 30, "png"): 30})
    store.fail_with, store.fail_ops = "r2_InternalError", {"delete"}
    counts = run(store)
    assert (counts["eligible"], counts["deleted"], counts["delete_failed"]) == (2, 0, 2)
    assert len(store.objects) == 2


def test_the_cleanup_command_reports_counts_only(strict_scratch_db, env, capsys):
    orphan, kept = key("voice-messages", 30), key("donation-media", 30, "png")
    store = store_with({orphan: 30, kept: 30})
    reference(store.public_url(kept), "media_url")
    settings = Settings()
    assert asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=True, store=store)) == 0
    out = capsys.readouterr().out
    assert "abandoned uploads (dry run)" in out and "would delete 1" in out and store.deleted == []
    assert asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=False, store=store)) == 0
    out = capsys.readouterr().out
    assert "deleted 1" in out and store.deleted == [orphan]
    for secret in (orphan, kept, BASE, SLUG):
        assert secret not in out
    events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert not events or all(SLUG not in json.dumps(e) for e in events)
    store.fail_with, store.fail_ops = "r2_InternalError", {"list"}
    assert asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=False, store=store)) == 1
    assert "failed before deleting anything (r2_InternalError)" in capsys.readouterr().err


def test_the_cleanup_command_skips_uploads_without_r2(strict_scratch_db, env, capsys):
    settings = Settings()
    assert not settings.r2_configured
    assert asyncio.run(run_cleanup(settings, connection_factory(settings), dry_run=True)) == 0
    assert "abandoned uploads: skipped (R2 not configured)" in capsys.readouterr().out
