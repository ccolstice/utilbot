from __future__ import annotations

import re
from datetime import datetime, timedelta


_DURATION_RE = re.compile(r"^(?:in\s+)?(?P<num>\d+)\s*(?P<unit>[smhd])$", re.IGNORECASE)


def parse_repeat_minutes(text: str | None) -> int | None:
    text = (text or "").strip().lower()
    if text in {"none", "без повтора", "не повторять", "no", ""}:
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    num = int(match.group("num"))
    unit = match.group("unit")
    mult = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}[unit]
    return max(1, int(num * mult))


def parse_datetime_input(text: str, base: datetime | None = None) -> datetime | None:
    base = base or datetime.now().replace(second=0, microsecond=0)
    raw = (text or "").strip().lower()

    if not raw:
        return None

    if raw.startswith("in "):
        raw = raw[3:].strip()

    m = _DURATION_RE.match(raw)
    if m:
        num = int(m.group("num"))
        unit = m.group("unit")
        delta = {
            "s": timedelta(seconds=num),
            "m": timedelta(minutes=num),
            "h": timedelta(hours=num),
            "d": timedelta(days=num),
        }[unit]
        return base + delta

    if raw.startswith("tomorrow"):
        time_part = raw.replace("tomorrow", "", 1).strip()
        if not time_part:
            return base + timedelta(days=1)
        if re.fullmatch(r"\d{1,2}:\d{2}", time_part):
            hh, mm = map(int, time_part.split(":"))
            dt = (base + timedelta(days=1)).replace(hour=hh, minute=mm)
            return dt

    if raw.startswith("today"):
        time_part = raw.replace("today", "", 1).strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", time_part):
            hh, mm = map(int, time_part.split(":"))
            dt = base.replace(hour=hh, minute=mm)
            if dt <= base:
                dt = dt + timedelta(days=1)
            return dt

    for fmt in ("%Y-%m-%d %H:%M", "%d.%m %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(text.strip(), fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=base.year)
                if dt < base:
                    dt = dt.replace(year=base.year + 1)
            return dt
        except ValueError:
            pass

    return None
