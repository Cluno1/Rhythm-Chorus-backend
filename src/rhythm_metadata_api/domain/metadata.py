from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SourceType(StrEnum):
    USER_OVERRIDE = "user_override"
    LOCAL_SIDECAR = "local_sidecar"
    EMBEDDED_FILE = "embedded_file"
    RHYTHM_CLOUD = "rhythm_cloud"
    STREAMING_SERVER = "streaming_server"
    THIRD_PARTY_API = "third_party_api"


class ResolutionPolicy(StrEnum):
    LOCAL_FIRST = "local_first"
    CLOUD_FIRST = "cloud_first"
    ASK_ON_DIFFERENCE = "ask_on_difference"


@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    value: Any
    source: SourceType
    content_hash: str
    revision: int = 0
    base_revision: int = 0
    user_edited: bool = False
    pinned: bool = False
    source_ref: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MetadataCandidate:
        return cls(
            value=value.get("value"),
            source=SourceType(value["source"]),
            content_hash=str(value["content_hash"]),
            revision=int(value.get("revision", 0)),
            base_revision=int(value.get("base_revision", 0)),
            user_edited=bool(value.get("user_edited", False)),
            pinned=bool(value.get("pinned", False)),
            source_ref=value.get("source_ref"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source.value,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "base_revision": self.base_revision,
            "user_edited": self.user_edited,
            "pinned": self.pinned,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    selected: MetadataCandidate | None
    conflict: bool
    candidates: tuple[MetadataCandidate, ...]


LOCAL_PRIORITY = {
    SourceType.USER_OVERRIDE: 0,
    SourceType.LOCAL_SIDECAR: 1,
    SourceType.EMBEDDED_FILE: 2,
    SourceType.RHYTHM_CLOUD: 3,
    SourceType.STREAMING_SERVER: 4,
    SourceType.THIRD_PARTY_API: 5,
}

CLOUD_PRIORITY = {
    SourceType.USER_OVERRIDE: 0,
    SourceType.RHYTHM_CLOUD: 1,
    SourceType.STREAMING_SERVER: 2,
    SourceType.LOCAL_SIDECAR: 3,
    SourceType.EMBEDDED_FILE: 4,
    SourceType.THIRD_PARTY_API: 5,
}


def resolve_field(
    candidates: Iterable[MetadataCandidate],
    policy: ResolutionPolicy = ResolutionPolicy.LOCAL_FIRST,
) -> Resolution:
    available = tuple(candidates)
    if not available:
        return Resolution(selected=None, conflict=False, candidates=())

    pinned = tuple(candidate for candidate in available if candidate.pinned)
    if len({candidate.content_hash for candidate in pinned}) > 1:
        return Resolution(selected=None, conflict=True, candidates=pinned)
    if pinned:
        return Resolution(
            selected=max(pinned, key=lambda item: item.revision),
            conflict=False,
            candidates=available,
        )

    edited = tuple(candidate for candidate in available if candidate.user_edited)
    if len({candidate.content_hash for candidate in edited}) > 1:
        return Resolution(selected=None, conflict=True, candidates=edited)

    if (
        policy == ResolutionPolicy.ASK_ON_DIFFERENCE
        and len({candidate.content_hash for candidate in available}) > 1
    ):
        return Resolution(selected=None, conflict=True, candidates=available)

    priority = CLOUD_PRIORITY if policy == ResolutionPolicy.CLOUD_FIRST else LOCAL_PRIORITY
    selected = min(available, key=lambda item: (priority[item.source], -item.revision))
    return Resolution(selected=selected, conflict=False, candidates=available)
