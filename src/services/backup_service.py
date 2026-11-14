from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from aiogram import Bot
from aiogram.types import FSInputFile

from src.db.database import DATABASE_FILE, DATABASE_BACKEND, create_sqlite_snapshot, get_connection

logger = logging.getLogger(__name__)


class BackupService:
    BACKUP_DIR = Path("data/backups")
    KEEP_LAST = 7

    @classmethod
    def create_snapshot(cls, *, prefix: str = "bot-backup") -> Path:
        cls.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = cls.BACKUP_DIR / f"{prefix}-{stamp}.sqlite3"
        create_sqlite_snapshot(backup_path)
        check = sqlite3.connect(str(backup_path))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise sqlite3.DatabaseError("Snapshot integrity check failed")
        finally:
            check.close()
        cls._prune_old_backups()
        return backup_path

    @staticmethod
    def has_user_data() -> bool:
        """Avoid replacing a useful Telegram backup with a freshly-reset empty Render DB."""
        if DATABASE_BACKEND == "sqlite" and not DATABASE_FILE.exists():
            return False
        conn = get_connection()
        try:
            for table in ("vault_items", "reminders", "rss_feeds", "collections"):
                try:
                    if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                        return True
                except sqlite3.OperationalError:
                    continue
            return False
        finally:
            conn.close()

    @staticmethod
    def get_meta(key: str, default: str | None = None) -> str | None:
        conn = get_connection()
        row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default

    @staticmethod
    def set_meta(key: str, value: str | None) -> None:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO bot_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
        conn.close()

    @classmethod
    def last_backup_at(cls) -> str | None:
        return cls.get_meta("last_backup_at")

    @classmethod
    def items_since_backup(cls) -> int:
        raw = cls.get_meta("items_since_backup", "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    @classmethod
    def _prune_old_backups(cls) -> None:
        backups = sorted(
            cls.BACKUP_DIR.glob("bot-backup-*.sqlite3"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[cls.KEEP_LAST:]:
            try:
                old.unlink()
            except Exception:
                logger.exception("Failed to remove old backup %s", old)

    @classmethod
    async def send_backup(cls, bot: Bot, recipients: Iterable[int]) -> Path | None:
        recipients = [int(uid) for uid in recipients if uid is not None]
        if not recipients:
            return None

        if not await asyncio.to_thread(cls.has_user_data):
            logger.warning("Skipping empty database backup; restore an existing Telegram snapshot first")
            return None

        backup_time = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")
        path = await asyncio.to_thread(cls.create_snapshot)
        logger.info("Preparing automatic backup snapshot size_bytes=%s recipient_count=%s",
                    path.stat().st_size, len(recipients))
        # Record the successful snapshot's timestamp inside the snapshot itself,
        # so restoring it does not immediately create another copy as if none existed.
        snapshot_db = sqlite3.connect(str(path))
        try:
            snapshot_db.execute(
                "INSERT INTO bot_meta (key, value) VALUES ('last_backup_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (backup_time,),
            )
            snapshot_db.execute(
                "INSERT INTO bot_meta (key, value) VALUES ('items_since_backup', '0') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            snapshot_db.commit()
            integrity = snapshot_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise sqlite3.DatabaseError("Snapshot integrity check failed before upload")
        finally:
            snapshot_db.close()
        item_count = 0
        count_db = sqlite3.connect(str(path))
        try:
            item_count = int(count_db.execute("SELECT COUNT(*) FROM vault_items").fetchone()[0])
        finally:
            count_db.close()
        caption = (
            "🗄 Резервная копия базы личного архива\n\n"
            f"Создана: {backup_time} UTC\n"
            f"Материалов в каталоге: {item_count}\n"
            "Не удаляй это сообщение. Для восстановления ответь на него командой /restore."
        )

        sent = False
        for chat_id in recipients:
            try:
                await bot.send_document(
                    chat_id,
                    document=FSInputFile(path),
                    caption=caption,
                )
                sent = True
            except Exception:
                logger.exception("Failed to send backup to %s", chat_id)

        if not sent:
            return None

        await asyncio.to_thread(cls.set_meta, "last_backup_at", backup_time)
        await asyncio.to_thread(cls.set_meta, "items_since_backup", "0")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Uploaded backup left a temporary local snapshot: %s", path)
        return path

async def backup_worker(
    bot: Bot,
    recipients: Iterable[int],
    *,
    interval_seconds: int = 24 * 60 * 60,
) -> None:
    recipients = [int(uid) for uid in recipients if uid is not None]
    if not recipients:
        logger.info("Backup worker disabled: no recipients configured")
        return

    interval_seconds = max(24 * 60 * 60, min(int(interval_seconds), 48 * 60 * 60))
    last_backup = await asyncio.to_thread(BackupService.last_backup_at)
    wait_seconds = 0
    if last_backup:
        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last_backup)).total_seconds()
            wait_seconds = max(0, interval_seconds - max(0, elapsed))
        except ValueError:
            logger.warning("Ignoring invalid last_backup_at value")
    if wait_seconds:
        await asyncio.sleep(wait_seconds)
    retry_attempts = 0
    while True:
        try:
            if await asyncio.to_thread(BackupService.has_user_data):
                result = await BackupService.send_backup(bot, recipients)
                if result is None:
                    retry_attempts += 1
                    delay = min(interval_seconds, 15 * 60 * (2 ** min(retry_attempts - 1, 3)))
                    if retry_attempts >= 4:
                        logger.error("Backup upload failed %s times; pausing until the next scheduled interval",
                                     retry_attempts)
                        retry_attempts = 0
                        delay = interval_seconds
                    else:
                        logger.error("Backup upload failed; retry attempt=%s/3 in %s seconds",
                                     retry_attempts, delay)
                    await asyncio.sleep(delay)
                    continue
                retry_attempts = 0
                await asyncio.sleep(interval_seconds)
            else:
                logger.warning("Automatic backup skipped because the database is empty")
                # A newly deployed Render instance may need a manual /restore first.
                # Recheck periodically so the daily schedule resumes after restoration.
                await asyncio.sleep(30 * 60)
        except Exception:
            logger.exception("Backup worker failed")
            retry_attempts += 1
            delay = min(interval_seconds, 15 * 60 * (2 ** min(retry_attempts - 1, 3)))
            if retry_attempts >= 4:
                logger.error("Backup worker reached retry limit; pausing until the next scheduled interval")
                retry_attempts = 0
                delay = interval_seconds
            await asyncio.sleep(delay)
