from __future__ import annotations

import re

def normalize_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split()).strip()


def normalize_multiline_input(text: str | None) -> str:
    if text is None:
        return ""
    value = str(text)
    value = value.replace("\r\n", "\n")
    value = value.replace("\r", "\n")
    value = value.replace("\t", " ")
    value = value.replace("\\n", "\n")
    value = value.replace("\\t", " ")
    return value


def safe_excerpt(text: str | None, limit: int = 120) -> str:
    clean = normalize_whitespace(text)
    if not clean:
        return ""
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def chunk_text(text: str | None, chunk_size: int = 3500):
    clean = text or ""
    for i in range(0, len(clean), chunk_size):
        yield clean[i : i + chunk_size]


def remove_noise(text: str | None) -> str:
    clean = normalize_whitespace(text)
    clean = re.sub(r"[\u200b\u200c\u200d]", "", clean)
    return clean.strip()
