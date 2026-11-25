"""Creator image pipeline units (onboarding Stage 4): generated images only, no database, no network."""

import asyncio
import hashlib
import io

import pytest
from PIL import Image

from app.core.errors import ApiError
from app.onboarding import images as m
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import WRITABLE_KEY, check_creator_asset_deletable

SID = "11111111-2222-4333-8444-555555555555"


def encode(size=(64, 48), fmt="PNG", mode="RGB", color=(200, 30, 30), **save) -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color if mode != "RGBA" else (*color, 128)).save(out, fmt, **save)
    return out.getvalue()


TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def run(kind, data, ctype="image/png", crop=None):
    return m.process(kind, data, ctype, crop)


def reason(exc_info) -> str:
    return exc_info.value.reason


def decoded(v: m.Variant) -> Image.Image:
    im = Image.open(io.BytesIO(v.data))
    im.load()
    return im


# ── declared type and size (before the body is read) ─────────────────────────────

@pytest.mark.parametrize("ctype", ["image/png", "image/jpeg", "image/jpg", "image/webp", "IMAGE/PNG; x=1"])
def test_declared_types_allowed(ctype):
    assert m.check_declared("profile", ctype, "100") == 5 * m.MB


@pytest.mark.parametrize("ctype", [None, "", "image/gif", "image/svg+xml", "video/mp4", "application/octet-stream"])
def test_declared_type_refused(ctype):
    with pytest.raises(ApiError) as e:
        m.check_declared("profile", ctype, "100")
    assert e.value.status == 415 and reason(e) == "image_type_rejected"


@pytest.mark.parametrize("kind,limit", [("profile", 5 * m.MB), ("background", 10 * m.MB)])
def test_oversized_content_length_refused_before_reading(kind, limit):
    assert m.check_declared(kind, "image/png", str(limit)) == limit
    with pytest.raises(ApiError) as e:
        m.check_declared(kind, "image/png", str(limit + 1))
    assert e.value.status == 413 and reason(e) == "image_too_large"


@pytest.mark.parametrize("value", ["-1", "1e3", "١٢", "abc"])
def test_bad_content_length(value):
    with pytest.raises(ApiError) as e:
        m.check_declared("profile", "image/png", value)
    assert reason(e) == "bad_content_length"


def test_body_over_the_cap_refused_even_without_content_length():
    with pytest.raises(ApiError) as e:
        run("profile", b"\x89PNG" + b"0" * (5 * m.MB))
    assert reason(e) == "image_too_large"


# ── sniff, decode ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_each_allowed_format_decodes(fmt):
    p = run("profile", encode(fmt=fmt), TYPES[fmt])
    assert p.variants and all(v.data[:4] == b"RIFF" and v.data[8:12] == b"WEBP" for v in p.variants)


def test_gif_refused_by_signature():
    with pytest.raises(ApiError) as e:
        run("profile", encode(fmt="GIF"), "image/png")
    assert e.value.status == 415 and reason(e) == "image_type_rejected"


def test_declared_type_must_match_bytes():
    with pytest.raises(ApiError) as e:
        run("profile", encode(fmt="JPEG"), "image/png")
    assert reason(e) == "image_signature_mismatch"


def test_corrupt_file_refused():
    data = encode(size=(300, 300), fmt="PNG")
    with pytest.raises(ApiError) as e:
        run("profile", data[: len(data) // 2])
    assert e.value.status == 422 and reason(e) == "image_corrupt"


def test_empty_body():
    with pytest.raises(ApiError) as e:
        run("profile", b"")
    assert reason(e) == "empty_body"


def test_pixel_cap_refused_from_the_header(monkeypatch):
    monkeypatch.setattr(m, "MAX_PIXELS", 100 * 100)
    with pytest.raises(ApiError) as e:
        run("profile", encode(size=(101, 100)))
    assert reason(e) == "image_too_many_pixels"
    assert run("profile", encode(size=(100, 100))).variants


def test_pixel_cap_default_is_40_megapixels():
    assert m.MAX_PIXELS == 40_000_000


# ── metadata ────────────────────────────────────────────────────────────────────

def _with_metadata() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"                    # Make
    exif.get_ifd(0x8825)[2] = (12.0, 34.0, 56.0)   # GPSLatitude
    out = io.BytesIO()
    Image.new("RGB", (80, 60), (10, 200, 10)).save(out, "JPEG", exif=exif, icc_profile=b"\0" * 128)
    return out.getvalue()


def test_exif_gps_and_icc_stripped():
    src = Image.open(io.BytesIO(_with_metadata()))
    assert src.getexif() and "icc_profile" in src.info  # the fixture really carries both
    for v in run("profile", _with_metadata(), "image/jpeg").variants:
        im = decoded(v)
        assert not im.getexif() and "icc_profile" not in im.info and "xmp" not in im.info and "exif" not in im.info
        assert b"Exif" not in v.data and b"ICCP" not in v.data and b"XMP " not in v.data


def test_exif_orientation_applied():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90: stored 80x40, displayed 40x80
    out = io.BytesIO()
    Image.new("RGB", (80, 40)).save(out, "JPEG", exif=exif)
    p = run("profile", out.getvalue(), "image/jpeg")
    assert (p.source_width, p.source_height) == (40, 80)
    assert (p.variants[0].width, p.variants[0].height) == (40, 80)


def test_modes_converted():
    for data, ctype in ((encode(mode="RGBA", fmt="PNG"), "image/png"),
                        (encode(mode="L", fmt="PNG", color=128), "image/png"),
                        (encode(mode="CMYK", fmt="JPEG", color=(0, 50, 50, 0)), "image/jpeg"),
                        (encode(mode="P", fmt="PNG", color=3), "image/png")):
        assert decoded(run("profile", data, ctype).variants[0]).mode in ("RGB", "RGBA")


# ── profile ─────────────────────────────────────────────────────────────────────

def test_profile_variants_1024_and_256():
    p = run("profile", encode(size=(2000, 1000)))
    assert [(v.width, v.height) for v in p.variants] == [(1024, 512), (256, 128)]
    assert not p.quality_warning


def test_profile_never_upscaled_and_small_warns():
    p = run("profile", encode(size=(511, 300)))
    assert [(v.width, v.height) for v in p.variants] == [(511, 300), (256, 150)]
    assert p.quality_warning
    assert not run("profile", encode(size=(512, 100))).quality_warning


def test_tiny_profile_has_one_deduplicated_variant():
    p = run("profile", encode(size=(200, 200)))
    assert len(p.variants) == 1 and (p.variants[0].width, p.variants[0].height) == (200, 200)


def test_profile_refuses_a_crop():
    with pytest.raises(ApiError) as e:
        run("profile", encode(), crop=m.Crop(0, 0, 10, 10))
    assert reason(e) == "bad_crop"


# ── background ──────────────────────────────────────────────────────────────────

def test_background_1800x1000_refused():
    with pytest.raises(ApiError) as e:
        run("background", encode(size=(1800, 1000)))
    assert e.value.status == 422 and reason(e) == "background_too_small"


def test_background_too_small_after_16_9_crop():
    with pytest.raises(ApiError) as e:  # 1920 wide but 1000 high: 16:9 crop is 1777x1000
        run("background", encode(size=(1920, 1000)))
    assert reason(e) == "background_too_small"


def test_background_4000x2000_cropped_to_16_9():
    p = run("background", encode(size=(4000, 2000)))
    assert [(v.width, v.height) for v in p.variants] == [(1920, 1080), (2560, 1440)]
    assert not p.quality_warning


def test_background_exact_minimum_has_only_1920_variant():
    p = run("background", encode(size=(1920, 1080)))
    assert [(v.width, v.height) for v in p.variants] == [(1920, 1080)]


def test_background_between_sizes_never_upscaled():
    p = run("background", encode(size=(2400, 1350)))
    assert [(v.width, v.height) for v in p.variants] == [(1920, 1080)]


def test_background_client_crop_in_source_pixels():
    # left half red, right half blue; crop the right 2000x1125 part
    im = Image.new("RGB", (4000, 2250), (255, 0, 0))
    im.paste((0, 0, 255), (2000, 0, 4000, 2250))
    out = io.BytesIO()
    im.save(out, "PNG")
    p = run("background", out.getvalue(), crop=m.Crop(2000, 0, 2000, 1125))
    v = decoded(p.variants[0])
    assert v.size == (1920, 1080)
    r, g, b = v.getpixel((960, 540))[:3]
    assert b > 200 and r < 50


def test_background_crop_smaller_than_minimum_refused():
    with pytest.raises(ApiError) as e:
        run("background", encode(size=(4000, 2250)), crop=m.Crop(0, 0, 1600, 900))
    assert reason(e) == "background_too_small"


def test_background_crop_outside_image():
    with pytest.raises(ApiError) as e:
        run("background", encode(size=(2000, 1125)), crop=m.Crop(100, 0, 2000, 1125))
    assert e.value.status == 400 and reason(e) == "bad_crop"


@pytest.mark.parametrize("args", [(0, 0, 10, None), (None, 0, 10, 10), (-1, 0, 10, 10), (0, 0, 0, 10)])
def test_parse_crop_refuses_partial_or_bad(args):
    with pytest.raises(ApiError):
        m.parse_crop(*args)


def test_parse_crop_none_and_full():
    assert m.parse_crop(None, None, None, None) is None
    assert m.parse_crop(1, 2, 3, 4) == m.Crop(1, 2, 3, 4)


# ── keys ────────────────────────────────────────────────────────────────────────

def test_content_addressed_key():
    p = run("profile", encode(size=(600, 600)))
    for v in p.variants:
        key = m.asset_key(SID, "profile", v)
        assert key == f"creator-assets/{SID}/logo/{hashlib.sha256(v.data).hexdigest()}.webp"
        assert WRITABLE_KEY.match(key)
    bg = run("background", encode(size=(1920, 1080))).variants[0]
    assert m.asset_key(SID, "background", bg).startswith(f"creator-assets/{SID}/background/")


def test_same_source_gives_the_same_keys():
    a, b = run("profile", encode(size=(700, 500))), run("profile", encode(size=(700, 500)))
    assert [v.sha256 for v in a.variants] == [v.sha256 for v in b.variants]


@pytest.mark.parametrize("key", [
    f"creator-assets/{SID}/logo/{'a' * 64}.png",            # a legacy copy keeps its original extension
    f"creator-assets/{SID}/other/{'a' * 64}.webp",
    f"creator-assets/{SID}/logo/{'a' * 63}.webp",
    f"soundboards/x/{SID}.webp",
    f"creator-assets/../{SID}/logo/{'a' * 64}.webp",
])
def test_delete_is_narrow(key):
    with pytest.raises(ValueError):
        check_creator_asset_deletable(key)


def test_writable_admits_the_s6_scheme_only():
    good = f"creator-assets/{SID}/background/{'0' * 64}.jpg"
    assert WRITABLE_KEY.match(good)
    for bad in (f"creator-assets/{SID}/logo/{'0' * 64}.svg", f"creator-assets/not-a-uuid/logo/{'0' * 64}.webp",
                f"creator-assets/{SID}/logo/{'G' * 64}.webp"):
        assert not WRITABLE_KEY.match(bad)


# ── store writes and failure cleanup (service, without a database) ──────────────

class _Cur:
    def __init__(self, got=True, row=True, fail_update=False):
        self.got, self.row, self.fail_update, self.sql = got, row, fail_update, []

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("UPDATE") and self.fail_update:
            raise RuntimeError("db down")

    async def fetchone(self):
        last = self.sql[-1]
        if "advisory" in last:
            return {"got": self.got}
        return {"id": SID} if self.row else None


class _Ctx:
    """An async context manager yielding `value` (the connection, its transaction, its cursor)."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def transaction(self):
        return _Ctx(None)

    def cursor(self, **kw):
        return _Ctx(self.cur)


def _service(store, cur):
    return m.CreatorImageService(lambda: _Ctx(_Conn(cur)), store, None, asyncio.Semaphore(1))


class FailSecondPut(InMemoryObjectStore):
    def __init__(self):
        super().__init__()
        self.puts = 0

    async def put_object(self, key, data, **kw):
        self.puts += 1
        if self.puts == 2:
            from app.storage.r2 import StorageError
            raise StorageError("r2_InternalError")
        await super().put_object(key, data, **kw)


def test_partial_r2_failure_deletes_what_it_wrote():
    store = FailSecondPut()
    with pytest.raises(ApiError) as e:
        asyncio.run(_service(store, _Cur()).upload(SID, "profile", encode(size=(900, 900)), "image/png", None))
    assert e.value.status == 502 and reason(e) == "storage_failed"
    assert store.objects == {} and len(store.deleted) == 1


def test_db_failure_deletes_new_objects_but_never_an_existing_one():
    data = encode(size=(900, 900))
    store = InMemoryObjectStore()
    first = asyncio.run(_service(store, _Cur()).upload(SID, "profile", data, "image/png", None))
    live = set(store.objects)
    assert len(live) == 2 and first["url"].endswith(".webp")
    # the same image again, but the pointer update fails: the existing (live) objects stay
    with pytest.raises(RuntimeError):
        asyncio.run(_service(store, _Cur(fail_update=True)).upload(SID, "profile", data, "image/png", None))
    assert set(store.objects) == live and store.deleted == []
    # a new image whose pointer update fails: only its own objects go
    with pytest.raises(RuntimeError):
        asyncio.run(_service(store, _Cur(fail_update=True)).upload(SID, "profile", encode(size=(800, 800), color=(9, 9, 9)),
                                                                   "image/png", None))
    assert set(store.objects) == live and len(store.deleted) == 2


def test_upload_in_progress_is_409_and_writes_nothing():
    store = InMemoryObjectStore()
    with pytest.raises(ApiError) as e:
        asyncio.run(_service(store, _Cur(got=False)).upload(SID, "profile", encode(), "image/png", None))
    assert e.value.status == 409 and store.objects == {}


def test_stored_objects_are_webp_and_immutable():
    store = InMemoryObjectStore()
    asyncio.run(_service(store, _Cur()).upload(SID, "background", encode(size=(2560, 1440)), "image/png", None))
    assert len(store.objects) == 2
    for obj in store.objects.values():
        assert obj.content_type == "image/webp" and "immutable" in obj.cache_control
