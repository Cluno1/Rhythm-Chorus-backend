"""issue 107: helpers for lyrics / number search over Works.

Matching is deliberately simple: compare the user's text against every stored version
(title, default lyrics, each translation) after removing whitespace and punctuation and
case-folding Latin letters. Simplified/Traditional conversion is only a fallback, applied to
the query when the direct match finds nothing, because most Works store both scripts.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any

try:  # pure-Python OpenCC; optional so the API still works without it
    from opencc import OpenCC

    _T2S = OpenCC("t2s").convert
    _S2T = OpenCC("s2t").convert
except Exception:  # noqa: BLE001
    _T2S = _S2T = None

NUMBER_KEY_ORDER = ("ihope_num", "heavenSong_num")
SNIPPET_RADIUS = 15
SNIPPET_HEAD = 30


def normalize_with_index(text: str) -> tuple[str, list[int]]:
    """NFKC, drop whitespace/punctuation/symbols, casefold. Returns (normalized, index map into text)."""
    chars: list[str] = []
    positions: list[int] = []
    for index, raw in enumerate(text or ""):
        for char in unicodedata.normalize("NFKC", raw):
            category = unicodedata.category(char)
            if category[0] in ("L", "N"):
                for folded in char.casefold():
                    chars.append(folded)
                    positions.append(index)
    return "".join(chars), positions


def normalize(text: str) -> str:
    return normalize_with_index(text)[0]


def find_match(haystack: str | None, needle_norm: str) -> int | None:
    """Return the original-text index where the normalized needle starts, or None."""
    if not haystack or not needle_norm:
        return None
    norm, positions = normalize_with_index(haystack)
    found = norm.find(needle_norm)
    return positions[found] if found >= 0 else None


def snippet_around(text: str, start: int) -> str:
    left = max(0, start - SNIPPET_RADIUS)
    right = min(len(text), start + SNIPPET_RADIUS * 2)
    piece = " ".join(text[left:right].split())
    return ("…" if left > 0 else "") + piece + ("…" if right < len(text) else "")


def head_snippet(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    flat = " ".join(text.split())
    return flat[:SNIPPET_HEAD] + ("…" if len(flat) > SNIPPET_HEAD else "")


def opencc_variants(query: str) -> list[tuple[str, str]]:
    """Fallback queries: (label, converted) for t2s then s2t, skipping no-op conversions."""
    variants: list[tuple[str, str]] = []
    for label, convert in (("opencc_t2s", _T2S), ("opencc_s2t", _S2T)):
        if convert is None:
            continue
        converted = convert(query)
        if converted != query and normalize(converted):
            variants.append((label, converted))
    return variants


def number_items(raw: Any) -> list[dict[str, Any]]:
    """issue 105 storage [{"ihope_num": 256}, ...] -> [{"key": ..., "value": ...}] in a stable order."""
    items: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            for key, value in entry.items():
                if isinstance(key, str) and isinstance(value, (int, str)) and not isinstance(value, bool):
                    items.append({"key": key, "value": value})
    order = {key: index for index, key in enumerate(NUMBER_KEY_ORDER)}
    return sorted(items, key=lambda item: (order.get(item["key"], len(order)), item["key"], str(item["value"])))


def parse_number_query(query: str) -> int | None:
    compact = "".join(query.split())
    return int(compact) if compact.isdigit() else None


def lyric_versions(
    lyrics: str | None, lyrics_language: str | None, translations: Iterable[dict[str, Any]] | None
) -> list[tuple[str | None, str]]:
    versions: list[tuple[str | None, str]] = []
    if lyrics and lyrics.strip():
        versions.append((lyrics_language, lyrics))
    for item in translations or []:
        text = item.get("lyrics") if isinstance(item, dict) else None
        if isinstance(text, str) and text.strip():
            versions.append((item.get("language"), text))
    return versions
