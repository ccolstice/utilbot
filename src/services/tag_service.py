from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

STOPWORDS = {
    "and", "the", "for", "with", "from", "this", "that", "you", "your",
    "что", "это", "как", "или", "для", "при", "под", "над", "на", "не", "ли",
    "же", "то", "по", "из", "без", "про", "via", "new", "old",
}

KEYWORD_HINTS = {
    "error": ["error", "ошибка", "traceback", "exception", "fail", "failed"],
    "code": ["python", "aiogram", "sqlite", "api", "code", "код", "скрипт"],
    "voice": ["voice", "audio", "transcript", "speech", "голос", "аудио", "транскрипт"],
    "image": ["photo", "image", "скрин", "screenshot", "picture", "картинка"],
    "work": ["work", "job", "задача", "task", "рабочее", "project", "проект"],
    "idea": ["idea", "идея", "feature", "фича", "todo", "think"],
    "readlater": ["read later", "readlater", "посмотреть позже", "почитать позже", "later"],
}

EXTENSION_TAGS = {
    ".pdf": "pdf",
    ".doc": "document",
    ".docx": "document",
    ".xls": "spreadsheet",
    ".xlsx": "spreadsheet",
    ".csv": "csv",
    ".zip": "archive",
    ".rar": "archive",
    ".7z": "archive",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".mp3": "audio",
    ".ogg": "audio",
    ".wav": "audio",
    ".mp4": "video",
    ".mov": "video",
}

def _normalize_tag(token: str) -> str | None:
    token = token.strip().lower()
    token = re.sub(r"[^\w\-]+", "", token, flags=re.UNICODE)
    token = token.strip("_-")
    if len(token) < 3:
        return None
    if token in STOPWORDS:
        return None
    return token

def extract_keywords(text: str | None, limit: int = 6) -> list[str]:
    if not text:
        return []
    text = str(text).lower()
    tokens = re.findall(r"[\wа-яёА-ЯЁ]{3,}", text, flags=re.UNICODE)
    counts = Counter()
    for token in tokens:
        norm = _normalize_tag(token)
        if not norm or norm.isdigit():
            continue
        counts[norm] += 1

    out: list[str] = []
    for tag, _count in counts.most_common():
        if tag not in out:
            out.append(tag)
        if len(out) >= limit:
            break
    return out

def build_auto_tags(*texts: str | None, limit: int = 6) -> list[str]:
    combined = " ".join(t or "" for t in texts if t)
    keywords = extract_keywords(combined, limit=limit * 2)
    tags: list[str] = []

    lower = combined.lower()
    urls = re.findall(r"https?://[^\s<>()\"']+", combined, flags=re.IGNORECASE)
    for url in urls:
        domain = urlparse(url.rstrip(".,;:!?)>]}" )).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain:
            tags.append("link")
            tags.append(domain.split(":")[0])

    for ext in re.findall(r"\.[a-z0-9]{2,5}\b", lower):
        tag = EXTENSION_TAGS.get(ext)
        if tag:
            tags.append(tag)

    for part in combined.split():
        suffix = Path(part.strip(".,;:!?)>]}" )).suffix.lower()
        tag = EXTENSION_TAGS.get(suffix)
        if tag:
            tags.append(tag)

    if re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\b", combined):
        tags.append("date")

    for canonical, hints in KEYWORD_HINTS.items():
        if any(h in lower for h in hints):
            tags.append(canonical)

    for kw in keywords:
        if kw not in tags:
            tags.append(kw)

    cleaned: list[str] = []
    for tag in tags:
        tag = tag.lower().lstrip("#")
        tag = re.sub(r"[^\w\-]+", "", tag, flags=re.UNICODE)
        if len(tag) < 2:
            continue
        if tag not in cleaned:
            cleaned.append(tag)

    return cleaned[:limit]

def format_tags(tags: list[str] | str | None) -> str | None:
    if not tags:
        return None
    if isinstance(tags, str):
        raw = [part.strip() for part in tags.split(",")]
    else:
        raw = [str(part).strip() for part in tags]
    raw = [part for part in raw if part]
    if not raw:
        return None
    return ", ".join(dict.fromkeys(raw))

def merge_user_tags(existing: str | None, extra: list[str] | None) -> str | None:
    base = []
    if existing:
        base.extend([part.strip().lstrip("#") for part in existing.split(",") if part.strip()])
    if extra:
        for tag in extra:
            tag = tag.strip().lstrip("#")
            if tag and tag not in base:
                base.append(tag)
    if not base:
        return None
    return ", ".join(base)
