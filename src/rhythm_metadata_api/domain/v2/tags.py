"""Canonical Work tags and their stable lookup keys."""

from __future__ import annotations

import unicodedata


def tag_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def normalize_work_tags(values: list[str]) -> list[str]:
    if len(values) > 50:
        raise ValueError("a work may have at most 50 tags")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("tags must be strings")
        label = " ".join(unicodedata.normalize("NFC", value).split())
        if not 1 <= len(label) <= 40:
            raise ValueError("each tag must contain 1 to 40 characters")
        key = label.casefold()
        if key not in seen:
            result.append(label)
            seen.add(key)
    return result
