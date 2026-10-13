"""Creator soundboard API end to end on PostgreSQL (real app; R2 replaced by the in-memory store, whose
presigned PUTs behave like R2's). Platform soundboard slots are a global table, so these tests only run on a
scratch database (strict_scratch_db), never on hyperchat_dev."""

import itertools
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.storage.memory import InMemoryObjectStore, StoredObject
from tests.integration import (
    ORIGIN,
    accept_soundboard_terms,
    csrf_headers,
    db,
    make_streamer,
    make_user,
    new_browser,
    owned_creator,
    requires_db,
)
from tests.soundboard import mp3_builder as m
from tests.uploads.test_media import GIF, JPEG, MP4, PNG

pytestmark = requires_db
S = "/api/v1/dashboard/streamers"
BASE = "https://pub-dev.r2.dev"
LEGACY = "https://pub-legacy.r2.dev"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SLOT_FIELDS = {"id", "source", "is_visible", "order_index", "is_locked", "swappable_index", "last_swapped_at",
               "display_name", "audio_url", "audio_duration_s", "image_url", "image_type", "default_order_index"}
_n = itertools.count(1)


@pytest.fixture
def sb(app, strict_scratch_db):
    now = [datetime.now(UTC).timestamp()]
    store = InMemoryObjectStore(public_base=BASE, clock=lambda: now[0])
    app.app_state.soundboard.store = store
    app.store, app.clock = store, now
    yield app


def platform(n: int = 3, locked: tuple = (), plain: tuple = ()) -> list[str]:
    """n platform sounds, default positions 0..n-1, swappable positions 1..n (locked/plain ones have none)."""
    ids = []
    with db() as conn:
        for i in range(n):
            swappable = None if i in locked or i in plain else i + 1
            ids.append(conn.execute(
                "INSERT INTO public.platform_soundboard_slots (display_name, audio_key, audio_url, audio_duration_s, "
                "image_key, image_url, image_type, default_order_index, is_locked, swappable_index) "
                "VALUES (%s, %s, %s, 2.5, %s, %s, 'png', %s, %s, %s) RETURNING id::text",
                (f"Plat {i}", f"authtest-hypersounds/p{i}.mp3", f"{LEGACY}/hypersounds/p{i}.mp3",
                 f"authtest-hypersound-thumbs/p{i}.png", f"{LEGACY}/hypersound-thumbs/p{i}.png", i, i in locked,
                 swappable)).fetchone()[0])
    return ids


def creator(app, slug: str, enabled: bool = True) -> tuple[str, dict]:
    """A signed-in owner's creator; enabled = eligible AND the owner accepted the current terms (D3)."""
    sid, h = owned_creator(app, slug, email=f"{slug}{next(_n)}@authtest.invalid", soundboard_enabled=enabled)
    if enabled:
        accept_soundboard_terms(sid)
    return sid, h


def browser(app, role: str = "streamer", **user) -> tuple:
    """A second signed-in user with its own cookie jar: (browser, CSRF headers, user id)."""
    email = f"sb{next(_n)}@authtest.invalid"
    uid = make_user(email, role=role, **user)
    b = new_browser(app)
    r = b.post("/api/v1/auth/login", json={"email": email, "password": "Correct-Horse-9"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return b, csrf_headers(r), uid


def other_creator(app, slug: str) -> tuple:
    """A second creator, signed in on its own browser (the test client keeps the first one's session)."""
    b, h, uid = browser(app)
    sid = make_streamer(slug, user_id=uid)
    with db() as conn:
        conn.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE id = %s", (sid,))
    accept_soundboard_terms(sid)
    return b, h


def upload(app, headers, slug: str, kind: str, data: bytes, ct: str | None = None, client=None) -> str:
    ct = ct or ("audio/mpeg" if kind == "audio" else "image/png")
    r = (client or app).post(f"{S}/{slug}/soundboard/uploads", headers=headers,
                             json={"kind": kind, "content_type": ct, "content_length": len(data)})
    assert r.status_code == 201, r.text
    t = r.json()
    assert app.store.client_put(t["key"], data, t["required_headers"]["Content-Type"]) == 200
    return t["key"]


def pair(app, headers, slug: str, seconds: float = 3.0, image: bytes = PNG) -> dict:
    return {"audio_key": upload(app, headers, slug, "audio", m.mp3(seconds)),
            "image_key": upload(app, headers, slug, "image", image)}


def board(app, slug: str, client=None) -> dict:
    r = (client or app).get(f"{S}/{slug}/soundboard")
    assert r.status_code == 200, r.text
    return r.json()


def rows(slug: str) -> list[dict]:
    with db() as conn:
        cur = conn.execute("SELECT id::text AS id, source::text AS source, is_visible, order_index, display_name, "
                           "audio_key, image_key, swappable_index FROM public.streamer_soundboard_slots "
                           "WHERE streamer_slug = %s ORDER BY id", (slug,))
        names = [c.name for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def custom_row(slug: str, *, order=None, visible=False, audio_key=None, audio_url=None, image_key=None,
               image_url=None, swappable=None) -> str:
    with db() as conn:
        return conn.execute(
            "INSERT INTO public.streamer_soundboard_slots (streamer_slug, source, display_name, audio_key, audio_url, "
            "audio_duration_s, image_key, image_url, image_type, is_visible, order_index, swappable_index) "
            "VALUES (%s, 'custom', 'Legacy', %s, %s, 2, %s, %s, 'png', %s, %s, %s) RETURNING id::text",
            (slug, audio_key or "legacy/a.mp3", audio_url or f"{LEGACY}/a.mp3", image_key or "legacy/a.png",
             image_url or f"{LEGACY}/a.png", visible, order, swappable)).fetchone()[0]


def swaps(slug: str) -> int:
    with db() as conn:
        return conn.execute("SELECT count(*) FROM public.soundboard_swap_log WHERE streamer_slug = %s",
                            (slug,)).fetchone()[0]


# ── authorization, CSRF, Origin, switch ─────────────────────────────────────────

ROUTES = [("GET", "", None), ("PATCH", "", {"hide": [{"id": "00000000-0000-4000-8000-000000000000"}]}),
          ("POST", "/restore-defaults", None),
          ("POST", "/uploads", {"kind": "audio", "content_type": "audio/mpeg", "content_length": 10}),
          ("POST", "/slots", {"display_name": "x", "audio_key": "k", "image_key": "k2"}),
          ("PUT", "/swappable/1", {"display_name": "x", "audio_key": "k", "image_key": "k2"}),
          ("PATCH", "/slots/00000000-0000-4000-8000-000000000000/audio", {"audio_key": "k"}),
          ("PATCH", "/slots/00000000-0000-4000-8000-000000000000/image", {"image_key": "k"}),
          ("DELETE", "/slots/00000000-0000-4000-8000-000000000000", None)]


def call(client, method, slug, path, body, headers=None):
    return client.request(method, f"{S}/{slug}/soundboard{path}", json=body, headers=headers or {})


def test_every_route_needs_a_signed_in_user_with_access(sb):
    platform(2)
    sid, owner_h = creator(sb, "authtest-sb-acl")
    anon = new_browser(sb)
    for method, path, body in ROUTES:
        assert call(anon, method, "authtest-sb-acl", path, body, ORIGIN).status_code == 401, (method, path)
    stranger, sh, _ = browser(sb)
    linked, lh, _ = browser(sb, streamer_id=sid)
    admin, ah, _ = browser(sb, role="admin")
    for method, path, body in ROUTES:
        r = call(stranger, method, "authtest-sb-acl", path, body, sh)
        assert (r.status_code, r.json()) == (403, {"error": "Forbidden"}), (method, path)
    assert board(sb, "authtest-sb-acl", linked)["active"] and board(sb, "authtest-sb-acl", admin)["active"]
    assert call(linked, "POST", "authtest-sb-acl", "/restore-defaults", None, lh).status_code == 200
    unclaimed = make_streamer("authtest-sb-unclaimed")
    with db() as conn:
        conn.execute("UPDATE public.streamers SET soundboard_enabled = true WHERE streamer_slug = 'authtest-sb-unclaimed'")
    assert call(linked, "GET", "authtest-sb-unclaimed", "", None).status_code == 403
    r = call(admin, "GET", "authtest-sb-unclaimed", "", None)  # no owner, so nobody has accepted the terms (D3)
    assert (r.status_code, r.json()["error"]) == (403, "Accept the current Custom Soundboard terms first")
    accept_soundboard_terms(unclaimed)
    assert board(sb, "authtest-sb-unclaimed", admin)["active"]
    assert sb.get(f"{S}/authtest-sb-nobody/soundboard").status_code == 404


def test_soundboard_switch_is_checked_after_access(sb):
    _, h = creator(sb, "authtest-sb-off", enabled=False)
    stranger, sh, _ = browser(sb)
    for method, path, body in ROUTES:
        r = call(sb, method, "authtest-sb-off", path, body, h)
        assert (r.status_code, r.json()) == (403, {"error": "Soundboard not enabled"}), (method, path)
        assert call(stranger, method, "authtest-sb-off", path, body, sh).json() == {"error": "Forbidden"}
        assert call(new_browser(sb), method, "authtest-sb-off", path, body, ORIGIN).status_code == 401
    assert rows("authtest-sb-off") == []  # nothing seeded


def test_writes_need_csrf_and_a_trusted_origin(sb):
    platform(2)
    _, h = creator(sb, "authtest-sb-csrf")
    before = board(sb, "authtest-sb-csrf")
    for method, path, body in ROUTES[1:]:
        for headers in ({}, ORIGIN, {**h, "origin": "https://evil.invalid"}, {"x-csrf-token": h["x-csrf-token"]},
                        {**h, "x-csrf-token": "0" * 43}):
            assert call(sb, method, "authtest-sb-csrf", path, body, headers).status_code == 403, (method, path, headers)
    assert board(sb, "authtest-sb-csrf")["active"] == before["active"] and sb.store.presigned == {}


# ── board ───────────────────────────────────────────────────────────────────────

def test_board_seeds_platform_defaults_once_and_resolves_fields(sb):
    p = platform(3, locked=(0,))
    _, _ = creator(sb, "authtest-sb-read")
    b = board(sb, "authtest-sb-read")
    assert set(b) == {"active", "hidden", "revision", "budget"} and b["hidden"] == []
    assert [s["display_name"] for s in b["active"]] == ["Plat 0", "Plat 1", "Plat 2"]
    assert all(set(s) == SLOT_FIELDS for s in b["active"])
    first = b["active"][0]
    assert first | {"id": None, "last_swapped_at": None} == {
        "id": None, "source": "platform", "is_visible": True, "order_index": 0, "is_locked": True,
        "swappable_index": None, "last_swapped_at": None, "display_name": "Plat 0",
        "audio_url": f"{LEGACY}/hypersounds/p0.mp3", "audio_duration_s": 2.5,
        "image_url": f"{LEGACY}/hypersound-thumbs/p0.png", "image_type": "png", "default_order_index": 0}
    assert [s["swappable_index"] for s in b["active"]] == [None, 2, 3]  # resolved through the platform slot
    assert b["budget"] == {"swaps_used": 0, "swaps_limit": 15, "window_days": 7, "next_budget_slot_frees_at": None,
                           "slot_cooldowns": {}}
    assert isinstance(b["revision"], int) and b["revision"] > 0
    board(sb, "authtest-sb-read")
    assert len(rows("authtest-sb-read")) == 3  # seeded once
    text = sb.get(f"{S}/authtest-sb-read/soundboard").text
    assert "audio_key" not in text and "authtest-hypersounds" not in text and p[0] not in text  # no keys / platform ids
    assert sb.get(f"{S}/authtest-sb-read/soundboard").headers["cache-control"] == "no-store"


# ── PATCH: hide, rename, reorder, unhide ───────────────────────────────────────

def test_hide_rename_reorder_and_unhide(sb):
    platform(4)
    _, h = creator(sb, "authtest-sb-edit")
    a, b_, c, d = (s["id"] for s in board(sb, "authtest-sb-edit")["active"])
    r = sb.patch(f"{S}/authtest-sb-edit/soundboard", headers=h, json={
        "hide": [{"id": a}], "update": [{"id": b_, "display_name": "  Big Drum  "}],
        "reorder": [{"id": c, "order_index": 3}, {"id": d, "order_index": 2}]})  # c and d exchange places
    assert (r.status_code, r.json()) == (200, {"visible_count": 3})
    after = board(sb, "authtest-sb-edit")
    assert [(s["id"], s["order_index"], s["display_name"]) for s in after["active"]] == [
        (b_, 1, "Big Drum"), (d, 2, "Plat 3"), (c, 3, "Plat 2")]
    assert [s["id"] for s in after["hidden"]] == [a]
    r = sb.patch(f"{S}/authtest-sb-edit/soundboard", headers=h, json={"unhide": [{"id": a.upper(), "order_index": 0}]})
    assert r.json() == {"visible_count": 4} and board(sb, "authtest-sb-edit")["active"][0]["id"] == a
    assert board(sb, "authtest-sb-edit")["revision"] >= after["revision"]


def test_locked_platform_sounds_cannot_be_changed(sb):
    platform(3, locked=(0,))
    _, h = creator(sb, "authtest-sb-lock")
    locked, other, _ = (s["id"] for s in board(sb, "authtest-sb-lock")["active"])
    for change in ({"hide": [{"id": locked}]}, {"update": [{"id": locked, "display_name": "X"}]},
                   {"reorder": [{"id": locked, "order_index": 9}]}, {"unhide": [{"id": locked, "order_index": 9}]},
                   {"hide": [{"id": other}], "update": [{"id": locked, "display_name": "X"}]}):
        r = sb.patch(f"{S}/authtest-sb-lock/soundboard", headers=h, json=change)
        assert (r.status_code, r.json()) == (403, {"error": "Locked sounds cannot be changed"}), change
    assert all(s["is_visible"] for s in board(sb, "authtest-sb-lock")["active"])  # the valid hide was rolled back
    r = sb.patch(f"{S}/authtest-sb-lock/soundboard/slots/{locked}/audio", headers=h,
                 json={"audio_key": upload(sb, h, "authtest-sb-lock", "audio", m.mp3(2))})
    assert (r.status_code, r.json()) == (403, {"error": "Locked sounds cannot be changed"})
    assert not [k for k in sb.store.objects if k.startswith("soundboards/")]  # the rejected upload was removed
    r = sb.delete(f"{S}/authtest-sb-lock/soundboard/slots/{locked}", headers=h)
    assert (r.status_code, r.json()) == (403, {"error": "Platform sounds cannot be deleted"})


@pytest.mark.parametrize("change, status", [
    ({}, 400), ({"hide": []}, 400),
    ({"update": [{"id": "{a}", "display_name": "x" * 61}]}, 422), ({"update": [{"id": "{a}", "display_name": "  "}]}, 422),
    ({"update": [{"id": "{a}", "display_name": "bad\nname"}]}, 422), ({"update": [{"id": "{a}"}]}, 422),
    ({"reorder": [{"id": "{a}", "order_index": 46}]}, 422), ({"reorder": [{"id": "{a}", "order_index": -1}]}, 422),
    ({"reorder": [{"id": "{a}", "order_index": "1"}]}, 422), ({"unhide": [{"id": "{a}", "order_index": 1.5}]}, 422),
    ({"hide": [{"id": "{a}"}], "surprise": 1}, 422), ({"hide": [{"id": "{a}", "order_index": 1}]}, 422),
    ({"hide": [{"id": "{a}"}, {"id": "{a}"}]}, 422), ({"hide": [{"id": 5}]}, 422),
    ({"hide": [{"id": "not-a-uuid"}]}, 404), ({"hide": [{"id": "00000000-0000-4000-8000-000000000000"}]}, 404),
    ({"hide": [{"id": "{a}"}], "update": [{"id": "{other}", "display_name": "x"}]}, 404),   # another creator's sound
    ({"hide": [{"id": "{a}"}], "reorder": [{"id": "{a}", "order_index": 5}]}, 409),         # reorder a hidden sound
    ({"hide": [{"id": "{a}"}], "reorder": [{"id": "{b}", "order_index": 2}]}, 409),         # position taken by c
    ({"hide": [{"id": "{a}"}, {"id": "{b}"}], "unhide": [{"id": "{a}", "order_index": 7},
                                                       {"id": "{b}", "order_index": 7}]}, 409),
], ids=lambda v: str(v)[:60] if not isinstance(v, int) else str(v))
def test_patch_validation_and_atomicity(sb, change, status):
    platform(3)
    _, h = creator(sb, "authtest-sb-val")
    other_slug = "authtest-sb-val-other"
    with db() as conn:
        conn.execute("INSERT INTO public.streamers (streamer_slug, streamer_name, soundboard_enabled) "
                     "VALUES (%s, 'Other', true)", (other_slug,))
    other = custom_row(other_slug)
    a, b_, c = (s["id"] for s in board(sb, "authtest-sb-val")["active"])
    before = rows("authtest-sb-val")
    text = str(change).replace("{a}", a).replace("{b}", b_).replace("{other}", other).replace("'", '"')
    import json
    r = sb.patch(f"{S}/authtest-sb-val/soundboard", headers=h, json=json.loads(text.replace("True", "true")))
    assert r.status_code == status, r.text
    assert rows("authtest-sb-val") == before  # nothing applied, even the valid parts


def test_visible_cap_of_45_rolls_back(sb):
    _, h = creator(sb, "authtest-sb-cap")
    board(sb, "authtest-sb-cap")
    for i in range(45):
        custom_row("authtest-sb-cap", order=i, visible=True)
    extra = custom_row("authtest-sb-cap")
    r = sb.patch(f"{S}/authtest-sb-cap/soundboard", headers=h, json={"unhide": [{"id": extra, "order_index": 45}]})
    assert (r.status_code, r.json()) == (409, {"error": "At most 45 sounds can be visible"})
    assert sum(r["is_visible"] for r in rows("authtest-sb-cap")) == 45


def test_restore_defaults(sb):
    platform(4, plain=(3,))  # Plat 3: neither locked nor swappable -> legacy restore leaves it hidden
    _, h = creator(sb, "authtest-sb-restore")
    a, b_, c, d = (s["id"] for s in board(sb, "authtest-sb-restore")["active"])
    custom = custom_row("authtest-sb-restore", order=9, visible=True)
    assert sb.patch(f"{S}/authtest-sb-restore/soundboard", headers=h, json={
        "hide": [{"id": a}], "update": [{"id": b_, "display_name": "Renamed"}],
        "reorder": [{"id": c, "order_index": 5}]}).status_code == 200
    r = sb.post(f"{S}/authtest-sb-restore/soundboard/restore-defaults", headers=h)
    assert (r.status_code, r.json()) == (200, {"restored": True})
    after = board(sb, "authtest-sb-restore")
    assert [(s["id"], s["order_index"], s["display_name"]) for s in after["active"]] == [
        (a, 0, "Plat 0"), (b_, 1, "Plat 1"), (c, 2, "Plat 2")]
    assert {s["id"] for s in after["hidden"]} == {d, custom}
    assert any(r["id"] == custom for r in rows("authtest-sb-restore"))  # custom sounds are kept (hidden)


# ── presigned uploads ───────────────────────────────────────────────────────────

def test_presigned_upload_tickets(sb):
    _, h = creator(sb, "authtest-sb-presign")
    r = sb.post(f"{S}/authtest-sb-presign/soundboard/uploads", headers=h,
                json={"kind": "audio", "content_type": "audio/mp3", "content_length": 1_500_000})
    assert r.status_code == 201 and set(r.json()) == {"upload_url", "key", "required_headers", "expires_in"}
    t = r.json()
    assert re.fullmatch(rf"soundboards/authtest-sb-presign/{UUID.pattern}\.mp3", t["key"])
    assert t["required_headers"] == {"Content-Type": "audio/mpeg"} and t["expires_in"] == 300
    assert "X-Amz-Expires=300" in t["upload_url"] and "content-length%3Bcontent-type%3Bhost" in t["upload_url"]
    assert sb.store.presigned[t["key"]].content_length == 1_500_000
    for ct, ext in (("image/gif", "gif"), ("image/png", "png"), ("image/jpeg", "jpg"), ("image/webp", "webp")):
        r = sb.post(f"{S}/authtest-sb-presign/soundboard/uploads", headers=h,
                    json={"kind": "image", "content_type": ct, "content_length": 3_000_000})
        assert r.status_code == 201 and r.json()["key"].endswith("." + ext)
    for body, status in (({"kind": "audio", "content_type": "audio/wav", "content_length": 10}, 400),
                         ({"kind": "audio", "content_type": "image/png", "content_length": 10}, 400),
                         ({"kind": "image", "content_type": "image/svg+xml", "content_length": 10}, 400),
                         ({"kind": "image", "content_type": "video/mp4", "content_length": 10}, 400),
                         ({"kind": "audio", "content_type": "audio/mpeg", "content_length": 1_500_001}, 400),
                         ({"kind": "image", "content_type": "image/png", "content_length": 3_000_001}, 400),
                         ({"kind": "audio", "content_type": "audio/mpeg", "content_length": 0}, 400),
                         ({"kind": "audio", "content_type": "audio/mpeg", "content_length": "10"}, 422),
                         ({"kind": "video", "content_type": "video/mp4", "content_length": 10}, 422),
                         ({"kind": "audio", "content_type": "audio/mpeg", "content_length": 10, "key": "x"}, 422)):
        assert sb.post(f"{S}/authtest-sb-presign/soundboard/uploads", headers=h, json=body).status_code == status, body
    logs = "\n".join(sb.app_state.log_capture.records)
    assert "X-Amz-Signature" not in logs and "r2.example.test" not in logs  # presigned URLs are never logged


def test_presign_rate_limit_per_user(sb):
    _, h = creator(sb, "authtest-sb-rate")
    body = {"kind": "image", "content_type": "image/png", "content_length": 10}
    codes = [sb.post(f"{S}/authtest-sb-rate/soundboard/uploads", headers=h, json=body).status_code for _ in range(31)]
    assert codes == [201] * 30 + [429]


def test_expired_presigned_url_cannot_be_used(sb):
    _, h = creator(sb, "authtest-sb-expiry")
    r = sb.post(f"{S}/authtest-sb-expiry/soundboard/uploads", headers=h,
                json={"kind": "audio", "content_type": "audio/mpeg", "content_length": len(m.mp3(2))})
    sb.clock[0] += 301
    assert sb.store.client_put(r.json()["key"], m.mp3(2), "audio/mpeg") == 403
    image = upload(sb, h, "authtest-sb-expiry", "image", PNG)
    r = sb.post(f"{S}/authtest-sb-expiry/soundboard/slots", headers=h,
                json={"display_name": "Late", "audio_key": r.json()["key"], "image_key": image})
    assert (r.status_code, r.json()) == (422, {"error": "Uploaded file not found"})


# ── create ──────────────────────────────────────────────────────────────────────

def test_create_custom_sound(sb):
    _, h = creator(sb, "authtest-sb-create")
    keys = pair(sb, h, "authtest-sb-create", seconds=3.0)
    r = sb.post(f"{S}/authtest-sb-create/soundboard/slots", headers=h, json={"display_name": " Horn ", **keys})
    assert r.status_code == 201 and set(r.json()) == {"slot_id"}
    slot = next(s for s in board(sb, "authtest-sb-create")["hidden"] if s["id"] == r.json()["slot_id"])
    assert slot | {"id": None} == {
        "id": None, "source": "custom", "is_visible": False, "order_index": None, "is_locked": False,
        "swappable_index": None, "last_swapped_at": None, "display_name": "Horn",
        "audio_url": f"{BASE}/{keys['audio_key']}", "audio_duration_s": 3.0, "image_url": f"{BASE}/{keys['image_key']}",
        "image_type": "png", "default_order_index": None}
    again = sb.post(f"{S}/authtest-sb-create/soundboard/slots", headers=h, json={"display_name": "Dup", **keys})
    assert (again.status_code, again.json()) == (422, {"error": "This upload was already used"})
    assert keys["audio_key"] in sb.store.objects and keys["image_key"] in sb.store.objects  # in use: never deleted


# ── confirmation of uploaded objects ───────────────────────────────────────────

def test_keys_of_other_creators_platform_donors_or_legacy_are_refused(sb):
    _, h = creator(sb, "authtest-sb-keys")
    bb, hb = other_creator(sb, "authtest-sb-keys-b")
    theirs = upload(sb, hb, "authtest-sb-keys-b", "audio", m.mp3(2), client=bb)
    image = upload(sb, h, "authtest-sb-keys", "image", PNG)
    for key in (theirs, "hypersounds/airhorn.mp3", "voice-messages/authtest-sb-keys_1800000000000_" + "a" * 32 + ".webm",
                f"{LEGACY}/soundboards/authtest-sb-keys/{theirs.rsplit('/', 1)[1]}",
                f"soundboards/authtest-sb-keys/../authtest-sb-keys-b/{theirs.rsplit('/', 1)[1]}",
                image, "soundboards/authtest-sb-keys/x.mp3", ""):
        r = sb.post(f"{S}/authtest-sb-keys/soundboard/slots", headers=h,
                    json={"display_name": "X", "audio_key": key, "image_key": image})
        assert (r.status_code, r.json()) == (422, {"error": "Invalid audio upload key"}), key
    assert theirs in sb.store.objects and sb.store.deleted == []  # nothing of anyone else was touched


@pytest.mark.parametrize("case", ["not_mp3", "too_long", "vbr_trick", "image_mismatch", "video_as_image",
                                  "wrong_stored_type", "oversized_object", "missing_audio", "bad_image_drops_audio"])
def test_uploaded_objects_are_validated_and_rejects_removed(sb, case):
    slug = "authtest-sb-check"
    _, h = creator(sb, slug)
    audio = {"not_mp3": b"\x00" * 9000, "too_long": m.mp3(8.0), "vbr_trick": m.vbr_trick(40)}.get(case, m.mp3(2))
    image = {"image_mismatch": JPEG, "video_as_image": MP4, "bad_image_drops_audio": GIF}.get(case, PNG)
    audio_key = upload(sb, h, slug, "audio", audio) if case != "missing_audio" else \
        sb.post(f"{S}/{slug}/soundboard/uploads", headers=h,
                json={"kind": "audio", "content_type": "audio/mpeg", "content_length": 100}).json()["key"]
    image_key = upload(sb, h, slug, "image", image, ct="image/gif" if case == "bad_image_drops_audio" else "image/png")
    if case == "bad_image_drops_audio":
        sb.store.objects[image_key] = StoredObject(PNG, "image/gif", "", {})  # GIF name, PNG bytes
    if case == "wrong_stored_type":
        sb.store.objects[audio_key] = StoredObject(audio, "audio/ogg", "", {})
    if case == "oversized_object":  # as if R2 had not enforced the signed length
        sb.store.objects[audio_key] = StoredObject(m.mp3(2) + b"\x00" * 1_500_000, "audio/mpeg", "", {})
    r = sb.post(f"{S}/{slug}/soundboard/slots", headers=h,
                json={"display_name": "X", "audio_key": audio_key, "image_key": image_key})
    expected = {"not_mp3": "Sound must be an MP3 file", "too_long": "Sound is longer than 7 seconds",
                "vbr_trick": "Sound is longer than 7 seconds", "image_mismatch": "Image content does not match its type",
                "video_as_image": "Image content does not match its type",
                "wrong_stored_type": "Uploaded file type does not match",
                "oversized_object": "Uploaded file is too large (max 1500000 bytes)",
                "missing_audio": "Uploaded file not found",
                "bad_image_drops_audio": "Image content does not match its type"}[case]
    assert (r.status_code, r.json()) == (422, {"error": expected})
    assert rows(slug) == []
    if case in ("image_mismatch", "video_as_image", "bad_image_drops_audio"):
        assert audio_key not in sb.store.objects and image_key not in sb.store.objects  # both of the pair removed
    elif case != "missing_audio":
        assert audio_key not in sb.store.objects and image_key in sb.store.objects


# ── swap ────────────────────────────────────────────────────────────────────────

def test_swap_takes_the_platform_position_then_cools_down(sb):
    platform(3)
    slug = "authtest-sb-swap"
    _, h = creator(sb, slug)
    a, b_, c = (s["id"] for s in board(sb, slug)["active"])
    keys = pair(sb, h, slug)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/2", headers=h, json={"display_name": "Mine", **keys})
    assert r.status_code == 200 and set(r.json()) == {"slot_id", "slot_cooldown_ends_at", "swaps_used", "swaps_limit"}
    assert (r.json()["swaps_used"], r.json()["swaps_limit"]) == (1, 15)
    after = board(sb, slug)
    assert [(s["id"], s["order_index"]) for s in after["active"]] == [(a, 0), (r.json()["slot_id"], 1), (c, 2)]
    assert [s["id"] for s in after["hidden"]] == [b_]  # the platform sound at position 2 made way
    mine = after["active"][1]
    assert (mine["source"], mine["swappable_index"], mine["display_name"]) == ("custom", 2, "Mine")
    assert set(after["budget"]["slot_cooldowns"]) == {"2"} and after["budget"]["swaps_used"] == 1
    again = pair(sb, h, slug)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/2", headers=h, json={"display_name": "Again", **again})
    assert r.status_code == 429 and r.json()["code"] == "slot_cooldown_active"
    assert set(r.json()) == {"error", "code", "swaps_used", "swaps_limit", "slot_cooldown_ends_at"}
    assert again["audio_key"] not in sb.store.objects and swaps(slug) == 1  # rejected upload removed, nothing logged
    empty = pair(sb, h, slug)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/10", headers=h, json={"display_name": "New", **empty})
    assert r.status_code == 200 and board(sb, slug)["active"][-1]["order_index"] == 3  # empty position: appended


def test_swapping_an_existing_custom_sound_replaces_its_files(sb):
    platform(2)
    slug = "authtest-sb-reswap"
    _, h = creator(sb, slug)
    board(sb, slug)
    first = pair(sb, h, slug)
    slot = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h, json={"display_name": "One", **first}).json()["slot_id"]
    with db() as conn:
        conn.execute("UPDATE public.soundboard_swap_log SET swapped_at = now() - interval '25 hours' "
                     "WHERE streamer_slug = %s", (slug,))
    second = pair(sb, h, slug, seconds=5)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h, json={"display_name": "Two", **second})
    assert r.status_code == 200 and r.json()["slot_id"] == slot and r.json()["swaps_used"] == 2
    assert first["audio_key"] not in sb.store.objects and first["image_key"] not in sb.store.objects
    assert second["audio_key"] in sb.store.objects
    mine = next(s for s in board(sb, slug)["active"] if s["id"] == slot)
    assert mine["display_name"] == "Two" and mine["audio_duration_s"] == pytest.approx(5.0, abs=0.03)  # 26 ms frames


def test_swap_budget_of_15_per_7_days(sb):
    platform(1)
    slug = "authtest-sb-budget"
    _, h = creator(sb, slug)
    board(sb, slug)
    oldest = datetime.now(UTC) - timedelta(days=6)
    with db() as conn:
        for i in range(15):
            conn.execute("INSERT INTO public.soundboard_swap_log (streamer_slug, slot_index, swapped_at) "
                         "VALUES (%s, %s, %s)", (slug, 20 + i, oldest + timedelta(hours=i)))
    keys = pair(sb, h, slug)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h, json={"display_name": "X", **keys})
    assert r.status_code == 429 and r.json()["code"] == "budget_exhausted" and r.json()["swaps_used"] == 15
    frees = datetime.fromisoformat(r.json()["next_budget_slot_frees_at"])
    assert abs((frees - (oldest + timedelta(days=7))).total_seconds()) < 1
    assert keys["audio_key"] not in sb.store.objects and swaps(slug) == 15
    with db() as conn:
        conn.execute("UPDATE public.soundboard_swap_log SET swapped_at = swapped_at - interval '2 days' "
                     "WHERE streamer_slug = %s AND slot_index = 20", (slug,))
    r = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h, json={"display_name": "X", **pair(sb, h, slug)})
    assert r.status_code == 200 and r.json()["swaps_used"] == 15  # one swap left the 7-day window


@pytest.mark.parametrize("index", ["0", "46", "-1", "abc", "1.5", "99999999999999999999"])
def test_swap_positions_1_to_45(sb, index):
    _, h = creator(sb, "authtest-sb-index")
    r = sb.put(f"{S}/authtest-sb-index/soundboard/swappable/{index}", headers=h,
               json={"display_name": "X", "audio_key": "k", "image_key": "k2"})
    assert (r.status_code, r.json()) == (400, {"error": "Invalid slot index (1-45)"})


def test_swap_rolls_back_when_the_visible_cap_is_reached(sb):
    slug = "authtest-sb-swapcap"
    _, h = creator(sb, slug)
    board(sb, slug)
    for i in range(45):
        custom_row(slug, order=i, visible=True)
    before = rows(slug)
    keys = pair(sb, h, slug)
    r = sb.put(f"{S}/{slug}/soundboard/swappable/40", headers=h, json={"display_name": "X", **keys})
    assert (r.status_code, r.json()) == (409, {"error": "At most 45 sounds can be visible"})
    assert rows(slug) == before and swaps(slug) == 0 and keys["audio_key"] not in sb.store.objects


# ── replace ─────────────────────────────────────────────────────────────────────

def test_replace_audio_and_image(sb):
    platform(2)
    slug = "authtest-sb-replace"
    _, h = creator(sb, slug)
    plat = board(sb, slug)["active"][1]["id"]
    keys = pair(sb, h, slug)
    slot = sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "C", **keys}).json()["slot_id"]
    new_audio = upload(sb, h, slug, "audio", m.mp3(6))
    r = sb.patch(f"{S}/{slug}/soundboard/slots/{slot}/audio", headers=h, json={"audio_key": new_audio})
    assert (r.status_code, r.json()) == (200, {"result": "replaced"})
    assert keys["audio_key"] not in sb.store.objects and new_audio in sb.store.objects
    new_image = upload(sb, h, slug, "image", GIF, ct="image/gif")
    assert sb.patch(f"{S}/{slug}/soundboard/slots/{slot}/image", headers=h, json={"image_key": new_image}).status_code == 200
    custom = next(s for s in board(sb, slug)["hidden"] if s["id"] == slot)
    assert (custom["audio_url"], custom["image_type"]) == (f"{BASE}/{new_audio}", "gif")
    assert custom["audio_duration_s"] == pytest.approx(6.0, abs=0.03)
    assert keys["image_key"] not in sb.store.objects
    # an unlocked platform sound gets an override; its platform hypersound is never deleted
    override = upload(sb, h, slug, "audio", m.mp3(1))
    assert sb.patch(f"{S}/{slug}/soundboard/slots/{plat}/audio", headers=h, json={"audio_key": override}).status_code == 200
    assert next(s for s in board(sb, slug)["active"] if s["id"] == plat)["audio_url"] == f"{BASE}/{override}"
    in_use = sb.patch(f"{S}/{slug}/soundboard/slots/{plat}/image", headers=h, json={"image_key": new_image})
    assert (in_use.status_code, in_use.json()) == (422, {"error": "This upload was already used"})
    assert new_image in sb.store.objects


def test_replace_unknown_or_foreign_slot_is_404_and_drops_the_upload(sb):
    slug = "authtest-sb-rep404"
    _, h = creator(sb, slug)
    bb, hb = other_creator(sb, "authtest-sb-rep404-b")
    theirs_keys = {"audio_key": upload(sb, hb, "authtest-sb-rep404-b", "audio", m.mp3(2), client=bb),
                   "image_key": upload(sb, hb, "authtest-sb-rep404-b", "image", PNG, client=bb)}
    theirs = bb.post(f"{S}/authtest-sb-rep404-b/soundboard/slots", headers=hb,
                     json={"display_name": "B", **theirs_keys}).json()["slot_id"]
    for slot in (theirs, "00000000-0000-4000-8000-000000000000"):
        key = upload(sb, h, slug, "audio", m.mp3(2))
        r = sb.patch(f"{S}/{slug}/soundboard/slots/{slot}/audio", headers=h, json={"audio_key": key})
        assert (r.status_code, r.json()) == (404, {"error": "Sound not found"})
        assert key not in sb.store.objects
    assert sb.patch(f"{S}/{slug}/soundboard/slots/not-a-uuid/audio", headers=h, json={"audio_key": "k"}).status_code == 404


# ── delete ──────────────────────────────────────────────────────────────────────

def test_delete_custom_sound_and_its_files(sb):
    platform(1)
    slug = "authtest-sb-del"
    _, h = creator(sb, slug)
    plat = board(sb, slug)["active"][0]["id"]
    keys = pair(sb, h, slug)
    slot = sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "C", **keys}).json()["slot_id"]
    r = sb.delete(f"{S}/{slug}/soundboard/slots/{slot}", headers=h)
    assert (r.status_code, r.json()) == (200, {"result": "removed"})
    assert slot not in {r["id"] for r in rows(slug)}
    assert sorted(sb.store.deleted) == sorted(keys.values())
    assert sb.delete(f"{S}/{slug}/soundboard/slots/{slot}", headers=h).status_code == 404
    assert sb.delete(f"{S}/{slug}/soundboard/slots/{plat}", headers=h).json() == {"error": "Platform sounds cannot be deleted"}
    bb, hb = other_creator(sb, "authtest-sb-del-b")
    assert bb.delete(f"{S}/{slug}/soundboard/slots/{plat}", headers=hb).status_code == 403  # not their creator


def test_platform_donor_legacy_and_foreign_objects_are_never_deleted(sb):
    slug = "authtest-sb-safe"
    _, h = creator(sb, slug)
    board(sb, slug)
    own_legacy = f"soundboards/{slug}/0f8fad5b-d9cb-469f-a165-70867728950e.mp3"
    foreign = "soundboards/authtest-other/0f8fad5b-d9cb-469f-a165-70867728950e.mp3"
    for key in (own_legacy, foreign):
        sb.store.objects[key] = StoredObject(b"x", "audio/mpeg", "", {})
    cases = [  # (audio_key, audio_url) of rows as legacy data or other environments may hold them
        (own_legacy, f"{LEGACY}/{own_legacy}"),                       # this creator's key, but in the legacy bucket
        ("hypersounds/airhorn.mp3", f"{BASE}/hypersounds/airhorn.mp3"),   # platform library
        ("donation-media/x_1800000000000_" + "a" * 32 + ".png", f"{BASE}/donation-media/x.png"),
        (foreign, f"{BASE}/{foreign}"),                                # another creator's prefix
    ]
    for key, url in cases:
        slot = custom_row(slug, audio_key=key, audio_url=url, image_key=key, image_url=url)
        assert sb.delete(f"{S}/{slug}/soundboard/slots/{slot}", headers=h).status_code == 200
    assert sb.store.deleted == [] and own_legacy in sb.store.objects and foreign in sb.store.objects


# ── storage failures and configuration ─────────────────────────────────────────

def test_r2_failures_are_generic_502_and_cleanup_is_best_effort(sb):
    platform(1)
    slug = "authtest-sb-r2fail"
    _, h = creator(sb, slug)
    keys = pair(sb, h, slug)
    sb.store.fail_with, sb.store.fail_ops = "r2_InternalError", {"head"}
    r = sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "X", **keys})
    assert (r.status_code, r.json()) == (502, {"error": "Upload storage failed. Please try again."})
    sb.store.fail_ops = {"presign"}
    r = sb.post(f"{S}/{slug}/soundboard/uploads", headers=h,
                json={"kind": "audio", "content_type": "audio/mpeg", "content_length": 10})
    assert (r.status_code, r.json()) == (502, {"error": "Upload storage failed. Please try again."})
    sb.store.fail_with = None
    slot = sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "X", **keys}).json()["slot_id"]
    sb.store.fail_with, sb.store.fail_ops = "r2_AccessDenied", {"delete"}
    r = sb.delete(f"{S}/{slug}/soundboard/slots/{slot}", headers=h)
    assert r.status_code == 200 and slot not in {r["id"] for r in rows(slug)}  # DB change stands; R2 best-effort
    logs = "\n".join(sb.app_state.log_capture.records)
    assert "soundboard object not removed" in logs and "TEST-ONLY" not in logs


def test_without_r2_uploads_answer_503_and_the_board_still_works(sb):
    platform(1)
    _, h = creator(sb, "authtest-sb-nor2")
    sb.app_state.soundboard.store = None
    assert board(sb, "authtest-sb-nor2")["active"]
    for path, body in (("/uploads", {"kind": "audio", "content_type": "audio/mpeg", "content_length": 10}),
                       ("/slots", {"display_name": "X", "audio_key": "k", "image_key": "k2"})):
        r = sb.post(f"{S}/authtest-sb-nor2/soundboard{path}", headers=h, json=body)
        assert (r.status_code, r.json()) == (503, {"error": "Uploads are not available"})


def test_no_internals_or_presigned_urls_in_error_bodies(sb):
    platform(1)
    slug = "authtest-sb-leak"
    _, h = creator(sb, slug)
    bodies = []
    keys = pair(sb, h, slug)
    for method, path, body in (("POST", "/slots", {"display_name": "X", "audio_key": "../../etc/passwd", "image_key": "k"}),
                               ("POST", "/slots", {"display_name": "X", **keys, "audio_key": keys["image_key"]}),
                               ("PATCH", "", {"hide": [{"id": "'; DROP TABLE x; --"}]}),
                               ("PUT", "/swappable/0", {"display_name": "X", **keys}),
                               ("DELETE", "/slots/00000000-0000-4000-8000-000000000000", None)):
        bodies.append(call(sb, method, slug, path, body, h).text)
    text = "\n".join(bodies)
    for needle in ("Traceback", "psycopg", "SELECT", "/opt/", "X-Amz", "r2.example.test", "streamer_soundboard_slots"):
        assert needle not in text, needle


def test_concurrent_swaps_of_one_position_are_serialized(sb):
    """The per-creator advisory lock: of four simultaneous swaps of the same position exactly one wins; the
    others see its cooldown (without the lock all four could pass the budget/cooldown checks)."""
    import asyncio
    import uuid

    from app.cli import connection_factory
    from app.core.config import Settings
    from app.soundboard.repository import SoundboardRepository, SwapLimited

    platform(1)
    slug = "authtest-sb-race"
    sid, _ = creator(sb, slug)
    board(sb, slug)
    repo = SoundboardRepository(connection_factory(Settings()))

    def sound(i):
        return {"display_name": f"R{i}", "audio_key": f"soundboards/{slug}/{uuid.uuid4()}.mp3", "audio_url": "u",
                "audio_duration_s": 1, "image_key": f"soundboards/{slug}/{uuid.uuid4()}.png", "image_url": "u",
                "image_type": "png"}

    async def race():
        return await asyncio.gather(*(repo.swap(sid, slug, 1, sound(i)) for i in range(4)), return_exceptions=True)
    results = asyncio.run(race())
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, SwapLimited) and r.code == "slot_cooldown_active" for r in results) == 3
    assert swaps(slug) == 1 and sum(r["source"] == "custom" for r in rows(slug)) == 1


def test_losing_a_race_for_an_upload_never_deletes_the_winners_files(sb):
    """Two requests confirm the same keys: the loser passes the pre-check (simulated) and then meets the
    in-transaction check; its failure cleanup must not delete files the winner's slot now uses."""
    slug = "authtest-sb-keyrace"
    _, h = creator(sb, slug)
    keys = pair(sb, h, slug)
    assert sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "Win", **keys}).status_code == 201
    repo = sb.app_state.soundboard.repo
    real, calls = repo.keys_in_use, {"n": 0}

    async def precheck_misses(k):  # the two confirm pre-checks ran before the winner committed
        calls["n"] += 1
        return False if calls["n"] <= 2 else await real(k)
    repo.keys_in_use = precheck_misses
    try:
        r = sb.post(f"{S}/{slug}/soundboard/slots", headers=h, json={"display_name": "Lose", **keys})
    finally:
        del repo.keys_in_use
    assert (r.status_code, r.json()) == (422, {"error": "This upload was already used"})
    assert sb.store.deleted == [] and all(k in sb.store.objects for k in keys.values())


def test_reswapping_a_visible_custom_sound_leaves_a_visible_platform_sound_alone(sb):
    platform(2)
    slug = "authtest-sb-twin"
    _, h = creator(sb, slug)
    p0 = board(sb, slug)["active"][0]["id"]
    slot = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h,
                  json={"display_name": "Mine", **pair(sb, h, slug)}).json()["slot_id"]
    assert p0 in {s["id"] for s in board(sb, slug)["hidden"]}  # first swap took the platform sound's place
    assert sb.patch(f"{S}/{slug}/soundboard", headers=h, json={"unhide": [{"id": p0, "order_index": 5}]}).status_code == 200
    with db() as conn:
        conn.execute("UPDATE public.soundboard_swap_log SET swapped_at = now() - interval '25 hours' "
                     "WHERE streamer_slug = %s", (slug,))
    r = sb.put(f"{S}/{slug}/soundboard/swappable/1", headers=h, json={"display_name": "Mine 2", **pair(sb, h, slug)})
    assert r.status_code == 200 and r.json()["slot_id"] == slot
    active = {s["id"]: s["order_index"] for s in board(sb, slug)["active"]}
    assert active[p0] == 5 and active[slot] == 0  # legacy: the custom sound keeps its place, the platform sound stays
