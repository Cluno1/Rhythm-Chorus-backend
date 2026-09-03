from __future__ import annotations

import hashlib
import os
import struct
import zipfile
from collections.abc import AsyncIterable
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from rhythm_metadata_api.infrastructure.storage.base import (
    EmptyUploadError,
    UploadTooLargeError,
    UploadValidationError,
)


class LocalAssetStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".uploads").mkdir(parents=True, exist_ok=True)

    async def write_upload(
        self,
        upload_id: str,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
    ) -> tuple[str, str, int]:
        temporary_key = f".uploads/{upload_id}.part"
        destination = self.resolve(temporary_key)
        staging = destination.with_suffix(f".tmp-{os.getpid()}-{os.urandom(4).hex()}")
        digest = hashlib.sha256()
        size = 0
        try:
            with staging.open("xb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLargeError("upload exceeds the configured size limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size == 0:
                raise EmptyUploadError("upload is empty")
            os.replace(staging, destination)
            return temporary_key, digest.hexdigest(), size
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    def inspect_upload(
        self,
        temporary_key: str,
        declared_media_type: str,
        original_filename: str | None,
    ) -> str:
        path = self.resolve(temporary_key)
        if not path.is_file():
            raise UploadValidationError("uploaded bytes are unavailable")
        media_type = declared_media_type.split(";", 1)[0].strip().lower()
        extension = Path(original_filename or "").suffix.lower()

        if media_type in MUSICXML_TYPES or extension in {".musicxml", ".xml"}:
            self._validate_musicxml(path)
            return "application/vnd.recordare.musicxml+xml"
        if media_type in MXL_TYPES or extension == ".mxl":
            self._validate_mxl(path)
            return "application/vnd.recordare.musicxml"
        if media_type in MIDI_TYPES or extension in {".mid", ".midi"}:
            self._validate_midi(path)
            return "audio/midi"
        if media_type.startswith("image/"):
            return self._validate_image(path)
        if media_type.startswith("audio/") or extension in AUDIO_EXTENSIONS:
            return self._validate_audio(path, media_type, extension)
        if media_type.startswith("text/") or extension in {".lrc", ".txt", ".srt"}:
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise UploadValidationError("text asset is not valid UTF-8") from error
            return media_type or "text/plain"
        raise UploadValidationError("unsupported asset media type")

    def promote(self, temporary_key: str, sha256: str) -> str:
        source = self.resolve(temporary_key)
        storage_key = f"sha256/{sha256[:2]}/{sha256}"
        destination = self.resolve(storage_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            source.unlink(missing_ok=True)
        else:
            os.replace(source, destination)
        return storage_key

    def discard(self, temporary_key: str | None) -> None:
        if temporary_key:
            self.resolve(temporary_key).unlink(missing_ok=True)

    def resolve(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("invalid storage key")
        return candidate

    @staticmethod
    def _validate_musicxml(path: Path) -> None:
        LocalAssetStorage._reject_xml_declarations(path)
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError as error:
            raise UploadValidationError("MusicXML is not valid XML") from error
        tag = root.tag.rsplit("}", 1)[-1]
        if tag not in {"score-partwise", "score-timewise"}:
            raise UploadValidationError("XML root is not a MusicXML score")

    @staticmethod
    def _reject_xml_declarations(path: Path) -> None:
        overlap = b""
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                inspected = (overlap + chunk).upper()
                if b"<!DOCTYPE" in inspected or b"<!ENTITY" in inspected:
                    raise UploadValidationError("MusicXML external declarations are not allowed")
                overlap = inspected[-16:]

    @classmethod
    def _validate_mxl(cls, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if len(members) > 256:
                    raise UploadValidationError("MXL contains too many files")
                if sum(item.file_size for item in members) > 100 * 1024 * 1024:
                    raise UploadValidationError("MXL expands beyond the safety limit")
                for item in members:
                    pure = PurePosixPath(item.filename)
                    if pure.is_absolute() or ".." in pure.parts:
                        raise UploadValidationError("MXL contains an unsafe path")
                container = archive.read("META-INF/container.xml")
                if b"<!DOCTYPE" in container.upper() or b"<!ENTITY" in container.upper():
                    raise UploadValidationError("MXL container declarations are not allowed")
                container_root = ElementTree.fromstring(container)
                rootfile = next(
                    (
                        element.attrib.get("full-path")
                        for element in container_root.iter()
                        if element.tag.rsplit("}", 1)[-1] == "rootfile"
                    ),
                    None,
                )
                if not rootfile:
                    raise UploadValidationError("MXL has no root MusicXML file")
                score = archive.read(rootfile)
                if b"<!DOCTYPE" in score[:65536].upper() or b"<!ENTITY" in score[:65536].upper():
                    raise UploadValidationError("MusicXML external declarations are not allowed")
                tag = ElementTree.fromstring(score).tag.rsplit("}", 1)[-1]
                if tag not in {"score-partwise", "score-timewise"}:
                    raise UploadValidationError("MXL root file is not a MusicXML score")
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as error:
            raise UploadValidationError("MXL archive is invalid") from error

    @staticmethod
    def _validate_midi(path: Path) -> None:
        with path.open("rb") as source:
            header = source.read(14)
        if len(header) < 14 or header[:4] != b"MThd" or struct.unpack(">I", header[4:8])[0] != 6:
            raise UploadValidationError("MIDI header is invalid")

    @staticmethod
    def _validate_image(path: Path) -> str:
        with path.open("rb") as source:
            header = source.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            width, height = struct.unpack(">II", header[16:24])
            media_type = "image/png"
        elif header.startswith(b"\xff\xd8\xff"):
            # Full JPEG dimension parsing belongs to the inspection worker.
            width, height = 1, 1
            media_type = "image/jpeg"
        elif header[:6] in {b"GIF87a", b"GIF89a"}:
            width, height = struct.unpack("<HH", header[6:10])
            media_type = "image/gif"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            width, height = 1, 1
            media_type = "image/webp"
        else:
            raise UploadValidationError("image signature is not recognized")
        if width <= 0 or height <= 0 or width * height > 100_000_000:
            raise UploadValidationError("image dimensions exceed the safety limit")
        return media_type

    @staticmethod
    def _validate_audio(path: Path, media_type: str, extension: str) -> str:
        with path.open("rb") as source:
            header = source.read(32)
        detected = None
        if header.startswith(b"fLaC"):
            detected = "audio/flac"
        elif header.startswith(b"OggS"):
            detected = "audio/ogg"
        elif header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            detected = "audio/wav"
        elif header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        ):
            detected = "audio/mpeg"
        elif len(header) >= 12 and header[4:8] == b"ftyp":
            detected = "audio/mp4"
        if detected is None:
            raise UploadValidationError("audio container signature is not recognized")
        return (
            detected
            or media_type
            or MIME_BY_AUDIO_EXTENSION.get(extension, "application/octet-stream")
        )


MUSICXML_TYPES = {
    "application/vnd.recordare.musicxml+xml",
    "application/xml",
    "text/xml",
}
MXL_TYPES = {"application/vnd.recordare.musicxml"}
MIDI_TYPES = {"audio/midi", "audio/x-midi", "application/x-midi"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".wav", ".flac", ".ogg", ".opus"}
MIME_BY_AUDIO_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
}
