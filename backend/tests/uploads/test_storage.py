"""R2 configuration and the object store. botocore's Stubber intercepts every call: no network, no real
credentials (all values here are TEST-ONLY)."""

import asyncio

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from pydantic import ValidationError

from app.core.config import Settings
from app.storage.memory import InMemoryObjectStore
from app.storage.r2 import IMMUTABLE, R2Config, R2Store, StorageError, build_object_store, check_writable_key

SECRET = "TEST-ONLY-r2-secret-access-key"
KEY_ID = "TEST-ONLY-r2-access-key-id"
FULL = {"r2_account_id": "0123456789abcdef0123456789abcdef", "r2_access_key_id": KEY_ID,
        "r2_secret_access_key": SECRET, "r2_bucket": "hyperchat-dev-media",
        "r2_public_base_url": "https://pub-test.r2.dev"}
VOICE_KEY = "voice-messages/creator-a_1800000000000_" + "a" * 32 + ".webm"
R2_VARS = ["HYPERCHAT_R2_ACCOUNT_ID", "HYPERCHAT_R2_ACCESS_KEY_ID", "HYPERCHAT_R2_SECRET_ACCESS_KEY",
           "HYPERCHAT_R2_BUCKET", "HYPERCHAT_R2_PUBLIC_BASE_URL", "HYPERCHAT_R2_ENDPOINT", "HYPERCHAT_UPLOAD_GRANT_SECRET"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in R2_VARS:
        monkeypatch.delenv(var, raising=False)


# ── settings ────────────────────────────────────────────────────────────────────

def test_unconfigured_by_default_and_reports_names_only():
    s = Settings()
    assert not s.r2_configured and not s.upload_grants_configured
    assert s.r2_config_problems() == ["HYPERCHAT_R2_ACCOUNT_ID", "HYPERCHAT_R2_ACCESS_KEY_ID",
                                      "HYPERCHAT_R2_SECRET_ACCESS_KEY", "HYPERCHAT_R2_BUCKET",
                                      "HYPERCHAT_R2_PUBLIC_BASE_URL"]
    assert build_object_store(s) is None


def test_reads_the_documented_environment_variable_names(monkeypatch):
    for var, value in zip(R2_VARS, [FULL["r2_account_id"], KEY_ID, SECRET, "hyperchat-dev-media",
                                    "https://pub-test.r2.dev/", "https://acc.r2.cloudflarestorage.com", "TEST-ONLY-g"]):
        monkeypatch.setenv(var, value)
    s = Settings()
    assert s.r2_configured and s.upload_grants_configured
    assert s.r2_endpoint_url == "https://acc.r2.cloudflarestorage.com"   # explicit endpoint wins
    assert s.r2_public_base == "https://pub-test.r2.dev"                 # trailing slash dropped


def test_endpoint_derived_from_account_id():
    assert Settings(**FULL).r2_endpoint_url == "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"


@pytest.mark.parametrize("field, value, problem", [
    ("r2_public_base_url", "http://pub-test.r2.dev", "HYPERCHAT_R2_PUBLIC_BASE_URL"),
    ("r2_public_base_url", "https://pub-test.r2.dev/?x=1", "HYPERCHAT_R2_PUBLIC_BASE_URL"),
    ("r2_public_base_url", "https://user:pw@pub-test.r2.dev", "HYPERCHAT_R2_PUBLIC_BASE_URL"),
    ("r2_public_base_url", "https:///nohost", "HYPERCHAT_R2_PUBLIC_BASE_URL"),
    ("r2_endpoint", "https://acc.r2.cloudflarestorage.com/bucket", "HYPERCHAT_R2_ENDPOINT"),
    ("r2_endpoint", "http://acc.r2.cloudflarestorage.com", "HYPERCHAT_R2_ENDPOINT"),
    ("r2_account_id", "evil.example/x", "HYPERCHAT_R2_ACCOUNT_ID"),
    ("r2_bucket", "Bad_Bucket", "HYPERCHAT_R2_BUCKET"),
    ("r2_bucket", "   ", "HYPERCHAT_R2_BUCKET"),
    ("r2_secret_access_key", "  ", "HYPERCHAT_R2_SECRET_ACCESS_KEY"),
])
def test_invalid_values_mean_not_configured_and_never_crash_startup(field, value, problem):
    s = Settings(**{**FULL, field: value})
    assert not s.r2_configured and s.r2_config_problems() == [problem]
    assert build_object_store(s) is None


def test_public_base_may_have_a_path():
    assert Settings(**{**FULL, "r2_public_base_url": "https://media.example.test/hc/"}).r2_public_base \
        == "https://media.example.test/hc"


def test_secrets_never_in_repr_or_validation_errors(monkeypatch):
    s = Settings(**FULL)
    for text in (repr(s), str(s), repr(R2Config.from_settings(s)), repr(build_object_store(s))):
        assert SECRET not in text and KEY_ID not in text
    monkeypatch.setenv("HYPERCHAT_R2_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.setenv("HYPERCHAT_UPLOAD_GRANT_SECRET", "TEST-ONLY-grant-secret-value")
    with pytest.raises(ValidationError) as exc:  # any refused configuration
        Settings(app_env="production", db_label="DEV x")
    assert SECRET not in str(exc.value) and "TEST-ONLY-grant-secret-value" not in str(exc.value)


# ── R2Store (stubbed) ───────────────────────────────────────────────────────────

def stubbed_store() -> tuple[R2Store, Stubber]:
    store = R2Store(R2Config.from_settings(Settings(**FULL)))
    client = store._s3()
    return store, Stubber(client)


def test_client_configuration():
    store, _ = stubbed_store()
    client = store._s3()
    assert client.meta.endpoint_url == "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    assert client.meta.region_name == "auto"
    assert client.meta.config.signature_version == "s3v4"
    assert client.meta.config.request_checksum_calculation == "when_required"
    assert client.meta.config.s3["addressing_style"] == "path"
    assert store._s3() is client  # created once


def test_put_object_sends_exactly_the_expected_request():
    store, stub = stubbed_store()
    stub.add_response("put_object", {}, {"Bucket": "hyperchat-dev-media", "Key": VOICE_KEY, "Body": b"bytes",
                                         "ContentType": "audio/webm", "CacheControl": IMMUTABLE,
                                         "Metadata": {"duration-ms": "1440"}})
    with stub:
        asyncio.run(store.put_object(VOICE_KEY, b"bytes", content_type="audio/webm", metadata={"duration-ms": "1440"}))
    stub.assert_no_pending_responses()
    assert store.public_url(VOICE_KEY) == "https://pub-test.r2.dev/" + VOICE_KEY


@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket", "InternalError"])
def test_r2_errors_become_secret_free_storage_errors(code):
    store, stub = stubbed_store()
    stub.add_client_error("put_object", service_error_code=code, service_message=f"detail {SECRET}",
                          http_status_code=403 if code == "AccessDenied" else 500)
    with stub, pytest.raises(StorageError) as exc:
        asyncio.run(store.put_object(VOICE_KEY, b"x", content_type="audio/webm"))
    assert str(exc.value) == f"r2_{code}"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert SECRET not in repr(exc.value) and KEY_ID not in repr(exc.value)


def test_network_failure_becomes_storage_error(monkeypatch):
    store, _ = stubbed_store()

    def boom(**_):
        raise EndpointConnectionError(endpoint_url="https://0123.r2.cloudflarestorage.com")
    monkeypatch.setattr(store._s3(), "put_object", boom)
    with pytest.raises(StorageError) as exc:
        asyncio.run(store.put_object(VOICE_KEY, b"x", content_type="audio/webm"))
    assert str(exc.value) == "r2_EndpointConnectionError"


@pytest.mark.parametrize("key", [
    "../etc/passwd", "/voice-messages/a_1800000000000_" + "a" * 32 + ".webm", "soundboards/a/x.mp3",
    "hypersounds/boom.mp3", "voice-messages/A_1800000000000_" + "a" * 32 + ".webm",
    "voice-messages/a_1800000000000_" + "a" * 32 + ".mp3", "donation-media/a_1800000000000_" + "a" * 32 + ".svg",
    "donation-media/a/../b_1800000000000_" + "a" * 32 + ".png", "voice-messages/a_18_" + "a" * 32 + ".webm", "",
])
def test_only_server_generated_keys_can_be_written(key):
    store, stub = stubbed_store()
    with stub, pytest.raises(ValueError):
        asyncio.run(store.put_object(key, b"x", content_type="audio/webm"))
    stub.assert_no_pending_responses()  # nothing was sent
    with pytest.raises(ValueError):
        check_writable_key(key)


def test_in_memory_store():
    store = InMemoryObjectStore()
    asyncio.run(store.put_object(VOICE_KEY, b"abc", content_type="audio/webm", metadata={"a": "1"}))
    obj = store.objects[VOICE_KEY]
    assert (obj.data, obj.content_type, obj.cache_control, obj.metadata) == (b"abc", "audio/webm", IMMUTABLE, {"a": "1"})
    assert store.public_url(VOICE_KEY) == "https://media.example.test/" + VOICE_KEY
    store.fail_with = "r2_InternalError"
    with pytest.raises(StorageError):
        asyncio.run(store.put_object(VOICE_KEY, b"abc", content_type="audio/webm"))
    with pytest.raises(ValueError):
        asyncio.run(store.put_object("../x", b"abc", content_type="audio/webm"))
