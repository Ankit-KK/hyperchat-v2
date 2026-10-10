"""`python -m app.cli seed-platform-soundboard`: pairing (legacy seed-platform-soundboard), refusals, dry run,
idempotency and atomicity. R2 is the in-memory store; the database tests only run on a scratch database."""

import asyncio

import pytest

from app import cli
from app.cli import seed_platform_soundboard
from app.core.config import Settings
from app.soundboard import seed
from app.storage.memory import InMemoryObjectStore
from tests.integration import db, requires_db

P = "authtest-"  # synthetic keys, removed by cleanup_synthetic_rows


def library(*keys: str) -> InMemoryObjectStore:
    store = InMemoryObjectStore()
    for key in keys:
        store.seed_library(key)
    return store


def test_pairing_follows_legacy():
    store = library("hypersounds/Air-Horn_2.mp3", "hypersounds/boom.mp3", "hypersounds/no-thumb.mp3",
                    "hypersounds/readme.txt", "hypersounds/upper.MP3",
                    "hypersound-thumbs/air-horn_2.webp", "hypersound-thumbs/AIR-HORN_2.png",
                    "hypersound-thumbs/boom.jpeg", "hypersound-thumbs/boom.jpg", "hypersound-thumbs/upper.gif",
                    "hypersound-thumbs/no-thumb.bmp", "hypersound-thumbs/.gif")
    rows, unpaired = seed.plan(sorted(k for k in store.objects if k.startswith("hypersounds/")),
                               sorted(k for k in store.objects if k.startswith("hypersound-thumbs/")),
                               store.public_url)
    assert rows == [
        {"display_name": "Air Horn 2", "audio_key": "hypersounds/Air-Horn_2.mp3",
         "audio_url": "https://media.example.test/hypersounds/Air-Horn_2.mp3", "audio_duration_s": 3.0,
         "image_key": "hypersound-thumbs/AIR-HORN_2.png",  # png beats webp; the stem matches in any case
         "image_url": "https://media.example.test/hypersound-thumbs/AIR-HORN_2.png", "image_type": "png",
         "default_order_index": 0},
        {"display_name": "Boom", "audio_key": "hypersounds/boom.mp3",
         "audio_url": "https://media.example.test/hypersounds/boom.mp3", "audio_duration_s": 3.0,
         "image_key": "hypersound-thumbs/boom.jpg", "image_url": "https://media.example.test/hypersound-thumbs/boom.jpg",
         "image_type": "jpg", "default_order_index": 1}]
    assert unpaired == ["hypersounds/no-thumb.mp3"]  # .bmp is not an image type; .MP3 is not a sound (as legacy)
    jpeg_only, _ = seed.plan(["hypersounds/x.mp3"], ["hypersound-thumbs/x.jpeg"], store.public_url)
    assert jpeg_only[0]["image_type"] == "jpg"  # the enum has no jpeg


@pytest.fixture
def no_db(monkeypatch):
    """The CLI without a database: the table count and the insert are faked and recorded."""
    calls = {"existing": 0, "inserted": None}

    async def existing_slots(connect):
        return calls["existing"]

    async def insert_if_empty(connect, rows):
        calls["inserted"] = rows
        return 0
    monkeypatch.setattr(seed, "existing_slots", existing_slots)
    monkeypatch.setattr(seed, "insert_if_empty", insert_if_empty)
    return calls


def run(store, dry_run=False):
    return asyncio.run(seed_platform_soundboard(Settings(db_enabled=False), dry_run, store=store))


def test_dry_run_lists_and_writes_nothing(no_db, capsys):
    assert run(library("hypersounds/a.mp3", "hypersound-thumbs/a.gif", "hypersounds/b.mp3"), dry_run=True) == 0
    out = capsys.readouterr().out
    assert "skipped (no thumbnail): hypersounds/b.mp3" in out and " 0  A  hypersounds/a.mp3" in out
    assert "Dry run: would insert 1 platform slots" in out and no_db["inserted"] is None


def test_refusals(no_db, monkeypatch, capsys):
    assert run(library("hypersounds/a.mp3")) == 1
    assert "no sound has a matching thumbnail" in capsys.readouterr().err
    too_many = library(*[k for i in range(45) for k in (f"hypersounds/s{i:02d}.mp3", f"hypersound-thumbs/s{i:02d}.png")])
    assert run(too_many) == 1 and "45 paired sounds" in capsys.readouterr().err
    failing = library("hypersounds/a.mp3", "hypersound-thumbs/a.gif")
    failing.fail_with = "TEST-ONLY"
    assert run(failing) == 1 and "Could not list" in capsys.readouterr().err
    monkeypatch.setattr(cli, "build_object_store", lambda settings: None)
    assert run(None) == 2 and "not configured" in capsys.readouterr().err
    assert no_db["inserted"] is None


def test_existing_slots_mean_nothing_to_do(no_db, capsys):
    no_db["existing"] = 44
    store = library("hypersounds/a.mp3", "hypersound-thumbs/a.gif")
    store.fail_with = "TEST-ONLY"  # never even lists the library
    assert run(store) == 0 and "Nothing to do: 44" in capsys.readouterr().out and no_db["inserted"] is None


def test_exactly_44_pairs_are_seeded(no_db):
    store = library(*[k for i in range(44) for k in (f"hypersounds/s{i:02d}.mp3", f"hypersound-thumbs/s{i:02d}.png")])
    assert run(store) == 0
    assert [r["default_order_index"] for r in no_db["inserted"]] == list(range(44))


# ── PostgreSQL (scratch database only: the platform table must be empty) ────────

@pytest.fixture
def empty_platform(strict_scratch_db, env):
    with db() as conn:
        if conn.execute("SELECT count(*) FROM public.platform_soundboard_slots").fetchone()[0]:
            pytest.skip("the scratch database already has platform soundboard slots")


def synthetic(n: int) -> InMemoryObjectStore:
    return library(*[k for i in range(n) for k in (f"hypersounds/{P}s{i:02d}.mp3", f"hypersound-thumbs/{P}s{i:02d}.gif")])


@requires_db
def test_seeds_once_then_does_nothing(empty_platform, capsys):
    store = synthetic(3)
    assert asyncio.run(seed_platform_soundboard(Settings(), True, store=store)) == 0
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.platform_soundboard_slots").fetchone()[0] == 0
    assert asyncio.run(seed_platform_soundboard(Settings(), False, store=store)) == 0
    assert "Inserted 3 platform soundboard slots" in capsys.readouterr().out
    with db() as conn:
        rows = conn.execute("SELECT display_name, audio_key, audio_duration_s::float, image_type::text, "
                            "default_order_index, is_locked FROM public.platform_soundboard_slots "
                            "ORDER BY default_order_index").fetchall()
    assert rows == [(f"Authtest S0{i}", f"hypersounds/{P}s0{i}.mp3", 3.0, "gif", i, False) for i in range(3)]
    assert asyncio.run(seed_platform_soundboard(Settings(), False, store=synthetic(5))) == 0
    assert "Nothing to do: 3" in capsys.readouterr().out


@requires_db
def test_concurrent_seeding_inserts_one_set(empty_platform):
    store = synthetic(4)
    rows, _ = seed.plan(sorted(k for k in store.objects if k.startswith("hypersounds/")),
                        sorted(k for k in store.objects if k.startswith("hypersound-thumbs/")), store.public_url)
    connect = cli.connection_factory(Settings())

    async def race():
        return await asyncio.gather(*(seed.insert_if_empty(connect, rows) for _ in range(4)))
    assert sorted(asyncio.run(race())) == [0, 4, 4, 4]
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.platform_soundboard_slots").fetchone()[0] == 4


@requires_db
def test_a_failing_row_inserts_nothing(empty_platform):
    store = synthetic(2)
    rows, _ = seed.plan(sorted(k for k in store.objects if k.startswith("hypersounds/")),
                        sorted(k for k in store.objects if k.startswith("hypersound-thumbs/")), store.public_url)
    rows[1]["default_order_index"] = 44  # outside platform_default_order_range
    with pytest.raises(Exception, match="platform_default_order_range"):
        asyncio.run(seed.insert_if_empty(cli.connection_factory(Settings()), rows))
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM public.platform_soundboard_slots").fetchone()[0] == 0
