"""In-memory ObjectStore for tests (same key rules as R2Store; nothing leaves the process)."""

from dataclasses import dataclass

from app.storage.r2 import IMMUTABLE, StorageError, check_writable_key


@dataclass(frozen=True)
class StoredObject:
    data: bytes
    content_type: str
    cache_control: str
    metadata: dict[str, str]


class InMemoryObjectStore:
    def __init__(self, public_base: str = "https://media.example.test") -> None:
        self.public_base = public_base.rstrip("/")
        self.objects: dict[str, StoredObject] = {}
        self.fail_with: str | None = None  # set to a reason code to simulate an R2 failure

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

    async def put_object(self, key: str, data: bytes, *, content_type: str, cache_control: str = IMMUTABLE,
                         metadata: dict[str, str] | None = None) -> None:
        check_writable_key(key)
        if self.fail_with is not None:
            raise StorageError(self.fail_with)
        self.objects[key] = StoredObject(bytes(data), content_type, cache_control, dict(metadata or {}))
