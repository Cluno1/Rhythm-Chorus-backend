from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path
from typing import Protocol


class AssetStorage(Protocol):
    async def write_upload(
        self, upload_id: str, chunks: AsyncIterable[bytes], max_bytes: int
    ) -> tuple[str, str, int]: ...

    def inspect_upload(
        self,
        temporary_key: str,
        declared_media_type: str,
        original_filename: str | None,
    ) -> str: ...

    def promote(self, temporary_key: str, sha256: str) -> str: ...

    def discard(self, temporary_key: str | None) -> None: ...

    def resolve(self, storage_key: str) -> Path: ...


class EmptyUploadError(ValueError):
    pass


class UploadTooLargeError(ValueError):
    pass


class UploadValidationError(ValueError):
    pass
