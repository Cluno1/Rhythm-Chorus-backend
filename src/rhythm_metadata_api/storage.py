from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterable
from pathlib import Path


class EmptyObjectError(ValueError):
    pass


class ObjectTooLargeError(ValueError):
    pass


class LocalObjectStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> tuple[str, str]:
        content_hash = hashlib.sha256(content).hexdigest()
        storage_key = f"sha256/{content_hash[:2]}/{content_hash}"
        destination = self.root / storage_key
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(f".tmp-{os.getpid()}")
            temporary.write_bytes(content)
            os.replace(temporary, destination)
        return storage_key, content_hash

    async def put_stream(
        self,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
    ) -> tuple[str, str, int]:
        upload_root = self.root / ".uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        temporary = upload_root / f"upload-{os.getpid()}-{os.urandom(8).hex()}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as destination:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise ObjectTooLargeError("artifact too large")
                    digest.update(chunk)
                    destination.write(chunk)
            if size == 0:
                raise EmptyObjectError("empty artifact")

            content_hash = digest.hexdigest()
            storage_key = f"sha256/{content_hash[:2]}/{content_hash}"
            destination = self.root / storage_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                os.replace(temporary, destination)
            return storage_key, content_hash, size
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("invalid storage key")
        return candidate
