from __future__ import annotations

import re
from collections.abc import Iterable

_LANGUAGE_TAG = re.compile(
    r"^(?:[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*|x(?:-[A-Za-z0-9]{1,8})+)$"
)


def normalize_language_tag(value: str) -> str:
    """Validate and apply conventional casing to a BCP 47-style language tag."""
    normalized = value.strip().replace("_", "-")
    if len(normalized) > 35 or not _LANGUAGE_TAG.fullmatch(normalized):
        raise ValueError("language must be a valid BCP 47-style tag such as en or zh-Hans")
    parts = normalized.split("-")
    canonical = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            canonical.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            canonical.append(part.upper())
        else:
            canonical.append(part.lower())
    return "-".join(canonical)


def normalize_lyrics_bundle(
    lyrics: str | None,
    lyrics_language: str | None,
    lyrics_translations: Iterable[dict[str, str]],
    *,
    fallback_language: str | None = None,
) -> tuple[str | None, str, list[dict[str, str]]]:
    """Normalize one entity's primary lyrics and its other language variants."""
    primary = lyrics.strip() if lyrics is not None else None
    if primary == "":
        raise ValueError("lyrics must be non-empty when supplied; use null to remove them")
    language = normalize_language_tag(lyrics_language or fallback_language or "und")
    translations: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in lyrics_translations:
        item_language = normalize_language_tag(item["language"])
        item_lyrics = item["lyrics"].strip()
        if not item_lyrics:
            raise ValueError("translated lyrics must not be empty")
        folded = item_language.casefold()
        if folded == language.casefold():
            raise ValueError("lyrics_translations must not repeat the default language")
        if folded in seen:
            raise ValueError("lyrics_translations contains a duplicate language")
        seen.add(folded)
        translations.append({"language": item_language, "lyrics": item_lyrics})
    if primary is None and translations:
        raise ValueError("translated lyrics require default-language lyrics")
    return primary, language, translations


def merge_lyrics_sources(
    sources: Iterable[tuple[str | None, str, Iterable[dict[str, str]]]],
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """Resolve rendition → score → work lyrics with per-language fallback.

    ``sources`` must be ordered from highest to lowest priority. A higher source
    replaces only the languages it supplies; lower-source translations remain
    available for languages that are otherwise missing.
    """
    source_list = list(sources)
    resolved: dict[str, tuple[str, str]] = {}
    default_language: str | None = None
    for lyrics, language, translations in source_list:
        if default_language is None and lyrics:
            default_language = language
        entries = ([{"language": language, "lyrics": lyrics}] if lyrics else []) + list(
            translations
        )
        for item in entries:
            item_language = normalize_language_tag(item["language"])
            key = item_language.casefold()
            if key not in resolved and item["lyrics"]:
                resolved[key] = (item_language, item["lyrics"])
    if not resolved:
        return None, None, []
    if default_language is None:
        default_language = next(iter(resolved.values()))[0]
    default_key = default_language.casefold()
    default_entry = resolved.get(default_key)
    if default_entry is None:
        default_entry = next(iter(resolved.values()))
        default_language = default_entry[0]
        default_key = default_language.casefold()
    translations = [
        {"language": language, "lyrics": text}
        for key, (language, text) in sorted(resolved.items())
        if key != default_key
    ]
    return default_entry[1], default_language, translations
