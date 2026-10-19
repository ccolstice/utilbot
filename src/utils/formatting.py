from __future__ import annotations

from datetime import datetime, timezone


def human_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def truncate(text: str, limit: int = 120) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def join_nonempty(*parts: str, sep: str = "\n") -> str:
    return sep.join(p for p in parts if p and p.strip())
