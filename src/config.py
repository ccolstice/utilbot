from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: set[int]
    owner_user_ids: set[int]
    database_path: Path
    host: str
    port: int
    enable_file_log: bool
    render_free_mode: bool
    backup_interval_hours: int
    archive_api_token: str
    chatgpt_ocr_url: str
    chatgpt_ocr_token: str


def get_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set")

    db_path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
    if os.getenv("DATABASE_BACKEND", "sqlite").strip().lower() == "sqlite":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    render_free_mode = _env_bool("RENDER_FREE_MODE", "0")
    allowed_user_ids = _parse_ids(os.getenv("ALLOWED_USER_IDS"))
    owner_user_ids = _parse_ids(os.getenv("OWNER_USER_IDS"))
    chatgpt_ocr_url = os.getenv("CHATGPT_OCR_URL", "").strip().rstrip("/")
    chatgpt_ocr_token = os.getenv("CHATGPT_OCR_TOKEN", "").strip()
    if bool(chatgpt_ocr_url) != bool(chatgpt_ocr_token):
        raise RuntimeError("Set both CHATGPT_OCR_URL and CHATGPT_OCR_TOKEN to enable OCR.")
    if chatgpt_ocr_url and not chatgpt_ocr_url.startswith("https://"):
        raise RuntimeError("CHATGPT_OCR_URL must use HTTPS.")

    if render_free_mode and not allowed_user_ids and not owner_user_ids:
        raise RuntimeError(
            "RENDER_FREE_MODE=1 requires ALLOWED_USER_IDS or OWNER_USER_IDS. "
            "Refusing to start an open bot in production."
        )

    return Settings(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        owner_user_ids=owner_user_ids,
        database_path=db_path,
        host=os.getenv("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8080),
        enable_file_log=_env_bool("ENABLE_FILE_LOG", "0"),
        render_free_mode=render_free_mode,
        backup_interval_hours=max(24, min(_env_int("BACKUP_INTERVAL_HOURS", 24), 48)),
        archive_api_token=os.getenv("ARCHIVE_API_TOKEN", "").strip(),
        chatgpt_ocr_url=chatgpt_ocr_url,
        chatgpt_ocr_token=chatgpt_ocr_token,
    )
