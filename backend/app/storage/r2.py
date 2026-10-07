"""Cloudflare R2 through its S3 API (boto3/botocore).

Credentials come from Settings as SecretStr and are only handed to botocore: they are never logged,
never part of an exception and never in a repr. A failed call raises StorageError whose message is a
short reason code (e.g. "r2_AccessDenied"); the API turns it into a generic 502. botocore is blocking,
so network calls run in a worker thread. Presigned URLs are computed locally and never logged.

Every operation is limited to the key patterns it needs, so a bug can never touch an unexpected object:
  write (put_object, presign_put)   server-generated donor upload and creator soundboard keys
  read (head_object, get_object)    creator soundboard keys
  metadata (head_upload)            donor upload keys (voice-messages/, donation-media/): existence, size,
                                    type and stored metadata only, never the bytes (Phase 7 checkout)
  delete (delete_object)            creator soundboard keys ONLY (never hypersounds/, hypersound-thumbs/,
                                    voice-messages/, donation-media/ or anything else)
  list (list_keys)                  the platform library prefixes hypersounds/ and hypersound-thumbs/
"""

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config
from pydantic import SecretStr

from app.core.config import Settings

# botocore logs request details (including signed headers) at DEBUG: keep it quiet whatever the app level.
for _name in ("boto3", "botocore", "s3transfer", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)

_SLUG = r"[a-z0-9][a-z0-9_-]{0,63}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Creator soundboard objects (Phase 6): soundboards/<creator slug>/<uuid>.<ext>
SOUNDBOARD_KEY = re.compile(rf"^soundboards/(?P<slug>{_SLUG})/{_UUID}\.(?P<ext>mp3|jpg|png|webp|gif)$")
# Donor uploads (Phase 6): <prefix>/<slug>_<epoch ms>_<uuid4 hex>.<ext>
_UPLOAD = (rf"voice-messages/{_SLUG}_\d{{13}}_[0-9a-f]{{32}}\.webm"
           rf"|donation-media/{_SLUG}_\d{{13}}_[0-9a-f]{{32}}\.(jpg|png|webp|gif)")
UPLOAD_KEY = re.compile(rf"^({_UPLOAD})$")
# Server-generated keys: donor uploads and soundboard objects.
WRITABLE_KEY = re.compile(rf"^({_UPLOAD}|soundboards/{_SLUG}/{_UUID}\.(mp3|jpg|png|webp|gif))$")
LISTABLE_PREFIXES = ("hypersounds/", "hypersound-thumbs/")
IMMUTABLE = "public, max-age=31536000, immutable"  # keys are unique and never rewritten
PRESIGN_SECONDS = 300
MAX_LISTED_KEYS = 10_000


class StorageError(Exception):
    """An object-store call failed. str() is a secret-free reason code, safe to log."""


@dataclass(frozen=True)
class ObjectHead:
    size: int
    content_type: str
    metadata: dict[str, str] = field(default_factory=dict)  # user metadata (head_upload only)


class ObjectStore(Protocol):
    def public_url(self, key: str) -> str: ...

    async def put_object(self, key: str, data: bytes, *, content_type: str, cache_control: str = IMMUTABLE,
                         metadata: dict[str, str] | None = None) -> None: ...

    def presign_put(self, key: str, *, content_type: str, content_length: int,
                    expires_in: int = PRESIGN_SECONDS) -> str: ...

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def head_upload(self, key: str) -> ObjectHead | None: ...

    async def get_object(self, key: str, *, max_bytes: int) -> bytes: ...

    async def delete_object(self, key: str) -> None: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


def check_writable_key(key: str) -> None:
    if not isinstance(key, str) or not WRITABLE_KEY.match(key):
        raise ValueError("refusing to write an object key outside the server-generated patterns")


def check_soundboard_key(key: str) -> None:
    """Read and delete are only ever allowed on creator soundboard objects."""
    if not isinstance(key, str) or not SOUNDBOARD_KEY.match(key):
        raise ValueError("refusing to read or delete an object outside soundboards/<slug>/<uuid>.<ext>")


def check_upload_key(key: str) -> None:
    """head_upload only ever looks at donor upload objects."""
    if not isinstance(key, str) or not UPLOAD_KEY.match(key):
        raise ValueError("refusing to inspect an object outside the donor upload patterns")


def check_listable_prefix(prefix: str) -> None:
    if prefix not in LISTABLE_PREFIXES:
        raise ValueError("refusing to list outside the platform library prefixes")


def public_url_for(base: str, key: str) -> str:
    return f"{base}/{quote(key, safe='/')}"


@dataclass(frozen=True, repr=False)
class R2Config:
    endpoint_url: str
    bucket: str
    public_base: str
    access_key_id: SecretStr
    secret_access_key: SecretStr

    def __repr__(self) -> str:
        return "R2Config(<redacted>)"

    @classmethod
    def from_settings(cls, settings: Settings) -> "R2Config | None":
        if not settings.r2_configured:
            return None
        return cls(settings.r2_endpoint_url, settings.r2_bucket, settings.r2_public_base,
                   settings.r2_access_key_id, settings.r2_secret_access_key)


def _reason(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    raw = f"r2_{code}" if code else f"r2_{type(exc).__name__}"
    return re.sub(r"[^A-Za-z0-9_]", "", raw)[:64]


def _not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


class R2Store:
    """ObjectStore on one R2 bucket. The boto3 client is created on first use and reused (thread-safe)."""

    def __init__(self, config: R2Config, client=None) -> None:
        self._config = config
        self._client = client
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "R2Store(<redacted>)"

    def public_url(self, key: str) -> str:
        return public_url_for(self._config.public_base, key)

    def _s3(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = boto3.session.Session().client(
                        "s3",
                        endpoint_url=self._config.endpoint_url,
                        region_name="auto",
                        aws_access_key_id=self._config.access_key_id.get_secret_value(),
                        aws_secret_access_key=self._config.secret_access_key.get_secret_value(),
                        config=Config(
                            signature_version="s3v4",
                            s3={"addressing_style": "path"},
                            retries={"max_attempts": 3, "mode": "standard"},
                            connect_timeout=5,
                            read_timeout=30,
                            # R2 does not need the newer default CRC trailers; only send checksums when required.
                            request_checksum_calculation="when_required",
                            response_checksum_validation="when_required",
                        ),
                    )
        return self._client

    async def _call(self, fn):
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a secret-free StorageError
            raise StorageError(_reason(exc)) from None

    async def put_object(self, key: str, data: bytes, *, content_type: str, cache_control: str = IMMUTABLE,
                         metadata: dict[str, str] | None = None) -> None:
        check_writable_key(key)
        params = {"Bucket": self._config.bucket, "Key": key, "Body": data, "ContentType": content_type,
                  "CacheControl": cache_control}
        if metadata:
            params["Metadata"] = metadata
        await self._call(lambda: self._s3().put_object(**params))

    def presign_put(self, key: str, *, content_type: str, content_length: int,
                    expires_in: int = PRESIGN_SECONDS) -> str:
        """A PUT URL valid `expires_in` seconds whose signature covers host, content-type and content-length:
        the client must send exactly that type and size. Computed locally (no network); never log it."""
        check_writable_key(key)
        try:
            return self._s3().generate_presigned_url(
                "put_object", ExpiresIn=expires_in, HttpMethod="PUT",
                Params={"Bucket": self._config.bucket, "Key": key, "ContentType": content_type,
                        "ContentLength": content_length})
        except Exception as exc:  # noqa: BLE001
            raise StorageError(_reason(exc)) from None

    async def head_object(self, key: str) -> ObjectHead | None:
        check_soundboard_key(key)

        def head():
            try:
                r = self._s3().head_object(Bucket=self._config.bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                if _not_found(exc):
                    return None
                raise
            return ObjectHead(int(r.get("ContentLength", 0)), str(r.get("ContentType", "")))
        return await self._call(head)

    async def head_upload(self, key: str) -> ObjectHead | None:
        """Size, type and user metadata of a donor upload; None when it does not exist."""
        check_upload_key(key)

        def head():
            try:
                r = self._s3().head_object(Bucket=self._config.bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                if _not_found(exc):
                    return None
                raise
            meta = r.get("Metadata") or {}
            return ObjectHead(int(r.get("ContentLength", 0)), str(r.get("ContentType", "")),
                              {str(k).lower(): str(v) for k, v in meta.items()})
        return await self._call(head)

    async def get_object(self, key: str, *, max_bytes: int) -> bytes:
        """The object's bytes; StorageError("too_large") if it is bigger than max_bytes (never read past it)."""
        check_soundboard_key(key)

        def get():
            r = self._s3().get_object(Bucket=self._config.bucket, Key=key, Range=f"bytes=0-{max_bytes}")
            body = r["Body"]
            try:
                return body.read(max_bytes + 1)
            finally:
                body.close()
        data = await self._call(get)
        if len(data) > max_bytes:
            raise StorageError("too_large")
        return data

    async def delete_object(self, key: str) -> None:
        check_soundboard_key(key)
        await self._call(lambda: self._s3().delete_object(Bucket=self._config.bucket, Key=key))

    async def list_keys(self, prefix: str) -> list[str]:
        check_listable_prefix(prefix)

        def list_all():
            keys, token = [], None
            while len(keys) < MAX_LISTED_KEYS:
                kwargs = {"Bucket": self._config.bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                r = self._s3().list_objects_v2(**kwargs)
                keys += [o["Key"] for o in r.get("Contents", []) if o.get("Key")]
                if not r.get("IsTruncated"):
                    break
                token = r.get("NextContinuationToken")
            return keys[:MAX_LISTED_KEYS]
        return await self._call(list_all)


def build_object_store(settings: Settings) -> R2Store | None:
    config = R2Config.from_settings(settings)
    return R2Store(config) if config else None
