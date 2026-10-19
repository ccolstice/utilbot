from __future__ import annotations

from dataclasses import dataclass, asdict


TYPE_ALIASES = {
    "photo": "photo",
    "img": "photo",
    "image": "photo",
    "voice": "voice",
    "audio": "audio",
    "video": "video",
    "video_note": "video_note",
    "gif": "animation",
    "animation": "animation",
    "document": "document",
    "doc": "document",
    "sticker": "sticker",
    "text": "text",
    "link": "link",
    "links": "link",
    "ocr": "ocr",
    "transcript": "transcript",
}

DEEP_HINTS = {"deep", "ai", "media", "ocr", "transcript", "fav", "favorite"}


@dataclass(slots=True)
class SearchRequest:
    query: str
    kind: str | None = None
    folder_name: str | None = None
    tags: list[str] | None = None
    domain: str | None = None
    sender: str | None = None
    before: str | None = None
    after: str | None = None
    today: bool = False
    untagged: bool = False
    deep: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_search_request(raw: str | None, *, deep_default: bool = False) -> SearchRequest:
    raw = (raw or "").strip()
    tokens = raw.split()

    kind = None
    folder_name = None
    tags: list[str] = []
    domain = None
    sender = None
    before = None
    after = None
    today = False
    untagged = False
    deep = deep_default
    query_parts: list[str] = []

    for token in tokens:
        low = token.lower().strip()

        if low in DEEP_HINTS:
            if low in {"deep", "ai", "media"}:
                deep = True
            elif low in {"ocr", "transcript"}:
                deep = True
                kind = TYPE_ALIASES[low]
            continue

        if low == "today":
            today = True
            continue

        if low == "untagged":
            untagged = True
            continue

        if low.startswith("type:") or low.startswith("kind:"):
            value = low.split(":", 1)[1].strip()
            kind = TYPE_ALIASES.get(value, value if value else None)
            if kind in {"ocr", "transcript"}:
                deep = True
            continue

        if low.startswith("tag:"):
            value = token.split(":", 1)[1].strip().lstrip("#")
            if value:
                tags.append(value)
            continue

        if low.startswith("#") and len(low) > 1:
            tags.append(token[1:].strip())
            continue

        if low.startswith("folder:"):
            folder_name = token.split(":", 1)[1].strip()
            continue

        if low.startswith("domain:"):
            value = token.split(":", 1)[1].strip().lower()
            if value.startswith("www."):
                value = value[4:]
            domain = value or None
            kind = kind or "link"
            continue

        if low.startswith("from:"):
            sender = token.split(":", 1)[1].strip().lstrip("@")
            continue

        if low.startswith("before:"):
            before = token.split(":", 1)[1].strip()
            continue

        if low.startswith("after:"):
            after = token.split(":", 1)[1].strip()
            continue

        query_parts.append(token)

    return SearchRequest(
        query=" ".join(query_parts).strip(),
        kind=kind,
        folder_name=folder_name,
        tags=tags or None,
        domain=domain,
        sender=sender,
        before=before,
        after=after,
        today=today,
        untagged=untagged,
        deep=deep,
    )
