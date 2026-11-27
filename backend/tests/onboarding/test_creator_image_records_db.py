"""Migration 0039 (onboarding Stage 4 Phase B): creator_image_uploads, the record of every stored creator image, and
the tracked cleanup of superseded and failed objects. The real app on PostgreSQL, R2 replaced by the in-memory store.
Synthetic creators only (authtest- slugs); their rows go with the env fixture's cleanup."""

import asyncio
import json

import psycopg
import pytest

from app.cli import connection_factory
from app.core.config import Settings
from app.onboarding.images import cleanup_images
from app.routers.creator_images import service_for
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import StorageError
from tests.integration import db, fixture_db, make_streamer, requires_db
from tests.onboarding.test_creator_images_api import BASE, creator, imgs, png, pointers, put  # noqa: F401 (imgs)

pytestmark = requires_db
REFUSED = psycopg.errors.InsufficientPrivilege


def records(sid: str, kind: str = "profile") -> list[tuple]:
    with fixture_db() as conn:
        return conn.execute("SELECT state, variants, uploaded_by::text, cleaned_at IS NOT NULL FROM "
                            "public.creator_image_uploads WHERE streamer_id = %s AND kind = %s "
                            "ORDER BY created_at, id", (sid, kind)).fetchall()


def keys(variants) -> set[str]:
    return {v["key"] for v in variants}


def age(sid: str, days: int = 31) -> None:
    with fixture_db() as conn:  # the operator: no guard (superseded 31 days ago)
        conn.execute("UPDATE public.creator_image_uploads SET superseded_at = superseded_at - make_interval(days => %s) "
                     "WHERE streamer_id = %s AND state = 'superseded'", (days, sid))


def clean(store, dry_run: bool = False) -> dict:
    return asyncio.run(cleanup_images(store, connection_factory(Settings()), dry_run=dry_run))


def owner_id(sid: str) -> str:
    with db() as conn:
        return conn.execute("SELECT user_id::text FROM public.streamers WHERE id = %s", (sid,)).fetchone()[0]


# ── the record ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_an_upload_records_every_variant_and_a_replacement_supersedes_it(imgs):
    sid, _, h = creator(imgs)
    first = put(imgs, sid, "profile", png((900, 900)), h).json()
    [(state, variants, by, cleaned)] = records(sid)
    assert (state, by, cleaned) == ("ready", owner_id(sid), False)
    assert [(v["url"], v["width"], v["height"]) for v in variants] == [
        (v["url"], v["width"], v["height"]) for v in first["variants"]]  # 1024 px and 256 px, URLs kept (0039)
    assert all(v["key"] == v["url"][len(BASE) + 1:] for v in variants)
    second = put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h).json()
    assert [r[0] for r in records(sid)] == ["superseded", "ready"]
    assert records(sid)[1][1][0]["url"] == second["url"] == pointers(sid)[0]
    view = imgs.get(f"/api/v1/streamers/{sid}/images", headers=h).json()
    assert view["profile"]["validated"] is True and view["profile"]["url"] == second["url"]
    assert [v["url"] for v in view["profile"]["variants"]] == [v["url"] for v in second["variants"]]
    assert (view["background"]["validated"], view["background"]["variants"]) == (False, [])  # nothing uploaded


def test_a_background_records_its_2560_variant(imgs):
    sid, _, h = creator(imgs)
    r = put(imgs, sid, "background", png((2560, 1440)), h).json()
    [(state, variants, _, _)] = records(sid, "background")
    assert state == "ready" and [(v["width"], v["height"]) for v in variants] == [(1920, 1080), (2560, 1440)]
    assert variants[1]["url"] == r["variants"][1]["url"]


def test_a_pointer_without_its_record_is_not_validated(imgs):
    sid, _, h = creator(imgs)
    put(imgs, sid, "profile", png((900, 900)), h)
    with fixture_db() as conn:  # e.g. a Phase A upload or a legacy copy at the same prefix
        conn.execute("UPDATE public.streamers SET brand_logo_url = %s WHERE id = %s",
                     (f"{BASE}/creator-assets/{sid}/logo/{'e' * 64}.webp", sid))
    assert imgs.get(f"/api/v1/streamers/{sid}/images", headers=h).json()["profile"]["validated"] is False


def test_an_undeletable_object_of_a_failed_upload_is_recorded_for_the_cleanup(imgs):
    sid, _, h = creator(imgs)

    class Stuck(InMemoryObjectStore):
        puts = 0

        async def put_object(self, key, data, **kw):
            Stuck.puts += 1
            if Stuck.puts == 2:
                raise StorageError("r2_InternalError")
            await super().put_object(key, data, **kw)

        async def delete_creator_asset(self, key):
            raise StorageError("r2_InternalError")

    stuck = Stuck(public_base=BASE)
    service_for(imgs.app).store = stuck
    assert put(imgs, sid, "profile", png((900, 900)), h).status_code == 502
    [(state, variants, _, _)] = records(sid)
    assert state == "failed" and keys(variants) == set(stuck.objects)  # the one object the call wrote
    assert pointers(sid)[0] is None
    healthy = InMemoryObjectStore(public_base=BASE)
    healthy.objects = stuck.objects
    counts = clean(healthy)
    assert (counts["deleted"], counts["rows_cleaned"]) == (1, 1) and healthy.objects == {}
    assert records(sid)[0][3] is True


# ── the guard ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_api_roles_only_insert_their_own_keys_and_only_supersede_or_clean_once(imgs):
    sid, _, h = creator(imgs)
    assert put(imgs, sid, "profile", png((900, 900)), h).status_code == 200
    other = make_streamer("authtest-img-guard-other")  # no login: creator() would sign the client in as its owner
    own = f"creator-assets/{sid}/logo/{'a' * 64}.webp"
    insert = ("INSERT INTO public.creator_image_uploads (streamer_id, kind, state, variants, error_code) "
              "VALUES (%s, %s, %s, %s::jsonb, %s)")
    bad_inserts = [
        (sid, "profile", "superseded", [{"key": own}], None),                                 # starts superseded
        (sid, "profile", "failed", [{"key": f"creator-assets/{other}/logo/{'a' * 64}.webp"}], "x"),  # another's key
        (sid, "profile", "failed", [{"key": f"creator-assets/{sid}/background/{'a' * 64}.webp"}], "x"),  # other kind
        (sid, "profile", "failed", [{"key": f"creator-assets/{sid}/logo/{'a' * 64}.jpg"}], "x"),  # a legacy copy
        (sid, "profile", "failed", [{"url": "https://x"}], "x"),                               # no key at all
    ]
    for args in bad_inserts:
        with pytest.raises(REFUSED), db() as conn:
            conn.execute(insert, (*args[:3], json.dumps(args[3]), args[4]))
    with db() as conn:
        conn.execute(insert, (sid, "profile", "failed", json.dumps([{"key": own}]), "test"))
    with db() as conn:
        rid = conn.execute("SELECT id::text FROM public.creator_image_uploads WHERE streamer_id = %s "
                           "AND state = 'ready'", (sid,)).fetchone()[0]
    for sql in ("UPDATE public.creator_image_uploads SET variants = '[{\"key\": \"x\"}]' WHERE id = %s",
                "UPDATE public.creator_image_uploads SET streamer_id = '" + other + "' WHERE id = %s",
                "UPDATE public.creator_image_uploads SET cleaned_at = now() WHERE id = %s",  # a ready row is live
                "UPDATE public.creator_image_uploads SET state = 'failed', error_code = 'x' WHERE id = %s",
                "UPDATE public.creator_image_uploads SET state = 'superseded', superseded_at = now() - "
                "interval '40 days' WHERE id = %s"):  # back-dated: the cleanup would run early
        with pytest.raises((REFUSED, psycopg.errors.CheckViolation)), db() as conn:
            conn.execute(sql, (rid,))
    with pytest.raises(REFUSED), db() as conn:  # never deleted by the API (0039 revokes DELETE)
        conn.execute("DELETE FROM public.creator_image_uploads WHERE id = %s", (rid,))
    with pytest.raises(REFUSED), db("hyperchat_worker") as conn:
        conn.execute("SELECT 1 FROM public.creator_image_uploads LIMIT 1")
    with db() as conn:  # the one legitimate sequence: superseded now, cleaned later, each once
        conn.execute("UPDATE public.creator_image_uploads SET state = 'superseded', superseded_at = now() "
                     "WHERE id = %s", (rid,))
        conn.execute("UPDATE public.creator_image_uploads SET cleaned_at = now() WHERE id = %s", (rid,))
    with pytest.raises(REFUSED), db() as conn:
        conn.execute("UPDATE public.creator_image_uploads SET cleaned_at = now() - interval '1 day' WHERE id = %s",
                     (rid,))


# ── the tracked cleanup ────────────────────────────────────────────────────────────────────────────────────────

def test_superseded_objects_go_after_30_days_and_never_one_still_in_use(imgs):
    sid, _, h = creator(imgs)
    put(imgs, sid, "profile", png((900, 900)), h)
    put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h)
    old = keys(records(sid)[0][1])
    assert clean(imgs.store)["deleted"] == 0 and old <= set(imgs.store.objects)  # under 30 days: kept
    age(sid)
    dry = clean(imgs.store, dry_run=True)
    assert dry["deleted"] == 2 and old <= set(imgs.store.objects) and records(sid)[0][3] is False  # dry run
    counts = clean(imgs.store)
    assert (counts["deleted"], counts["rows_cleaned"]) == (2, 1) and not old & set(imgs.store.objects)
    assert keys(records(sid)[1][1]) <= set(imgs.store.objects)  # the live image stays
    assert clean(imgs.store)["rows"] == 0  # cleaned once


def test_an_image_uploaded_again_keeps_its_objects(imgs):
    sid, _, h = creator(imgs)
    put(imgs, sid, "profile", png((900, 900)), h)
    put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h)
    put(imgs, sid, "profile", png((900, 900)), h)  # the first image again: its keys are live once more
    assert [r[0] for r in records(sid)] == ["superseded", "superseded", "ready"]
    age(sid)
    counts = clean(imgs.store)
    assert counts["kept_referenced"] == 2 and counts["deleted"] == 2 and counts["rows_cleaned"] == 2
    assert keys(records(sid)[2][1]) <= set(imgs.store.objects)


def test_a_failed_delete_leaves_the_row_for_the_next_run_and_a_locked_creator_is_skipped(imgs):
    sid, _, h = creator(imgs)
    put(imgs, sid, "profile", png((900, 900)), h)
    put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h)
    age(sid)

    class NoDelete(InMemoryObjectStore):
        async def delete_creator_asset(self, key):
            raise StorageError("r2_InternalError")

    broken = NoDelete(public_base=BASE)
    broken.objects = imgs.store.objects
    counts = clean(broken)
    assert counts["delete_failed"] == 2 and counts["rows_cleaned"] == 0 and records(sid)[0][3] is False
    with db() as conn, conn.transaction():  # an upload of that creator's profile is in progress
        assert conn.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                            (f"hyperchat:creator-image:{sid}:profile",)).fetchone()[0]
        locked = clean(imgs.store)
    assert locked["skipped_locked"] >= 1 and records(sid)[0][3] is False
    assert clean(imgs.store)["rows_cleaned"] >= 1 and records(sid)[0][3] is True


def test_a_superseded_key_still_named_by_the_pointer_is_kept(imgs):
    sid, _, h = creator(imgs)
    first = put(imgs, sid, "profile", png((900, 900)), h).json()["url"]
    put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h)
    with fixture_db() as conn:  # the operator pointed the creator back at the old image (any host)
        conn.execute("UPDATE public.streamers SET brand_logo_url = %s WHERE id = %s",
                     ("https://legacy.example/" + first[len(BASE) + 1:], sid))
    age(sid)
    counts = clean(imgs.store)
    assert counts["kept_referenced"] >= 1 and first[len(BASE) + 1:] in imgs.store.objects
