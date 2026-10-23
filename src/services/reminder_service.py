from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from src.db.database import get_connection
from src.keyboards.reminder_keyboard import reminder_notification_keyboard
from src.utils.text_cleaner import normalize_multiline_input

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_RETRY_MINUTES = 5


def _now_str() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")


class ReminderService:
    @staticmethod
    def _row_to_dict(row):
        return dict(row) if row is not None else None

    @staticmethod
    def create_reminder(
        user_id: int,
        title: str,
        due_at: datetime,
        repeat_minutes: int | None = None,
        note: str | None = None,
        source_item_id: int | None = None,
    ):
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO reminders (
                user_id,
                title,
                due_at,
                repeat_minutes,
                status,
                created_at,
                completed_at,
                last_notified_at,
                next_retry_at,
                retry_count,
                source_item_id,
                note
            )
            VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, NULL, 0, ?, ?)
            """,
            (
                user_id,
                normalize_multiline_input(title).strip(),
                due_at.replace(microsecond=0).isoformat(sep=" "),
                repeat_minutes,
                _now_str(),
                source_item_id,
                normalize_multiline_input(note) if note is not None else None,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def list_reminders(user_id: int, include_done: bool = False, limit: int = 50):
        conn = get_connection()
        if include_done:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE user_id = ?
                ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, due_at ASC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM reminders
                WHERE user_id = ? AND status = 'pending'
                ORDER BY due_at ASC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def count_reminders(user_id: int | None = None, *, pending_only: bool = False) -> int:
        conn = get_connection()
        clauses = []
        params = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if pending_only:
            clauses.append("status = 'pending'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(f"SELECT COUNT(*) AS n FROM reminders {where}", params).fetchone()
        conn.close()
        return int(row["n"] or 0)

    @staticmethod
    def today_reminders(user_id: int, limit: int = 20):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ?
              AND status = 'pending'
              AND DATE(due_at) = DATE('now')
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def upcoming_reminders(user_id: int, limit: int = 10):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ?
              AND status = 'pending'
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_reminder(user_id: int, reminder_id: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def delete_reminder(user_id: int, reminder_id: int):
        conn = get_connection()
        conn.execute(
            """
            DELETE FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def mark_done(user_id: int, reminder_id: int):
        conn = get_connection()
        conn.execute(
            """
            UPDATE reminders
            SET status = 'done',
                completed_at = ?,
                next_retry_at = NULL,
                last_notified_at = NULL,
                retry_count = 0
            WHERE user_id = ? AND id = ?
            """,
            (_now_str(), user_id, reminder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def snooze_reminder(user_id: int, reminder_id: int, minutes: int):
        conn = get_connection()
        current = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        if not current:
            conn.close()
            return None

        base = datetime.fromisoformat(str(current["due_at"])) if current["due_at"] else datetime.utcnow()
        new_due = max(datetime.utcnow(), base) + timedelta(minutes=minutes)
        conn.execute(
            """
            UPDATE reminders
            SET due_at = ?,
                status = 'pending',
                completed_at = NULL,
                next_retry_at = NULL,
                last_notified_at = NULL,
                retry_count = 0
            WHERE user_id = ? AND id = ?
            """,
            (new_due.replace(microsecond=0).isoformat(sep=" "), user_id, reminder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def update_due(user_id: int, reminder_id: int, due_at: datetime):
        conn = get_connection()
        conn.execute(
            """
            UPDATE reminders
            SET due_at = ?,
                status = 'pending',
                completed_at = NULL,
                next_retry_at = NULL,
                last_notified_at = NULL,
                retry_count = 0
            WHERE user_id = ? AND id = ?
            """,
            (due_at.replace(microsecond=0).isoformat(sep=" "), user_id, reminder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def due_reminders(limit: int = 20):
        now = _now_str()
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE status = 'pending'
              AND due_at <= ?
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (now, now, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def reschedule_recurring(reminder_id: int, repeat_minutes: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE id = ?
            """,
            (reminder_id,),
        ).fetchone()
        if not row:
            conn.close()
            return None

        due_at = datetime.fromisoformat(str(row["due_at"])) if row["due_at"] else datetime.utcnow()
        next_due = due_at + timedelta(minutes=repeat_minutes)
        while next_due <= datetime.utcnow():
            next_due += timedelta(minutes=repeat_minutes)

        conn.execute(
            """
            UPDATE reminders
            SET due_at = ?,
                status = 'pending',
                completed_at = NULL,
                repeat_minutes = ?,
                next_retry_at = NULL,
                last_notified_at = NULL,
                retry_count = 0
            WHERE id = ?
            """,
            (next_due.replace(microsecond=0).isoformat(sep=" "), repeat_minutes, reminder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE id = ?
            """,
            (reminder_id,),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def register_notification(user_id: int, reminder_id: int, retry_minutes: int = DEFAULT_REMINDER_RETRY_MINUTES):
        conn = get_connection()
        now = datetime.utcnow().replace(microsecond=0)
        next_retry = now + timedelta(minutes=retry_minutes)
        conn.execute(
            """
            UPDATE reminders
            SET last_notified_at = ?,
                next_retry_at = ?,
                retry_count = COALESCE(retry_count, 0) + 1
            WHERE user_id = ? AND id = ?
            """,
            (
                now.isoformat(sep=" "),
                next_retry.isoformat(sep=" "),
                user_id,
                reminder_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, reminder_id),
        ).fetchone()
        conn.close()
        return row


async def reminder_worker(bot, interval_seconds: int = 30):
    while True:
        try:
            due = ReminderService.due_reminders()
            for row in due:
                reminder_id = int(row["id"])
                title = normalize_multiline_input(row["title"]).strip()
                user_id = row["user_id"]
                retry_count = int(row["retry_count"] or 0)

                extra = "Если не нажмёшь кнопку, я повторю это через 5 минут."
                if retry_count > 0:
                    extra = f"Повтор #{retry_count + 1}. Если не нажмёшь кнопку, я повторю это через 5 минут."

                await bot.send_message(
                    user_id,
                    f"🔔 Напоминание\n\n{title}\n\n{extra}",
                    reply_markup=reminder_notification_keyboard(reminder_id, row["source_item_id"]),
                )

                ReminderService.register_notification(user_id, reminder_id, DEFAULT_REMINDER_RETRY_MINUTES)
        except Exception:
            logger.exception("Reminder worker failed")

        await asyncio.sleep(interval_seconds)
