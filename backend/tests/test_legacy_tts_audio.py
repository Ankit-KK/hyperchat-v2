"""Security audit run-4 NV4-6: generated TTS audio still at the pre-keyed, derivable name tts-audio/<creator>/<donation>
.mp3 is listed (count, bytes, referencing donations) and, only with --delete, its donations are pointed at silence and
exactly those objects are deleted (app.maintenance.legacy_tts_audio, app.cli legacy-tts-audio)."""

import asyncio
import uuid

import pytest

from app.cli import connection_factory, legacy_tts
from app.core.config import Settings
from app.maintenance import legacy_tts_audio
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import SILENCE_KEY, StorageError
from tests.integration import db, make_donation, make_streamer, requires_db

BASE = "https://media.example.test"
SID, DID, OTHER = (str(uuid.uuid4()) for _ in range(3))
OLD = f"tts-audio/{SID}/{DID}.mp3"
OLD2 = f"tts-audio/{SID}/{OTHER}.mp3"
KEYED = f"tts-audio/{SID}/{DID}-{'a' * 32}.mp3"
NEVER = (KEYED, f"tts-audio/{SID}/notes.txt", SILENCE_KEY, f"voice-messages/x_1800000000000_{'b' * 32}.webm",
         "hypersounds/boom.mp3")


def store() -> InMemoryObjectStore:
    s = InMemoryObjectStore(BASE)
    for k in (OLD, OLD2, *NEVER):
        s.seed_object(k, data=b"0123456789")
    return s


# ── no database: the store only ever deletes the pre-keyed shape ───────────────────────────────────────────────

@pytest.mark.parametrize("key", NEVER + (f"tts-audio/{SID}/{DID}.mp3/../x", f"TTS-AUDIO/{SID}/{DID}.mp3", ""))
def test_the_store_refuses_to_delete_anything_but_a_pre_keyed_name(key):
    with pytest.raises(ValueError):
        asyncio.run(store().delete_legacy_tts(key))


def test_the_listing_covers_tts_audio_only_with_sizes():
    listed = {o.key: o.size for o in asyncio.run(store().list_tts_audio())}
    assert listed == {OLD: 10, OLD2: 10, KEYED: 10, f"tts-audio/{SID}/notes.txt": 10}


def test_delete_needs_the_connected_databases_name(monkeypatch):
    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql):
            assert sql == "SELECT current_database()"

            class Row:
                async def fetchone(self_inner):
                    return ("hyperchat_dev",)
            return Row()

    monkeypatch.setattr("app.cli.connection_factory", lambda settings: (lambda: Conn()))
    s = store()
    assert asyncio.run(legacy_tts(Settings(), True, None, store=s)) == 2
    assert asyncio.run(legacy_tts(Settings(), True, "hyperchat", store=s)) == 2
    assert s.deleted == []  # refused before anything was listed or changed


# ── with the scratch database: references, then objects ────────────────────────────────────────────────────────

@requires_db
def test_report_counts_and_delete_repoints_references_then_removes_exactly_the_pre_keyed_objects(env, capsys):
    sid = make_streamer("authtest-ltts")
    ref, other_host = make_donation(sid, 100), make_donation(sid, 100)
    with db() as conn:
        conn.execute("UPDATE public.donations SET tts_audio_url = %s WHERE id = %s", (f"{BASE}/{OLD}", ref))
        # an imported legacy row naming the same key on another host is not this bucket's object: untouched
        conn.execute("UPDATE public.donations SET tts_audio_url = %s WHERE id = %s",
                     (f"https://legacy.example.test/{OLD}", other_host))
    s, factory = store(), connection_factory(Settings())
    report = asyncio.run(legacy_tts_audio(s, factory, delete=False))
    assert report == {"scanned": 4, "keyed_kept": 1, "unrecognized_kept": 1, "legacy": 2, "legacy_bytes": 20,
                      "referencing_donations": 1, "repointed_to_silence": 0, "deleted": 0, "delete_failed": 0,
                      "listing_capped": 0}
    assert s.deleted == [] and OLD in s.objects  # report mode changes nothing
    done = asyncio.run(legacy_tts_audio(s, factory, delete=True))
    assert (done["repointed_to_silence"], done["deleted"], done["delete_failed"]) == (1, 2, 0)
    assert sorted(s.deleted) == sorted([OLD, OLD2]) and all(k in s.objects for k in NEVER)
    with db() as conn:
        urls = dict(conn.execute("SELECT id::text, tts_audio_url FROM public.donations WHERE id = ANY(%s::uuid[])",
                                 ([ref, other_host],)).fetchall())
    assert urls == {ref: f"{BASE}/{SILENCE_KEY}", other_host: f"https://legacy.example.test/{OLD}"}
    assert asyncio.run(legacy_tts_audio(s, factory, delete=True))["legacy"] == 0  # idempotent


def test_a_failed_listing_changes_nothing():
    s = store()
    s.fail_with, s.fail_ops = "r2_Unavailable", {"list"}

    def no_database():
        raise AssertionError("the database is never reached after a failed listing")
    with pytest.raises(StorageError):
        asyncio.run(legacy_tts_audio(s, no_database, delete=True))
    assert s.deleted == [] and OLD in s.objects


def test_the_cli_documents_the_command():
    import app.cli
    assert "legacy-tts-audio [--delete --confirm-database DB]" in app.cli.__doc__
