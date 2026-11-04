"""issue 154: administrator announcements stored as a strict rich text document.

Invariant: every stored body passes ``normalize_body``, which rebuilds the document from an allow-list.
Why: clients render these nodes natively, so an unknown node must never come from this API version, and
no value can carry markup, scripts, colors or third-party media.
Silent failure if broken: an unchecked image or link host would make every client contact a third party,
and style attributes would let one announcement break the app's look.
Rejected: storing sanitized HTML (needs a WebView and a sanitizer) and passing unknown attributes through.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from rhythm_metadata_api.domain.v2.schemas import ApiModel

SCHEMA_VERSION = 1
IMAGE_PATH_PREFIX = "/v2/announcements/images/"
IMAGE_NAME = re.compile(r"^[a-f0-9]{64}\.(?:jpg|png|webp|gif)$")
MAX_BODY_BYTES = 256 * 1024
_MAX_DEPTH = 8
_MAX_DIMENSION = 20_000
_EXCERPT_LENGTH = 120

Importance = Literal["normal", "important"]
Status = Literal["draft", "published", "archived"]


def _nodes(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} content must be a list")  # noqa: TRY004 - pydantic only maps ValueError to 422
    return value


def _link_mark(mark: dict) -> dict:
    href = (mark.get("attrs") or {}).get("href")
    if not isinstance(href, str) or len(href) > 2000 or any(ord(char) < 33 for char in href):
        raise ValueError("link href is invalid")
    parts = urlsplit(href)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("links must use https")
    return {"type": "link", "attrs": {"href": href}}


def _inline(node: Any) -> dict:
    if not isinstance(node, dict):
        raise ValueError("inline node must be an object")  # noqa: TRY004 - pydantic only maps ValueError to 422
    kind = node.get("type")
    if kind == "hardBreak":
        return {"type": "hardBreak"}
    if kind != "text":
        raise ValueError(f"inline node is not allowed: {kind}")
    text = node.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("text node must not be empty")
    marks = []
    for mark in _nodes(node.get("marks"), "marks"):
        mark_type = mark.get("type") if isinstance(mark, dict) else None
        if mark_type in {"bold", "italic"}:
            marks.append({"type": mark_type})
        elif mark_type == "link":
            marks.append(_link_mark(mark))
        else:
            raise ValueError(f"mark is not allowed: {mark_type}")
    return {"type": "text", "text": text, **({"marks": marks} if marks else {})}


def _inline_container(result: dict, node: dict) -> dict:
    content = [_inline(child) for child in _nodes(node.get("content"), str(node.get("type")))]
    return {**result, **({"content": content} if content else {})}


def _blocks(node: dict, depth: int, *, allowed: frozenset[str] | None = None) -> list[dict]:
    children = _nodes(node.get("content"), str(node.get("type")))
    if not children:
        raise ValueError(f"{node.get('type')} must not be empty")
    return [_block(child, depth + 1, allowed=allowed) for child in children]


def _image_attrs(node: dict) -> dict:
    attrs = node.get("attrs") or {}
    src = attrs.get("src")
    if not isinstance(src, str) or not src.startswith(IMAGE_PATH_PREFIX):
        raise ValueError("images must be uploaded announcement images")
    if not IMAGE_NAME.fullmatch(src.removeprefix(IMAGE_PATH_PREFIX)):
        raise ValueError("image path is invalid")
    result: dict[str, Any] = {"src": src}
    alt = attrs.get("alt")
    if alt:
        if not isinstance(alt, str) or len(alt) > 200:
            raise ValueError("image alt is invalid")
        result["alt"] = alt
    for name in ("width", "height"):
        value = attrs.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DIMENSION:
            raise ValueError(f"image {name} is invalid")
        result[name] = value
    return result


def _block(node: Any, depth: int, *, allowed: frozenset[str] | None = None) -> dict:
    if depth > _MAX_DEPTH:
        raise ValueError("document is nested too deeply")
    if not isinstance(node, dict):
        raise ValueError("block node must be an object")  # noqa: TRY004 - pydantic only maps ValueError to 422
    kind = node.get("type")
    if allowed is not None and kind not in allowed:
        raise ValueError(f"block is not allowed here: {kind}")
    if kind == "paragraph":
        return _inline_container({"type": "paragraph"}, node)
    if kind == "heading":
        if (node.get("attrs") or {}).get("level") != 2:
            raise ValueError("only level 2 headings are allowed")
        return _inline_container({"type": "heading", "attrs": {"level": 2}}, node)
    if kind == "bulletList":
        return {"type": "bulletList", "content": _blocks(node, depth, allowed=_LIST_ITEM)}
    if kind == "orderedList":
        start = (node.get("attrs") or {}).get("start", 1)
        if isinstance(start, bool) or not isinstance(start, int) or not 1 <= start <= 100_000:
            raise ValueError("ordered list start is invalid")
        attrs = {"attrs": {"start": start}} if start != 1 else {}
        return {"type": "orderedList", **attrs, "content": _blocks(node, depth, allowed=_LIST_ITEM)}
    if kind == "listItem":
        return {"type": "listItem", "content": _blocks(node, depth, allowed=_ITEM_CHILDREN)}
    if kind == "blockquote":
        return {"type": "blockquote", "content": _blocks(node, depth, allowed=_ITEM_CHILDREN)}
    if kind == "horizontalRule":
        return {"type": "horizontalRule"}
    if kind == "image":
        return {"type": "image", "attrs": _image_attrs(node)}
    raise ValueError(f"block is not allowed: {kind}")


_LIST_ITEM = frozenset({"listItem"})
_ITEM_CHILDREN = frozenset({"paragraph", "bulletList", "orderedList", "blockquote"})
_TOP_LEVEL = frozenset({"paragraph", "heading", "bulletList", "orderedList", "blockquote", "horizontalRule", "image"})


def normalize_body(value: Any) -> dict:
    """Rebuild a document from allowed nodes only; unknown nodes fail, unknown attributes are dropped."""
    if not isinstance(value, dict) or value.get("type") != "doc":
        raise ValueError("body must be a doc node")
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        raise ValueError("body is too large")
    return {
        "type": "doc",
        "content": [_block(child, 1, allowed=_TOP_LEVEL) for child in _nodes(value.get("content"), "doc")],
    }


def _walk(node: dict):
    yield node
    for child in node.get("content", []):
        yield from _walk(child)


def body_image_paths(body: dict) -> list[str]:
    return [node["attrs"]["src"] for node in _walk(body) if node.get("type") == "image"]


def body_has_content(body: dict) -> bool:
    return any(
        node.get("type") == "image" or (node.get("type") == "text" and node["text"].strip())
        for node in _walk(body)
    )


def body_excerpt(body: dict) -> str:
    parts: list[str] = []
    for node in _walk(body):
        if node.get("type") == "text":
            parts.append(node["text"])
    return " ".join(" ".join(parts).split())[:_EXCERPT_LENGTH]


class AnnouncementWrite(ApiModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str | None = Field(default=None, max_length=200)
    importance: Importance = "normal"
    body: dict[str, Any]

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @field_validator("summary")
    @classmethod
    def strip_summary(cls, value: str | None) -> str | None:
        return " ".join(value.split()) or None if value is not None else None

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: dict[str, Any]) -> dict[str, Any]:
        return normalize_body(value)


class AnnouncementPatch(AnnouncementWrite):
    revision: int = Field(ge=1)


class AnnouncementTransition(ApiModel):
    revision: int = Field(ge=1)


class AnnouncementListItem(ApiModel):
    id: str
    title: str
    summary: str
    importance: Importance
    published_at: datetime
    cover_image: str | None
    revision: int


class AnnouncementListResponse(ApiModel):
    items: list[AnnouncementListItem]
    next_cursor: str | None


class AnnouncementDetail(AnnouncementListItem):
    schema_version: int
    body: dict[str, Any]
    updated_at: datetime


class AdminAnnouncementListItem(ApiModel):
    id: str
    title: str
    summary: str | None
    importance: Importance
    status: Status
    published_at: datetime | None
    revision: int
    created_by: str
    created_at: datetime
    updated_at: datetime


class AdminAnnouncementListResponse(ApiModel):
    items: list[AdminAnnouncementListItem]


class AdminAnnouncementDetail(AdminAnnouncementListItem):
    schema_version: int
    body: dict[str, Any]


class AnnouncementImageResponse(ApiModel):
    name: str
    path: str
    media_type: str
    byte_size: int
