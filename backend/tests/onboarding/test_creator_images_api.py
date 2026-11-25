"""Creator image uploads (onboarding Stage 4) end to end on PostgreSQL: the real app, R2 replaced by the in-memory
store. Synthetic creators only (slugs authtest-...); rate-limit rows go with the env fixture's cleanup (upload:%)."""

import io
import itertools

import pytest
from PIL import Image

from app.onboarding.images import RATE_BUCKET
from app.routers.creator_images import service_for
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import StorageError
from tests.integration import (
    ORIGIN,
    csrf_headers,
    db,
    make_streamer,
    make_user,
    new_browser,
    owned_creator,
    requires_db,
    update_streamer,
)

pytestmark = requires_db
BASE = "https://media.example.test"
_n = itertools.count(1)


def png(size, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def imgs(app):
    store = InMemoryObjectStore(public_base=BASE)
    service_for(app.app).store = store
    app.store = store
    yield app


def put(client, ref, kind, data, headers, ctype="image/png", params=None):
    return client.put(f"/api/v1/streamers/{ref}/images/{kind}", content=data, params=params,
                      headers={**headers, "content-type": ctype})


def pointers(sid: str) -> tuple:
    with db() as conn:
        return conn.execute("SELECT brand_logo_url, background_url FROM public.streamers WHERE id = %s",
                            (sid,)).fetchone()


def creator(app) -> tuple[str, str, dict]:
    slug = f"authtest-img{next(_n)}"
    sid, h = owned_creator(app, slug, email=f"{slug}@authtest.invalid")
    return sid, slug, h


def other_user(app, **user) -> tuple:
    email = f"img-other{next(_n)}@authtest.invalid"
    make_user(email, **user)
    b = new_browser(app)
    r = b.post("/api/v1/auth/login", json={"email": email, "password": "Correct-Horse-9"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return b, csrf_headers(r)


def test_owner_uploads_both_images_and_the_public_api_serves_them(imgs):
    sid, slug, h = creator(imgs)
    r = put(imgs, sid, "profile", png((1500, 1500)), h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert [(v["width"], v["height"]) for v in body["variants"]] == [(1024, 1024), (256, 256)]
    assert body["quality_warning"] is False and body["url"] == body["variants"][0]["url"]
    assert body["url"].startswith(f"{BASE}/creator-assets/{sid}/logo/") and body["url"].endswith(".webp")
    r = put(imgs, slug, "background", png((4000, 2000)), h)
    assert r.status_code == 200, r.text
    bg = r.json()
    assert [(v["width"], v["height"]) for v in bg["variants"]] == [(1920, 1080), (2560, 1440)]
    assert pointers(sid) == (body["url"], bg["url"])
    assert len(imgs.store.objects) == 4
    listed = imgs.get(f"/api/v1/streamers/{sid}/images")
    assert listed.status_code == 200
    assert listed.json() == {"profile": {"url": body["url"]}, "background": {"url": bg["url"]}}
    public = imgs.get(f"/api/v1/public/streamers/{slug}").json()
    assert (public["brand_logo_url"], public["background_url"]) == (body["url"], bg["url"])


def test_linked_account_may_upload(imgs):
    sid, _, _ = creator(imgs)
    b, h = other_user(imgs, streamer_id=sid)
    r = put(b, sid, "profile", png((300, 300)), h)
    assert r.status_code == 200, r.text
    assert r.json()["quality_warning"] is True
    assert pointers(sid)[0] == r.json()["url"]


def test_stranger_403_and_anonymous_401(imgs):
    sid, _, _ = creator(imgs)
    other_sid = make_streamer(f"authtest-imgx{next(_n)}")
    b, h = other_user(imgs, streamer_id=other_sid)
    assert put(b, sid, "profile", png((300, 300)), h).status_code == 403
    anon = new_browser(imgs)
    assert anon.put(f"/api/v1/streamers/{sid}/images/profile", content=png((300, 300)),
                    headers={**ORIGIN, "content-type": "image/png"}).status_code == 401
    assert anon.get(f"/api/v1/streamers/{sid}/images").status_code == 401
    assert pointers(sid) == (None, None) and imgs.store.objects == {}


def test_csrf_required(imgs):
    sid, _, h = creator(imgs)
    assert put(imgs, sid, "profile", png((300, 300)), {"origin": h["origin"]}).status_code == 403
    assert put(imgs, sid, "profile", png((300, 300)), {**h, "origin": "https://evil.example"}).status_code == 403
    assert pointers(sid) == (None, None)


def test_pending_creator_may_upload(imgs):
    sid, _, h = creator(imgs)
    update_streamer(sid, status="pending")
    r = put(imgs, sid, "background", png((1920, 1080)), h)
    assert r.status_code == 200, r.text
    assert pointers(sid)[1] == r.json()["url"]


def test_validation_failures_leave_nothing(imgs):
    sid, _, h = creator(imgs)
    assert put(imgs, sid, "background", png((1800, 1000)), h).status_code == 422
    assert put(imgs, sid, "profile", b"GIF89a" + b"\0" * 64, h, ctype="image/gif").status_code == 415
    assert put(imgs, sid, "profile", png((100, 100)), h, ctype="image/jpeg").status_code == 415
    assert put(imgs, sid, "background", png((4000, 2250)), h,
               params={"crop_x": 0, "crop_y": 0, "crop_width": 100}).status_code == 400
    assert pointers(sid) == (None, None) and imgs.store.objects == {}


def test_client_crop(imgs):
    sid, _, h = creator(imgs)
    r = put(imgs, sid, "background", png((4000, 2250)), h,
            params={"crop_x": 100, "crop_y": 50, "crop_width": 2560, "crop_height": 1440})
    assert r.status_code == 200, r.text
    assert [(v["width"], v["height"]) for v in r.json()["variants"]] == [(1920, 1080), (2560, 1440)]


def test_rate_limit_20_per_creator_per_day(imgs):
    sid, _, h = creator(imgs)
    with db() as conn:
        conn.execute("INSERT INTO public.rate_limits (ip_address, endpoint, request_count, window_start) "
                     "VALUES (%s, %s, 20, now())", (f"creator:{sid}", RATE_BUCKET))
    r = put(imgs, sid, "profile", png((300, 300)), h)
    assert r.status_code == 429 and r.json()["error"]
    assert pointers(sid) == (None, None)


def test_failed_upload_keeps_the_pointer_and_deletes_its_objects(imgs):
    sid, _, h = creator(imgs)
    first = put(imgs, sid, "profile", png((900, 900)), h).json()["url"]
    live = set(imgs.store.objects)

    class FailSecondPut(InMemoryObjectStore):
        puts = 0

        async def put_object(self, key, data, **kw):
            FailSecondPut.puts += 1
            if FailSecondPut.puts == 2:
                raise StorageError("r2_InternalError")
            await super().put_object(key, data, **kw)

    failing = FailSecondPut(public_base=BASE)
    failing.objects = imgs.store.objects
    service_for(imgs.app).store = failing
    r = put(imgs, sid, "profile", png((900, 900), color=(1, 2, 3)), h)
    assert r.status_code == 502 and r.json()["error"]
    assert pointers(sid)[0] == first
    assert set(failing.objects) == live and len(failing.deleted) == 1


def test_replacement_swaps_the_pointer_and_keeps_the_old_objects(imgs):
    sid, slug, h = creator(imgs)
    first = put(imgs, sid, "profile", png((900, 900)), h).json()["url"]
    second = put(imgs, sid, "profile", png((900, 900), color=(0, 0, 255)), h).json()["url"]
    assert first != second and pointers(sid)[0] == second
    assert first[len(BASE) + 1:] in imgs.store.objects  # superseded objects stay (Phase B cleans them up)
    assert imgs.get(f"/api/v1/public/streamers/{slug}").json()["brand_logo_url"] == second


def test_same_image_again_reuses_its_objects(imgs):
    sid, _, h = creator(imgs)
    a = put(imgs, sid, "profile", png((900, 900)), h).json()
    b = put(imgs, sid, "profile", png((900, 900)), h).json()
    assert a["url"] == b["url"] and len(imgs.store.objects) == 2


def test_r2_not_configured_is_503(imgs):
    sid, _, h = creator(imgs)
    service_for(imgs.app).store = None
    assert put(imgs, sid, "profile", png((300, 300)), h).status_code == 503
