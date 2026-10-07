"""In-memory ObjectStore for tests (same key rules as R2Store; nothing leaves the process).

presign_put records what the URL allows; client_put() plays the browser's PUT and, like R2 with a signed
content-type/content-length, refuses anything else or an expired URL.
"""

import time
from dataclasses import dataclass

from app.storage.r2 import (
    IMMUTABLE,
    LISTABLE_PREFIXES,
    PRESIGN_SECONDS,
    ObjectHead,
    StorageError,
    check_listable_prefix,
    check_generated_key,
    check_soundboard_key,
    check_upload_key,
    check_voice_key,
    check_writable_key,
    public_url_for,
)


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str
    cache_control: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class Presigned:
    content_type: str
    content_length: int
    expires_at: float


class InMemoryObjectStore:
    def __init__(self, public_base: str = "https://media.example.test", clock=time.time) -> None:
        self.public_base = public_base.rstrip("/")
        self.objects: dict[str, StoredObject] = {}
        self.presigned: dict[str, Presigned] = {}
        self.deleted: list[str] = []
        self.fail_with: str | None = None  # set to a reason code to simulate an R2 failure (every operation)
        self.fail_ops: set[str] = set()     # or only for these operations ("put", "head", "get", "delete", "list")
        self.clock = clock

    def _maybe_fail(self, op: str) -> None:
        if self.fail_with is not None and (not self.fail_ops or op in self.fail_ops):
            raise StorageError(self.fail_with)

    def public_url(self, key: str) -> str:
        return public_url_for(self.public_base, key)

    async def put_object(self, key: str, data: bytes, *, content_type: str, cache_control: str = IMMUTABLE,
                         metadata: dict[str, str] | None = None) -> None:
        check_writable_key(key)
        self._maybe_fail("put")
        self.objects[key] = StoredObject(bytes(data), content_type, cache_control, dict(metadata or {}))

    def presign_put(self, key: str, *, content_type: str, content_length: int,
                    expires_in: int = PRESIGN_SECONDS) -> str:
        check_writable_key(key)
        self._maybe_fail("presign")
        self.presigned[key] = Presigned(content_type, content_length, self.clock() + expires_in)
        return (f"https://r2.example.test/bucket/{key}?X-Amz-Expires={expires_in}"
                f"&X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost&X-Amz-Signature=test")

    def client_put(self, key: str, data: bytes, content_type: str) -> int:
        """The browser's PUT to a presigned URL: HTTP status as R2 would answer."""
        p = self.presigned.get(key)
        if p is None or self.clock() > p.expires_at:
            return 403
        if content_type != p.content_type or len(data) != p.content_length:
            return 403  # signature mismatch
        self.objects[key] = StoredObject(bytes(data), content_type, "", {})
        return 200

    async def head_object(self, key: str) -> ObjectHead | None:
        check_soundboard_key(key)
        self._maybe_fail("head")
        obj = self.objects.get(key)
        return ObjectHead(len(obj.data), obj.content_type) if obj else None

    async def head_upload(self, key: str) -> ObjectHead | None:
        check_upload_key(key)
        self._maybe_fail("head")
        obj = self.objects.get(key)
        return ObjectHead(len(obj.data), obj.content_type, dict(obj.metadata)) if obj else None

    async def head_generated(self, key: str) -> ObjectHead | None:
        check_generated_key(key)
        self._maybe_fail("head")
        obj = self.objects.get(key)
        return ObjectHead(len(obj.data), obj.content_type) if obj else None

    async def get_voice(self, key: str, *, max_bytes: int) -> bytes:
        check_voice_key(key)
        return await self._get(key, max_bytes)

    async def get_object(self, key: str, *, max_bytes: int) -> bytes:
        check_soundboard_key(key)
        return await self._get(key, max_bytes)

    async def _get(self, key: str, max_bytes: int) -> bytes:
        self._maybe_fail("get")
        obj = self.objects.get(key)
        if obj is None:
            raise StorageError("r2_NoSuchKey")
        if len(obj.data) > max_bytes:
            raise StorageError("too_large")
        return obj.data

    async def delete_object(self, key: str) -> None:
        check_soundboard_key(key)
        self._maybe_fail("delete")
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def list_keys(self, prefix: str) -> list[str]:
        check_listable_prefix(prefix)
        self._maybe_fail("list")
        return sorted(k for k in self.objects if k.startswith(prefix))

    def seed_library(self, key: str, data: bytes = b"x", content_type: str = "audio/mpeg") -> None:
        """Test helper: platform library objects (hypersounds/, hypersound-thumbs/) as an operator uploads them."""
        assert key.startswith(LISTABLE_PREFIXES)
        self.objects[key] = StoredObject(data, content_type, "", {})
