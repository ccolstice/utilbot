from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import uuid
from contextlib import suppress
from typing import Any

import aiohttp

from src.config import Settings
from src.db.database import get_connection
from src.services.chatgpt_ai_service import ChatGptAiService

logger = logging.getLogger(__name__)

FLIP_MAP = str.maketrans({
    **dict(zip("abcdefghijklmnopqrstuvwxyz", "ɐqɔpǝɟƃɥᴉɾʞןɯuodbɹsʇnʌʍxʎz")),
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "∀𐐒ƆᗡƎℲפHIſꓘ˥WNOԀΌᴚS⊥∩ΛMX⅄Z")),
    **dict(zip("0123456789", "0ƖᄅƐㄣϛ9ㄥ86")),
    # Cyrillic has no reliable upside-down equivalents. Keep its alphabet
    # intact; only reverse the text order instead of transliterating to Latin.
    **{chr(codepoint): chr(codepoint) for codepoint in range(0x0400, 0x0530)},
    **{"!": "¡", "?": "¿", ".": "˙", ",": "'", "'": ",", "(": ")", ")": "(",
       "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "_": "‾", "&": "⅋", ";": "؛"},
})
TINY_MAP = str.maketrans({
    **dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ")),
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
})
TINY_CYRILLIC_MAP = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "ʺ", "ы": "y", "ь": "ʹ", "э": "e", "ю": "yu", "я": "ya",
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ё": "Yo",
    "Ж": "Zh", "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M",
    "Н": "N", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U",
    "Ф": "F", "Х": "H", "Ц": "Ts", "Ч": "Ch", "Ш": "Sh", "Щ": "Shch",
    "Ъ": "ʺ", "Ы": "Y", "Ь": "ʹ", "Э": "E", "Ю": "Yu", "Я": "Ya",
})

COMMAND_RE = re.compile(r"^\.(mute|unmute|typing|type|del|spam|hack|flip|tiny)(?:\s+([\s\S]*))?$", re.IGNORECASE)
AI_MODE_COMMANDS = {".быдло": "bydlo", ".троль": "troll", ".офис": "office", ".все": None}
DURATION_RE = re.compile(r"^(\d+)(s|sec|сек|m|min|мин|h|час|d|д)?$", re.IGNORECASE)
TYPE_CHARACTER_INTERVAL = 0.12
TELEGRAM_TEXT_LIMIT = 4096


def _telegram_text_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, payload: dict[str, Any]):
        self.method = method
        self.code = payload.get("error_code")
        self.retry_after = (payload.get("parameters") or {}).get("retry_after")
        super().__init__(str(payload.get("description") or f"Telegram API {method} failed"))


class SecretaryPollingConflict(RuntimeError):
    """Another process is already polling this secretary bot token."""


def _db(callable_):
    # A failed Turso interactive transaction is rolled back remotely. Retry
    # the complete database-only callback on a fresh connection so a transient
    # Hrana SQLITE_BUSY does not strand time-sensitive jobs such as .type.
    delays = (0.15, 0.35, 0.75, 1.5)
    for attempt in range(len(delays) + 1):
        conn = get_connection()
        try:
            result = callable_(conn)
            conn.commit()
            return result
        except Exception as error:
            message = str(error).lower()
            retryable = (
                "sqlite_busy" in message
                or "database is locked" in message
                or "stream not found" in message
            )
            if not retryable or attempt >= len(delays):
                raise
            with suppress(Exception):
                conn.rollback()
            delay = delays[attempt] + random.uniform(0, 0.1)
            logger.warning("Secretary DB transaction busy; retrying attempt=%s/%s in %.2fs",
                           attempt + 1, len(delays), delay)
            time.sleep(delay)
        finally:
            with suppress(Exception):
                conn.close()


def _row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class SecretaryService:
    """Small raw Bot API poller for the second, Business-enabled bot token."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.token = settings.secretary_bot_token
        self.owner_id = settings.secretary_owner_user_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.session: aiohttp.ClientSession | None = None
        self.active_mutes: dict[tuple[str, int], float] = {}
        self.scheduler_task: asyncio.Task | None = None
        self.scheduler_wakeup = asyncio.Event()
        self.chat_write_times: dict[tuple[str, int], float] = {}
        self.ai_modes: dict[tuple[str, int], str | None] = {}
        self.ai_reply_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self.ai_reply_tasks: set[asyncio.Task] = set()

    async def _api(self, method: str, data: dict[str, Any] | None = None, *, timeout: int = 40):
        if self.session is None:
            raise RuntimeError("Secretary Bot API session is not ready")
        async with self.session.post(
            f"{self.base_url}/{method}",
            json=data or {},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                payload = {}
            if response.status != 200 or not payload.get("ok"):
                raise TelegramApiError(method, payload)
            return payload.get("result")

    async def _db(self, callable_):
        return await asyncio.to_thread(_db, callable_)

    async def _remember_chat(self, connection_id: str, chat: dict[str, Any], message_date: int | None = None):
        chat_id = chat.get("id")
        chat_type = str(chat.get("type") or "")
        if not chat_id or (not connection_id and chat_type not in {"group", "supergroup"}):
            return
        connection_id = str(connection_id or "")
        now = time.time()
        key = (connection_id, int(chat_id))
        if now - self.chat_write_times.get(key, 0) < 60:
            return
        self.chat_write_times[key] = now
        seen_at = float(message_date or now)

        def save(conn):
            conn.execute(
                """INSERT INTO secretary_chats
                   (business_connection_id, chat_id, chat_type, title, username, first_name, last_name, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET
                     chat_type=excluded.chat_type, title=excluded.title, username=excluded.username,
                     first_name=excluded.first_name, last_name=excluded.last_name,
                     last_seen_at=MAX(secretary_chats.last_seen_at, excluded.last_seen_at)""",
                (connection_id, int(chat_id), chat_type or "private", chat.get("title"),
                 chat.get("username"), chat.get("first_name"), chat.get("last_name"), seen_at),
            )

        await self._db(save)

    async def _remember_chat_safely(self, connection_id: str, chat: dict[str, Any], message_date: int | None = None):
        try:
            await self._remember_chat(connection_id, chat, message_date)
        except Exception:
            logger.exception("Secretary could not update recent-chat list chat_id=%s", chat.get("id"))

    async def _create_job(self, kind: str, connection_id: str, chat_id: int, *, message_id: int | None = None,
                          payload: dict[str, Any] | None = None, next_at: float | None = None,
                          expires_at: float | None = None, job_id: str | None = None):
        job_id = job_id or uuid.uuid4().hex
        created = time.time()
        encoded = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))

        def insert(conn):
            if kind in {"mute", "typing"}:
                conn.execute(
                    "UPDATE secretary_jobs SET status='done' WHERE kind=? AND business_connection_id=? "
                    "AND chat_id=? AND status='active'",
                    (kind, connection_id, int(chat_id)),
                )
            elif kind in {"type", "hack"}:
                conn.execute(
                    "UPDATE secretary_jobs SET status='done' WHERE kind IN ('type','hack') "
                    "AND business_connection_id=? AND chat_id=? AND status='active'",
                    (connection_id, int(chat_id)),
                )
            conn.execute(
                "INSERT OR REPLACE INTO secretary_jobs "
                "(id, kind, business_connection_id, chat_id, message_id, payload, next_at, expires_at, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (job_id, kind, connection_id, int(chat_id), message_id, encoded,
                 float(next_at if next_at is not None else created), expires_at, created),
            )

        await self._db(insert)
        self.scheduler_wakeup.set()
        return job_id

    async def _finish_job(self, job_id: str):
        await self._db(lambda conn: conn.execute(
            "UPDATE secretary_jobs SET status='done' WHERE id=?", (job_id,)
        ))

    async def _update_job(self, job_id: str, *, payload: dict[str, Any] | None = None,
                          next_at: float | None = None, expires_at: float | None = None,
                          message_id: int | None = None):
        fields, values = [], []
        if payload is not None:
            fields.append("payload=?")
            values.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if next_at is not None:
            fields.append("next_at=?")
            values.append(float(next_at))
        if expires_at is not None:
            fields.append("expires_at=?")
            values.append(float(expires_at))
        if message_id is not None:
            fields.append("message_id=?")
            values.append(int(message_id))
        if not fields:
            return
        values.append(job_id)
        await self._db(lambda conn: conn.execute(
            f"UPDATE secretary_jobs SET {', '.join(fields)} WHERE id=? AND status='active'", tuple(values)
        ))
        if next_at is not None:
            self.scheduler_wakeup.set()

    async def _load_startup_state(self):
        def load(conn):
            conn.execute("DELETE FROM secretary_jobs WHERE status='done' AND created_at<?", (time.time() - 30 * 86400,))
            jobs = conn.execute(
                "SELECT * FROM secretary_jobs WHERE status='active' ORDER BY next_at"
            ).fetchall()
            meta = conn.execute(
                "SELECT value FROM secretary_meta WHERE key='last_update_id'"
            ).fetchone()
            return [_row_to_dict(row) for row in jobs], int(meta["value"]) + 1 if meta else None

        jobs, offset = await self._db(load)
        for job in jobs:
            if job["kind"] == "mute" and job.get("expires_at"):
                self.active_mutes[(job["business_connection_id"], int(job["chat_id"]))] = float(job["expires_at"])
        return jobs, offset

    async def _save_offset(self, update_id: int):
        await self._db(lambda conn: conn.execute(
            "INSERT INTO secretary_meta(key, value) VALUES('last_update_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(update_id),)
        ))

    async def _list_chats(self):
        def load(conn):
            rows = conn.execute(
                "SELECT business_connection_id, chat_id, chat_type, title, username, first_name, last_name "
                "FROM secretary_chats WHERE chat_type IN ('private','group','supergroup') "
                "ORDER BY last_seen_at DESC LIMIT 20"
            ).fetchall()
            return [_row_to_dict(row) for row in rows]
        return await self._db(load)

    async def _get_chat(self, chat_id: int):
        return await self._db(lambda conn: _row_to_dict(conn.execute(
            "SELECT * FROM secretary_chats WHERE chat_id=? ORDER BY last_seen_at DESC LIMIT 1", (int(chat_id),)
        ).fetchone()))

    async def _set_session(self, owner_id: int, connection_id: str, chat_id: int):
        await self._db(lambda conn: conn.execute(
            "INSERT INTO secretary_sessions(owner_id, business_connection_id, chat_id, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET business_connection_id=excluded.business_connection_id, "
            "chat_id=excluded.chat_id, updated_at=excluded.updated_at",
            (int(owner_id), connection_id, int(chat_id), time.time()),
        ))

    async def _get_session(self, owner_id: int):
        return await self._db(lambda conn: _row_to_dict(conn.execute(
            "SELECT c.* FROM secretary_sessions s JOIN secretary_chats c "
            "ON c.business_connection_id=s.business_connection_id AND c.chat_id=s.chat_id WHERE s.owner_id=?",
            (int(owner_id),),
        ).fetchone()))

    @staticmethod
    def _chat_label(chat: dict[str, Any]) -> str:
        title = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
        if not title:
            title = f"Чат {chat.get('chat_id')}"
        if chat.get("username"):
            title += f" (@{chat['username']})"
        return title[:60]

    async def _show_chats(self, chat_id: int):
        chats = await self._list_chats()
        if not chats:
            await self._render_control_panel(chat_id,
                "Пока не видел чатов через Secretary Mode. Сначала включи секретаря для нужного чата и отправь там сообщение.",
                [[{"text": "🏠 Меню", "callback_data": "hist:menu"}]])
            return
        keyboard = []
        for chat in chats:
            keyboard.append([{"text": self._chat_label(chat), "callback_data": f"sec:{chat['chat_id']}"}])
        keyboard.append([{"text": "🏠 Меню", "callback_data": "hist:menu"}])
        await self._render_control_panel(chat_id,
            "Выбери чат для команд. Команды из этой лички не отправляются собеседнику; `.type текст` отправляет только сам текст.",
            keyboard)

    async def _render_control_panel(self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]],
                                   message_id: int | None = None):
        """Keep one persistent, editable control panel in the owner's DM."""
        await self._clear_history_media(chat_id)
        key = f"control_panel:{self.owner_id}"
        saved = await self._db(lambda conn: conn.execute(
            "SELECT value FROM secretary_meta WHERE key=?", (key,)
        ).fetchone())
        old_id = int(saved["value"]) if saved and str(saved["value"]).isdigit() else None
        panel_id = message_id or old_id
        params = {"chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard}}
        if panel_id:
            params["message_id"] = panel_id
            try:
                await self._api("editMessageText", params)
            except TelegramApiError as error:
                if error.code == 400 and "message is not modified" in str(error).lower():
                    await self._db(lambda conn: conn.execute(
                        "INSERT INTO secretary_meta(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(panel_id)),
                    ))
                    if old_id and old_id != panel_id:
                        with suppress(Exception):
                            await self._api("deleteMessage", {"chat_id": chat_id, "message_id": old_id})
                    return
                elif error.code not in {400, 403}:
                    raise
                else:
                    panel_id = None
            else:
                await self._db(lambda conn: conn.execute(
                    "INSERT INTO secretary_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(panel_id)),
                ))
                if old_id and old_id != panel_id:
                    with suppress(Exception):
                        await self._api("deleteMessage", {"chat_id": chat_id, "message_id": old_id})
                return
        sent = await self._api("sendMessage", {
            "chat_id": chat_id, "text": text, "reply_markup": {"inline_keyboard": keyboard},
        })
        new_id = int(sent["message_id"])
        await self._db(lambda conn: conn.execute(
            "INSERT INTO secretary_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(new_id)),
        ))
        if panel_id and panel_id != new_id:
            with suppress(Exception):
                await self._api("deleteMessage", {"chat_id": chat_id, "message_id": panel_id})

    async def _clear_history_media(self, chat_id: int):
        key = f"history_media:{self.owner_id}"
        saved = await self._db(lambda conn: conn.execute(
            "SELECT value FROM secretary_meta WHERE key=?", (key,)
        ).fetchone())
        if saved and str(saved["value"]).isdigit():
            with suppress(Exception):
                await self._api("deleteMessage", {"chat_id": chat_id, "message_id": int(saved["value"])})
            await self._db(lambda conn: conn.execute("DELETE FROM secretary_meta WHERE key=?", (key,)))

    async def _show_history_media(self, chat_id: int, message: dict[str, Any] | None):
        media = self._message_media(message)
        if not media:
            return
        method, field, file_id = media
        sent = await self._api(method, {"chat_id": chat_id, field: file_id})
        await self._db(lambda conn: conn.execute(
            "INSERT INTO secretary_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"history_media:{self.owner_id}", str(sent["message_id"])),
        ))

    async def _command_feedback(self, control_chat_id: int, target_chat_id: int, status: str):
        target = await self._get_chat(target_chat_id)
        label = self._chat_label(target) if target else f"Чат {target_chat_id}"
        await self._render_control_panel(control_chat_id, f"{status}\nЧат: {label}", [
            [{"text": "💬 Выбрать чат", "callback_data": "hist:chats"}],
            [{"text": "🗂 Архив", "callback_data": "hist:root"}],
            [{"text": "🏠 Меню", "callback_data": "hist:menu"}],
        ])

    async def _send_dm_help(self, chat_id: int):
        await self._render_control_panel(chat_id,
            "Команды в выбранном чате:\n"
            "`.mute` — удалить сообщения собеседника минуту; `.unmute` — снять режим\n"
            "`.typing 30min` — показывать «печатает» заданное время\n"
            "`.type текст` — показать текст поэтапно\n"
            "`.del 100 текст` — отправить текст и удалить его через 100 секунд\n"
            "`.spam 5 текст` — отправить текст 5 раз\n"
            "`.hack`, `.flip текст`, `.tiny текст`\n\n"
            "Команды `.mute`, `.unmute` и `.typing` не отправляют текст собеседнику.\n"
            "/deleted — архив удалённых сообщений по авторам\n"
            "/edited — архив изменений по авторам\n"
            "/chats — выбрать чат заново", [[{"text": "🏠 Меню", "callback_data": "hist:menu"}]])

    async def _send_dm_menu(self, chat_id: int, message_id: int | None = None):
        await self._render_control_panel(chat_id,
            "Секретарь. Архив сообщений разложен по авторам; выбранный чат используется для команд.", [
                [{"text": "🗂 Архив", "callback_data": "hist:root"}],
                [{"text": "💬 Выбрать чат", "callback_data": "hist:chats"}],
                [{"text": "❔ Команды", "callback_data": "hist:help"}],
            ], message_id)

    @staticmethod
    def _sender_name(sender: dict[str, Any]) -> str:
        name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
        if sender.get("username"):
            name += f" (@{sender['username']})"
        return name or ("Бот" if sender.get("is_bot") else "Участник чата")

    @staticmethod
    def _message_content(message: dict[str, Any] | None) -> str:
        if not message:
            return "[предыдущая версия недоступна]"
        text = str(message.get("text") or message.get("caption") or "").strip()
        media_names = {
            "photo": "фото", "video": "видео", "animation": "анимация", "voice": "голосовое",
            "audio": "аудио", "video_note": "видеосообщение", "document": "файл",
            "sticker": "стикер", "location": "геопозиция", "venue": "место",
            "contact": "контакт", "poll": "опрос", "dice": "кубик",
        }
        labels = [f"[{label}]" for field, label in media_names.items() if message.get(field)]
        if text and labels:
            return f"{text}\n{' '.join(labels)}"
        return text or " ".join(labels) or "[сообщение без текста]"

    @staticmethod
    def _message_media(message: dict[str, Any] | None) -> tuple[str, str, str] | None:
        if not message:
            return None
        for field, method, parameter in (
            ("photo", "sendPhoto", "photo"), ("video", "sendVideo", "video"),
            ("animation", "sendAnimation", "animation"), ("voice", "sendVoice", "voice"),
            ("audio", "sendAudio", "audio"), ("video_note", "sendVideoNote", "video_note"),
            ("document", "sendDocument", "document"), ("sticker", "sendSticker", "sticker"),
        ):
            value = message.get(field)
            if isinstance(value, list) and value:
                value = value[-1]
            if isinstance(value, dict) and value.get("file_id"):
                return method, parameter, str(value["file_id"])
        return None

    async def _save_message_snapshot(self, message: dict[str, Any], *, ignore_delete: bool = False):
        connection_id = str(message.get("business_connection_id") or "")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        if (not connection_id or chat.get("type") != "private" or not chat_id or not message_id
                or not int(sender.get("id") or 0)):
            return
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        await self._db(lambda conn: conn.execute(
            "INSERT INTO secretary_message_snapshots "
            "(business_connection_id, chat_id, message_id, sender_id, sender_name, message_json, ignore_delete, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(business_connection_id, chat_id, message_id) DO UPDATE SET "
            "sender_id=excluded.sender_id, sender_name=excluded.sender_name, message_json=excluded.message_json, "
            "ignore_delete=excluded.ignore_delete, updated_at=excluded.updated_at",
            (connection_id, chat_id, message_id, int(sender.get("id") or 0), self._sender_name(sender),
             encoded, int(ignore_delete), time.time()),
        ))

    async def _set_ai_mode(self, connection_id: str, chat_id: int, mode: str | None):
        key = (connection_id, int(chat_id))
        if mode is None:
            await self._db(lambda conn: conn.execute(
                "DELETE FROM secretary_ai_modes WHERE business_connection_id=? AND chat_id=?", key,
            ))
        else:
            await self._db(lambda conn: conn.execute(
                "INSERT INTO secretary_ai_modes(business_connection_id, chat_id, mode, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET "
                "mode=excluded.mode, updated_at=excluded.updated_at",
                (connection_id, int(chat_id), mode, time.time()),
            ))
        self.ai_modes[key] = mode

    async def _get_ai_mode(self, connection_id: str, chat_id: int) -> str | None:
        key = (connection_id, int(chat_id))
        if key not in self.ai_modes:
            row = await self._db(lambda conn: conn.execute(
                "SELECT mode FROM secretary_ai_modes WHERE business_connection_id=? AND chat_id=?",
                key,
            ).fetchone())
            self.ai_modes[key] = str(row["mode"]) if row else None
        return self.ai_modes[key]

    async def _ai_context(self, connection_id: str, chat_id: int, message_id: int) -> tuple[str, dict[str, Any] | None]:
        rows = await self._db(lambda conn: conn.execute(
            "SELECT message_id, message_json FROM secretary_message_snapshots "
            "WHERE business_connection_id=? AND chat_id=? AND message_id<=? AND updated_at>=? "
            "ORDER BY updated_at DESC LIMIT 12",
            (connection_id, int(chat_id), int(message_id), time.time() - 6 * 60 * 60),
        ).fetchall())
        lines = []
        trigger_message = None
        for row in reversed(rows):
            try:
                message = json.loads(row["message_json"])
            except (TypeError, ValueError):
                continue
            if int(row["message_id"]) == int(message_id):
                trigger_message = message
            sender = message.get("from") or {}
            text = str(message.get("text") or message.get("caption") or "").strip()
            if int(sender.get("id") or 0) == self.owner_id and text.startswith("."):
                continue
            if not text:
                text = self._message_content(message)
            lines.append(f"{self._sender_name(sender)}: {text[:1200]}")
        return "\n".join(lines)[-9000:], trigger_message

    async def _secretary_image(self, message: dict[str, Any] | None) -> tuple[str, str]:
        photos = (message or {}).get("photo") or []
        if not photos or not isinstance(photos[-1], dict) or not photos[-1].get("file_id"):
            return "", ""
        if self.session is None:
            raise RuntimeError("Secretary Telegram session is not ready")
        file_info = await self._api("getFile", {"file_id": photos[-1]["file_id"]})
        if int(file_info.get("file_size") or 0) > 15 * 1024 * 1024:
            raise ValueError("Фото больше 15 МБ и не передано в ИИ-режим.")
        file_path = str(file_info.get("file_path") or "")
        if not file_path:
            return "", ""
        async with self.session.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}") as response:
            if response.status != 200:
                raise RuntimeError(f"Telegram не отдал фото (HTTP {response.status}).")
            image = await response.read()
        if not image or len(image) > 15 * 1024 * 1024:
            raise ValueError("Фото больше 15 МБ и не передано в ИИ-режим.")
        lowered = file_path.lower()
        mime_type = "image/png" if lowered.endswith(".png") else "image/webp" if lowered.endswith(".webp") else "image/jpeg"
        return base64.b64encode(image).decode("ascii"), mime_type

    async def _queue_ai_reply(self, connection_id: str, chat_id: int, message: dict[str, Any], mode: str):
        message_id = int(message.get("message_id") or 0)
        key = (connection_id, int(chat_id))
        lock = self.ai_reply_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if await self._get_ai_mode(connection_id, chat_id) != mode:
                return
            try:
                context, trigger_message = await self._ai_context(connection_id, chat_id, message_id)
                if trigger_message is None:
                    trigger_message = message
                    sender = message.get("from") or {}
                    trigger_text = str(message.get("text") or message.get("caption") or "").strip()
                    if not trigger_text:
                        trigger_text = self._message_content(message)
                    line = f"{self._sender_name(sender)}: {trigger_text[:1200]}"
                    if line not in context.splitlines():
                        context = f"{context}\n{line}".strip()
                if not context:
                    return
                started_at = time.perf_counter()
                image_base64, mime_type = await self._secretary_image(trigger_message)
                reply = await ChatGptAiService.generate_secretary_reply(
                    self.settings, self.owner_id, mode, context,
                    image_base64=image_base64, mime_type=mime_type,
                )
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                if await self._get_ai_mode(connection_id, chat_id) != mode:
                    logger.info("Secretary AI reply discarded after mode changed chat_id=%s", chat_id)
                    return
                sent = await self._api("sendMessage", self._with_business(connection_id, {
                    "chat_id": int(chat_id), "text": reply[:3900],
                    "reply_parameters": {"message_id": int(message_id), "allow_sending_without_reply": True},
                }))
                logger.info("Secretary AI reply delivered chat_id=%s mode=%s elapsed_ms=%s message_id=%s",
                            chat_id, mode, elapsed_ms, sent.get("message_id"))
            except Exception:
                logger.exception("Secretary AI auto-reply failed chat_id=%s mode=%s", chat_id, mode)

    def _start_ai_reply(self, connection_id: str, chat_id: int, message: dict[str, Any], mode: str):
        task = asyncio.create_task(self._queue_ai_reply(connection_id, chat_id, message, mode))
        self.ai_reply_tasks.add(task)
        task.add_done_callback(self.ai_reply_tasks.discard)

    async def _save_edited_message(self, update_id: int, message: dict[str, Any]):
        connection_id = str(message.get("business_connection_id") or "")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        message_id = int(message.get("message_id") or 0)
        if not connection_id or chat.get("type") != "private" or not chat_id or not message_id:
            return
        sender_id = int(sender.get("id") or 0)
        if not sender_id or sender.get("is_bot"):
            return
        sender_name = self._sender_name(sender)
        current_json = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        chat_title = self._chat_label({**chat, "chat_id": chat_id})
        event_key = f"edit:{update_id}"

        def save(conn):
            previous = conn.execute(
                "SELECT * FROM secretary_message_snapshots WHERE business_connection_id=? AND chat_id=? AND message_id=?",
                (connection_id, chat_id, message_id),
            ).fetchone()
            previous_json = previous["message_json"] if previous else None
            old_sender_id = int(previous["sender_id"] or 0) if previous else sender_id
            added = False
            if old_sender_id != self.owner_id and sender_id != self.owner_id:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO secretary_message_events "
                    "(event_key, event_type, business_connection_id, chat_id, message_id, sender_id, sender_name, chat_title, old_message_json, new_message_json, created_at) "
                    "VALUES (?, 'edited', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_key, connection_id, chat_id, message_id, sender_id, sender_name, chat_title,
                     previous_json, current_json, time.time()),
                )
                added = cursor.rowcount > 0
            conn.execute(
                "INSERT INTO secretary_message_snapshots "
                "(business_connection_id, chat_id, message_id, sender_id, sender_name, message_json, ignore_delete, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?) ON CONFLICT(business_connection_id, chat_id, message_id) DO UPDATE SET "
                "sender_id=excluded.sender_id, sender_name=excluded.sender_name, message_json=excluded.message_json, "
                "ignore_delete=0, updated_at=excluded.updated_at",
                (connection_id, chat_id, message_id, sender_id, sender_name, current_json, time.time()),
            )
            if not added:
                return None
            row = conn.execute("SELECT * FROM secretary_message_events WHERE event_key=?", (event_key,)).fetchone()
            return _row_to_dict(row)

        await self._db(save)

    async def _consume_deleted_message(self, connection_id: str, chat: dict[str, Any], message_id: int):
        chat_id = int(chat.get("id") or 0)
        event_key = f"delete:{connection_id}:{chat_id}:{message_id}"
        chat_title = self._chat_label({**chat, "chat_id": chat_id})

        def consume(conn):
            snapshot = conn.execute(
                "SELECT * FROM secretary_message_snapshots WHERE business_connection_id=? AND chat_id=? AND message_id=?",
                (connection_id, chat_id, message_id),
            ).fetchone()
            if snapshot is None:
                return None
            conn.execute(
                "DELETE FROM secretary_message_snapshots WHERE business_connection_id=? AND chat_id=? AND message_id=?",
                (connection_id, chat_id, message_id),
            )
            if bool(snapshot["ignore_delete"]) or int(snapshot["sender_id"] or 0) == self.owner_id:
                return None
            cursor = conn.execute(
                "INSERT OR IGNORE INTO secretary_message_events "
                "(event_key, event_type, business_connection_id, chat_id, message_id, sender_id, sender_name, chat_title, old_message_json, new_message_json, created_at) "
                "VALUES (?, 'deleted', ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (event_key, connection_id, chat_id, message_id, snapshot["sender_id"], snapshot["sender_name"],
                 chat_title, snapshot["message_json"], time.time()),
            )
            if cursor.rowcount <= 0:
                return None
            return _row_to_dict(conn.execute(
                "SELECT * FROM secretary_message_events WHERE event_key=?", (event_key,)
            ).fetchone())

        return await self._db(consume)

    async def _show_history(self, chat_id: int, event_type: str, page: int = 0,
                            sender_id: int | None = None):
        page_size = 8
        label = "Удалённые" if event_type == "deleted" else "Изменённые"
        if sender_id is None:
            folders = await self._db(lambda conn: [
                _row_to_dict(row) for row in conn.execute(
                    "SELECT sender_id, MAX(sender_name) AS sender_name, COUNT(*) AS count, MAX(created_at) AS latest "
                    "FROM secretary_message_events WHERE event_type=? GROUP BY sender_id "
                    "ORDER BY latest DESC LIMIT ? OFFSET ?", (event_type, page_size, page * page_size),
                ).fetchall()
            ])
            total = await self._db(lambda conn: conn.execute(
                "SELECT COUNT(DISTINCT sender_id) AS count FROM secretary_message_events WHERE event_type=?",
                (event_type,),
            ).fetchone()["count"])
            keyboard = [[{"text": f"👤 {row['sender_name']} · {row['count']}",
                          "callback_data": f"hist:user:{event_type}:{row['sender_id']}:0"}]
                        for row in folders]
            body = f"{label} · папки авторов ({total})\nВыбери автора:" if total else f"{label}\nПока записей нет."
            back = "hist:root"
            if (page + 1) * page_size < total:
                keyboard.append([{"text": "➡️", "callback_data": f"hist:{event_type}:{page + 1}"}])
            if page > 0:
                keyboard.append([{"text": "⬅️ Назад", "callback_data": f"hist:{event_type}:{page - 1}"}])
        else:
            total = await self._db(lambda conn: conn.execute(
                "SELECT COUNT(*) AS count FROM secretary_message_events WHERE event_type=? AND sender_id=?",
                (event_type, sender_id),
            ).fetchone()["count"])
            rows = await self._db(lambda conn: [
                _row_to_dict(row) for row in conn.execute(
                    "SELECT id, sender_name, chat_title, old_message_json, new_message_json "
                    "FROM secretary_message_events WHERE event_type=? AND sender_id=? "
                    "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (event_type, sender_id, page_size, page * page_size),
                ).fetchall()
            ])
            keyboard = []
            for row in rows:
                raw = row.get("old_message_json") if event_type == "deleted" else row.get("new_message_json")
                snippet = self._message_content(json.loads(raw or "{}"))[:30]
                keyboard.append([{"text": f"{row['chat_title']} · {snippet}"[:60],
                                  "callback_data": f"hist:item:{event_type}:{row['id']}:{page}:{sender_id}"}])
            body = f"{label} · {total}\n{rows[0]['sender_name'] if rows else 'Автор'}" if total else f"{label}\nУ этого автора записей нет."
            back = f"hist:{event_type}:0"
            if (page + 1) * page_size < total:
                keyboard.append([{"text": "➡️", "callback_data": f"hist:user:{event_type}:{sender_id}:{page + 1}"}])
            if page > 0:
                keyboard.append([{"text": "⬅️", "callback_data": f"hist:user:{event_type}:{sender_id}:{page - 1}"}])
        keyboard.append([{"text": "⬅️ Папки", "callback_data": back},
                         {"text": "🏠 Меню", "callback_data": "hist:menu"}])
        await self._render_control_panel(chat_id, body, keyboard)

    async def _show_history_item(self, chat_id: int, event_type: str, event_id: int, page: int,
                                sender_id: int | None = None):
        row = await self._db(lambda conn: _row_to_dict(conn.execute(
            "SELECT * FROM secretary_message_events WHERE id=? AND event_type=?", (event_id, event_type)
        ).fetchone()))
        if not row:
            await self._render_control_panel(chat_id, "Запись не найдена.", [[{"text": "🏠 Меню", "callback_data": "hist:menu"}]])
            return
        old_message = json.loads(row.get("old_message_json") or "{}") or None
        new_message = json.loads(row.get("new_message_json") or "{}") or None
        old_text = self._message_content(old_message)
        if event_type == "deleted":
            body = f"🗑 Удалено в «{row['chat_title']}»\nАвтор: {row['sender_name']}\n\n{old_text}"
        else:
            body = (f"✏️ Изменено в «{row['chat_title']}»\nАвтор: {row['sender_name']}\n\n"
                    f"Было:\n{old_text}\n\nСтало:\n{self._message_content(new_message)}")
        back = f"hist:user:{event_type}:{sender_id}:{page}" if sender_id is not None else f"hist:{event_type}:{page}"
        await self._render_control_panel(chat_id, body[:4000], [[
            {"text": "⬅️ К списку", "callback_data": back},
            {"text": "🏠 Меню", "callback_data": "hist:menu"},
        ]])
        await self._show_history_media(chat_id, old_message if event_type == "deleted" else new_message)

    async def _callback(self, update: dict[str, Any]):
        query = update.get("callback_query") or {}
        sender = query.get("from") or {}
        message = query.get("message") or {}
        callback_data = str(query.get("data") or "")
        if int(sender.get("id") or 0) != self.owner_id:
            await self._api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Нет доступа", "show_alert": True})
            return
        if callback_data.startswith("hist:"):
            parts = callback_data.split(":")
            await self._api("answerCallbackQuery", {"callback_query_id": query.get("id")})
            chat_id = int((message.get("chat") or {}).get("id") or self.owner_id)
            message_id = int(message.get("message_id") or 0)
            if parts[1] == "menu":
                await self._send_dm_menu(chat_id, message_id or None)
            elif parts[1] == "chats":
                await self._show_chats(chat_id)
            elif parts[1] == "help":
                await self._send_dm_help(chat_id)
            elif parts[1] == "root":
                await self._render_control_panel(chat_id, "Архив сообщений. Выбери тип:", [
                    [{"text": "🗑 Удалённые", "callback_data": "hist:deleted:0"}],
                    [{"text": "✏️ Изменённые", "callback_data": "hist:edited:0"}],
                    [{"text": "🏠 Меню", "callback_data": "hist:menu"}],
                ])
            elif parts[1] == "item" and len(parts) in {5, 6}:
                sender_id = int(parts[5]) if len(parts) == 6 else None
                await self._show_history_item(chat_id, parts[2], int(parts[3]), int(parts[4]), sender_id)
            elif parts[1] == "user" and len(parts) == 5:
                await self._show_history(chat_id, parts[2], max(0, int(parts[4])), int(parts[3]))
            elif parts[1] in {"deleted", "edited"} and len(parts) == 3:
                await self._show_history(chat_id, parts[1], max(0, int(parts[2])))
            return
        if not callback_data.startswith("sec:"):
            await self._api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Некорректная кнопка"})
            return
        try:
            target_id = int(callback_data.split(":", 1)[1])
        except ValueError:
            await self._api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Некорректный чат"})
            return
        # Telegram expires callback queries quickly; acknowledge before any
        # remote Turso reads/writes so database latency cannot invalidate it.
        await self._api("answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Открываю чат…"})
        target = await self._get_chat(target_id)
        if not target:
            await self._render_control_panel(int((message.get("chat") or {}).get("id") or self.owner_id),
                "Этот чат уже недоступен. Выбери другой.", [[
                    {"text": "💬 Выбрать чат", "callback_data": "hist:chats"},
                    {"text": "🏠 Меню", "callback_data": "hist:menu"},
                ]])
            return
        await self._set_session(self.owner_id, target["business_connection_id"], target_id)
        await self._render_control_panel(int((message.get("chat") or {}).get("id") or self.owner_id),
            f"Выбран чат: {self._chat_label(target)}. Пришли команду сюда или открой справку.", [
                [{"text": "❔ Команды", "callback_data": "hist:help"}],
                [{"text": "💬 Выбрать другой чат", "callback_data": "hist:chats"}],
                [{"text": "🏠 Меню", "callback_data": "hist:menu"}],
            ], int(message.get("message_id") or 0) or None)

    async def _handle_dm(self, message: dict[str, Any]):
        sender = message.get("from") or {}
        if int(sender.get("id") or 0) != self.owner_id:
            return
        control_chat = message.get("chat") or {}
        if control_chat.get("type") != "private":
            return
        chat_id = int(control_chat.get("id") or 0)
        raw_text = str(message.get("text") or "")
        text = raw_text.strip()
        if not chat_id or not text:
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if text.startswith(("/", ".")):
            with suppress(Exception):
                await self._api("deleteMessage", {"chat_id": chat_id, "message_id": int(message["message_id"])})
        if command == "/start":
            await self._send_dm_menu(chat_id)
            return
        if command in {"/chats", "💬 выбрать чат"}:
            await self._show_chats(chat_id)
            return
        if command in {"/deleted", "🗑 удалённые сообщения"}:
            await self._show_history(chat_id, "deleted")
            return
        if command in {"/edited", "✏️ изменённые сообщения"}:
            await self._show_history(chat_id, "edited")
            return
        if command in {"/help"}:
            await self._send_dm_help(chat_id)
            return
        if command in {"/cancel"}:
            await self._db(lambda conn: conn.execute("DELETE FROM secretary_sessions WHERE owner_id=?", (self.owner_id,)))
            await self._render_control_panel(chat_id, "Выбор чата сброшен.", [
                [{"text": "💬 Выбрать чат", "callback_data": "hist:chats"}],
                [{"text": "🏠 Меню", "callback_data": "hist:menu"}],
            ])
            return
        if not text.startswith("."):
            return
        target = await self._get_session(self.owner_id)
        if not target:
            await self._show_chats(chat_id)
            return
        await self._execute(raw_text, target["business_connection_id"], int(target["chat_id"]),
                            source=None, operator_chat_id=chat_id)

    async def _handle_business_message(self, message: dict[str, Any]):
        connection_id = str(message.get("business_connection_id") or "")
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        sender = message.get("from") or {}
        sender_id = int(sender.get("id") or 0)
        if not connection_id or not chat_id or chat.get("type") != "private":
            return
        # Even with Bot-to-Bot mode enabled, automated actions only react to
        # messages from human participants, never to another bot's output.
        if sender.get("is_bot"):
            return

        # Keep the latest message body so Telegram's deletion update (which
        # contains only message IDs) can be resolved to its original content.
        # Messages removed by .mute are marked so they do not trigger a reply.
        mute_key = (connection_id, chat_id)
        mute_expiry = self.active_mutes.get(mute_key)
        muted_contact_message = sender_id != self.owner_id and bool(mute_expiry and mute_expiry > time.time())
        await self._save_message_snapshot(message, ignore_delete=muted_contact_message)

        # Deletion is handled from the in-memory timer map so no Turso round-trip
        # sits in front of the fastest path.
        if sender_id != self.owner_id and mute_expiry:
            if mute_expiry > time.time():
                try:
                    await self._api("deleteBusinessMessages", {
                        "business_connection_id": connection_id, "message_ids": [int(message["message_id"])],
                    })
                except TelegramApiError as error:
                    logger.warning("Secretary mute delete failed chat_id=%s: %s", chat_id, error)
                return
            self.active_mutes.pop(mute_key, None)

        asyncio.create_task(self._remember_chat_safely(connection_id, chat, message.get("date")))
        if sender_id == self.owner_id and not sender.get("is_bot"):
            text = str(message.get("text") or "").strip()
            mode_command = text.lower()
            if mode_command in AI_MODE_COMMANDS:
                mode = AI_MODE_COMMANDS[mode_command]
                await self._set_ai_mode(connection_id, chat_id, mode)
                try:
                    await self._delete_message(connection_id, chat_id, int(message["message_id"]))
                except TelegramApiError as error:
                    logger.warning("Secretary AI mode command could not be removed chat_id=%s code=%s: %s",
                                   chat_id, error.code, error)
                logger.info("Secretary AI mode changed chat_id=%s mode=%s", chat_id, mode or "off")
                return
            if text.startswith("."):
                await self._execute(text, connection_id, chat_id, source=message)
            return
        if sender.get("is_bot"):
            return
        mode = await self._get_ai_mode(connection_id, chat_id)
        if mode:
            self._start_ai_reply(connection_id, chat_id, message, mode)

    async def _handle_group_message(self, message: dict[str, Any]):
        chat = message.get("chat") or {}
        chat_type = str(chat.get("type") or "")
        if chat_type not in {"group", "supergroup"}:
            return
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        chat_id = int(chat.get("id") or 0)
        sender_id = int(sender.get("id") or 0)
        if not chat_id:
            return

        mute_key = ("", chat_id)
        mute_expiry = self.active_mutes.get(mute_key)
        if sender_id and sender_id != self.owner_id and mute_expiry:
            if mute_expiry > time.time():
                try:
                    await self._api("deleteMessage", {
                        "chat_id": chat_id, "message_id": int(message["message_id"]),
                    })
                except TelegramApiError as error:
                    logger.warning("Secretary group mute delete failed chat_id=%s: %s", chat_id, error)
                return
            self.active_mutes.pop(mute_key, None)

        asyncio.create_task(self._remember_chat_safely("", chat, message.get("date")))
        if sender_id != self.owner_id:
            return
        text = str(message.get("text") or "")
        if text.startswith("."):
            await self._execute(text, "", chat_id, source=message)

    @staticmethod
    def _with_business(connection_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"business_connection_id": connection_id, **params} if connection_id else params

    async def _delete_message(self, connection_id: str, chat_id: int, message_id: int):
        if connection_id:
            await self._api("deleteBusinessMessages", {
                "business_connection_id": connection_id, "message_ids": [int(message_id)],
            })
        else:
            await self._api("deleteMessage", {"chat_id": int(chat_id), "message_id": int(message_id)})

    async def _send_message(self, connection_id: str, chat_id: int, text: str) -> int:
        sent = await self._api("sendMessage", self._with_business(connection_id, {
            "chat_id": int(chat_id), "text": text,
        }))
        return int(sent["message_id"])

    async def _edit_message(self, connection_id: str, chat_id: int, message_id: int, text: str):
        await self._api("editMessageText", self._with_business(connection_id, {
            "chat_id": int(chat_id), "message_id": int(message_id), "text": text,
        }))

    async def _handle_update(self, update: dict[str, Any]):
        if update.get("business_connection"):
            connection = update["business_connection"]
            connection_id = str(connection.get("id") or "")
            rights = connection.get("rights") or {}
            logger.info(
                "Secretary business connection enabled=%s can_reply=%s can_delete_all_messages=%s",
                bool(connection.get("is_enabled")), bool(rights.get("can_reply")),
                bool(rights.get("can_delete_all_messages")),
            )
            if connection_id and not connection.get("is_enabled"):
                for key in [key for key in self.active_mutes if key[0] == connection_id]:
                    self.active_mutes.pop(key, None)
                await self._db(lambda conn: conn.execute(
                    "UPDATE secretary_jobs SET status='done' WHERE business_connection_id=? AND status='active'",
                    (connection_id,),
                ))
            return
        if update.get("business_message"):
            await self._handle_business_message(update["business_message"])
        elif update.get("edited_business_message"):
            await self._save_edited_message(int(update.get("update_id") or 0), update["edited_business_message"])
        elif update.get("deleted_business_messages"):
            deleted = update["deleted_business_messages"]
            connection_id = str(deleted.get("business_connection_id") or "")
            chat = deleted.get("chat") or {}
            chat_id = int(chat.get("id") or 0)
            public_lines = []
            for message_id in deleted.get("message_ids") or []:
                event = await self._consume_deleted_message(connection_id, chat, int(message_id))
                if not event:
                    continue
                old_message = json.loads(event.get("old_message_json") or "{}") or None
                removed_text = self._message_content(old_message).replace("\n", " ")
                public_lines.append(f"Зачем ты удалил сообщение «{removed_text[:220]}»?")
            if connection_id and chat_id and public_lines:
                notice = "\n".join(public_lines[:5])
                if len(public_lines) > 5:
                    notice += f"\n…и ещё {len(public_lines) - 5} удалённых сообщений."
                try:
                    await self._send_message(connection_id, chat_id, notice[:4000])
                except Exception:
                    logger.exception("Secretary could not post deletion notice in chat_id=%s", chat_id)
        elif update.get("message"):
            message = update["message"]
            if (message.get("chat") or {}).get("type") in {"group", "supergroup"}:
                await self._handle_group_message(message)
            else:
                await self._handle_dm(message)
        elif update.get("callback_query"):
            await self._callback(update)

    @staticmethod
    def _duration(raw: str) -> int | None:
        match = DURATION_RE.fullmatch(raw.strip().lower())
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2) or "s"
        factor = 60 if unit in {"m", "min", "мин"} else 3600 if unit in {"h", "час"} else 86400 if unit in {"d", "д"} else 1
        return amount * factor if amount > 0 else None

    async def _edit_or_send(self, connection_id: str, chat_id: int, source: dict[str, Any] | None,
                            text: str, *, delete_source: bool = False) -> int | None:
        if source:
            message_id = int(source["message_id"])
            if delete_source:
                await self._delete_message(connection_id, chat_id, message_id)
                return None
            if connection_id:
                try:
                    await self._edit_message(connection_id, chat_id, message_id, text)
                    return message_id
                except TelegramApiError as error:
                    if error.code != 400 or "message_id_invalid" not in str(error).lower():
                        raise
                    # A duplicate/stale update can arrive after another
                    # polling process already edited or removed this command.
                    # Preserve the requested action by sending its replacement.
                    logger.warning("Secretary source message is no longer editable; sending replacement chat_id=%s message_id=%s",
                                   chat_id, message_id)
                    try:
                        await self._delete_message(connection_id, chat_id, message_id)
                    except TelegramApiError as delete_error:
                        if delete_error.code != 400 or "message_id_invalid" not in str(delete_error).lower():
                            raise
                    return await self._send_message(connection_id, chat_id, text)
            # Group messages belong to their sender: delete the command and
            # send the replacement from the secretary bot itself.
            await self._delete_message("", chat_id, message_id)
        return await self._send_message(connection_id, chat_id, text)

    async def _execute(self, command_text: str, connection_id: str, chat_id: int, *,
                       source: dict[str, Any] | None, operator_chat_id: int | None = None):
        match = COMMAND_RE.match(command_text)
        if not match:
            return
        command = match.group(1).lower()
        args = (match.group(2) or "").strip()
        if command == "type":
            type_match = re.match(r"^\.type(?:\s([\s\S]*))?$", command_text, re.IGNORECASE)
            args = (type_match.group(1) or "") if type_match else args
        now = time.time()
        mute_key = (connection_id, chat_id)

        async def feedback(text: str):
            if source:
                await self._edit_or_send(connection_id, chat_id, source, text)
            elif operator_chat_id:
                await self._command_feedback(operator_chat_id, chat_id, text)

        try:
            if command == "mute":
                expires = now + 60
                self.active_mutes[mute_key] = expires
                if source:
                    await self._edit_or_send(connection_id, chat_id, source, "Тише зайка")
                await self._create_job("mute", connection_id, chat_id, next_at=expires, expires_at=expires)
                if operator_chat_id:
                    await feedback("Режим mute включён на 1 минуту. Собеседнику ничего не отправлено.")
                return

            if command == "unmute":
                self.active_mutes.pop(mute_key, None)
                if source:
                    await self._edit_or_send(connection_id, chat_id, source, "Можно говорить 🙂")
                await self._db(lambda conn: conn.execute(
                    "UPDATE secretary_jobs SET status='done' WHERE kind='mute' AND business_connection_id=? AND chat_id=? AND status='active'",
                    (connection_id, chat_id),
                ))
                if operator_chat_id:
                    await feedback("Режим mute выключен. Собеседнику ничего не отправлено.")
                return

            if command == "typing":
                if args.lower() in {"stop", "off", "0"}:
                    if source:
                        await self._edit_or_send(connection_id, chat_id, source, "Печать остановлена")
                    await self._db(lambda conn: conn.execute(
                        "UPDATE secretary_jobs SET status='done' WHERE kind='typing' AND business_connection_id=? AND chat_id=? AND status='active'",
                        (connection_id, chat_id),
                    ))
                    if operator_chat_id:
                        await feedback("Индикатор печати остановлен. Собеседнику текст не отправлен.")
                    return
                duration = self._duration(args) if args else None
                if not duration:
                    await feedback("Формат: .typing 30min")
                    return
                expires = now + duration
                if source:
                    await self._edit_or_send(connection_id, chat_id, source, "ща чо расскажу вам")
                await self._api("sendChatAction", self._with_business(connection_id, {
                    "chat_id": chat_id, "action": "typing",
                }))
                await self._create_job("typing", connection_id, chat_id, next_at=now + 4, expires_at=expires)
                if operator_chat_id:
                    await feedback(f"Индикатор печати включён на {args}. Собеседнику текст не отправлен.")
                return

            if command in {"type", "del", "flip", "tiny"} and not args:
                await feedback(f"Формат: .{command} текст")
                return

            if command == "type":
                text = args
                first = text[:1]
                message_id = await self._edit_or_send(connection_id, chat_id, source, first)
                if len(first) < len(text) and message_id:
                    await self._create_job("type", connection_id, chat_id, message_id=message_id,
                        payload={"text": text, "cursor": len(first), "segment_start": 0},
                        next_at=now + TYPE_CHARACTER_INTERVAL)
                if operator_chat_id:
                    await feedback("Текст передан в выбранный чат.")
                return

            if command == "del":
                delete_match = re.match(r"^(?:(\d+)\s+)?([\s\S]+)$", args)
                seconds = int(delete_match.group(1) or 100) if delete_match else 100
                text = (delete_match.group(2) if delete_match else args).strip()[:4096]
                if not text:
                    await feedback("Формат: .del 100 текст")
                    return
                message_id = await self._edit_or_send(connection_id, chat_id, source, text)
                if message_id:
                    await self._create_job("delete", connection_id, chat_id, message_id=message_id,
                        next_at=now + max(1, seconds), expires_at=now + max(1, seconds))
                return

            if command == "spam":
                spam_match = re.match(r"^(\d+)\s+([\s\S]+)$", args)
                if not spam_match:
                    await feedback("Формат: .spam 5 текст")
                    return
                count, text = int(spam_match.group(1)), spam_match.group(2).strip()[:4096]
                if count < 1 or not text:
                    await feedback("Формат: .spam 5 текст")
                    return
                if source:
                    await self._edit_or_send(connection_id, chat_id, source, "", delete_source=True)
                await self._create_job("spam", connection_id, chat_id,
                    payload={"text": text, "remaining": count}, next_at=now)
                if operator_chat_id:
                    await feedback(f"Запущена отправка: {count} раз(а).")
                return

            if command == "hack":
                stages = ["Подключаюсь к серверу…", "Проверяю уровень доступа…", "Загружаю мемы…"]
                message_id = await self._edit_or_send(connection_id, chat_id, source, stages[0])
                if message_id:
                    await self._create_job("hack", connection_id, chat_id, message_id=message_id,
                        payload={"stages": stages, "index": 1}, next_at=now + 0.45)
                if operator_chat_id:
                    await feedback("Сценка запущена в выбранном чате.")
                return

            if command == "flip":
                result = args.translate(FLIP_MAP)[::-1]
                await self._edit_or_send(connection_id, chat_id, source, result[:4096])
                if operator_chat_id:
                    await feedback("Преобразованный текст отправлен.")
                return

            if command == "tiny":
                tiny_text = args.translate(TINY_CYRILLIC_MAP).translate(TINY_MAP)
                await self._edit_or_send(connection_id, chat_id, source, tiny_text[:4096])
                if operator_chat_id:
                    await feedback("Преобразованный текст отправлен.")
                return
        except TelegramApiError as error:
            logger.warning("Secretary action failed command=%s chat_id=%s code=%s: %s", command, chat_id, error.code, error)
            if operator_chat_id:
                with suppress(Exception):
                    await feedback(f"Не получилось выполнить .{command}: {error}")
            if source and error.code in {400, 403}:
                # Do not expose technical details in the target chat; the failed
                # API request itself leaves the original command available.
                pass
        except Exception:
            logger.exception("Secretary action failed command=%s chat_id=%s", command, chat_id)
            if operator_chat_id:
                with suppress(Exception):
                    await feedback(f"Ошибка при выполнении .{command}; подробности сохранены в логах.")

    async def _run_job(self, job: dict[str, Any]):
        job_id = job["id"]
        kind = job["kind"]
        connection_id = job["business_connection_id"]
        chat_id = int(job["chat_id"])
        message_id = int(job["message_id"] or 0)
        try:
            if kind == "mute":
                key = (connection_id, chat_id)
                if float(job.get("expires_at") or 0) <= time.time():
                    self.active_mutes.pop(key, None)
                    await self._finish_job(job_id)
                else:
                    await self._update_job(job_id, next_at=float(job["expires_at"]))
                return

            if kind == "typing":
                expires = float(job.get("expires_at") or 0)
                if expires <= time.time():
                    await self._finish_job(job_id)
                    return
                await self._api("sendChatAction", self._with_business(connection_id, {
                    "chat_id": chat_id, "action": "typing",
                }))
                await self._update_job(job_id, next_at=min(time.time() + 4, expires))
                return

            if kind == "delete":
                if float(job.get("expires_at") or 0) <= time.time():
                    await self._delete_message(connection_id, chat_id, message_id)
                    await self._finish_job(job_id)
                return

            payload = json.loads(job.get("payload") or "{}")
            if kind == "spam":
                remaining = int(payload.get("remaining") or 0)
                if remaining <= 0:
                    await self._finish_job(job_id)
                    return
                await self._send_message(connection_id, chat_id, str(payload.get("text") or "")[:4096])
                remaining -= 1
                if remaining <= 0:
                    await self._finish_job(job_id)
                else:
                    payload["remaining"] = remaining
                    await self._update_job(job_id, payload=payload, next_at=time.time() + 1.05)
                return

            if kind == "type":
                text = str(payload.get("text") or "")
                cursor = int(payload.get("cursor") or 0)
                if cursor >= len(text):
                    await self._finish_job(job_id)
                    return

                segment_start = int(payload.get("segment_start") or 0)
                current_segment = text[segment_start:cursor]
                next_character = text[cursor]
                if _telegram_text_units(current_segment) + _telegram_text_units(next_character) > TELEGRAM_TEXT_LIMIT:
                    # Telegram limits each message to 4096 characters. Continue
                    # longer input in another message, still one character at a time.
                    message_id = await self._send_message(connection_id, chat_id, next_character)
                    segment_start = cursor
                else:
                    try:
                        await self._edit_message(
                            connection_id, chat_id, message_id, text[segment_start:cursor + 1]
                        )
                    except TelegramApiError as error:
                        # If Telegram applied an edit but its response was lost,
                        # retrying the same step returns this 400. Treat it as an
                        # acknowledgement so the persisted cursor can move forward.
                        if error.code != 400 or "message is not modified" not in str(error).lower():
                            raise
                cursor += 1
                if cursor >= len(text):
                    await self._finish_job(job_id)
                else:
                    payload["cursor"] = cursor
                    payload["segment_start"] = segment_start
                    payload.pop("retry_attempts", None)
                    await self._update_job(job_id, payload=payload,
                                           next_at=time.time() + TYPE_CHARACTER_INTERVAL,
                                           message_id=message_id)
                return

            if kind == "hack":
                stages = payload.get("stages") or []
                index = int(payload.get("index") or 0)
                if index >= len(stages):
                    await self._edit_message(connection_id, chat_id, message_id,
                                             "Взлом завершён: доступ к мемам получен 😎")
                    await self._finish_job(job_id)
                else:
                    await self._edit_message(connection_id, chat_id, message_id, str(stages[index]))
                    payload["index"] = index + 1
                    await self._update_job(job_id, payload=payload, next_at=time.time() + 0.45)
        except TelegramApiError as error:
            if error.retry_after:
                await self._update_job(job_id, next_at=time.time() + int(error.retry_after) + 1)
                return
            if kind == "type" and isinstance(error.code, int) and error.code >= 500:
                attempts = int(payload.get("retry_attempts") or 0) + 1
                payload["retry_attempts"] = attempts
                await self._update_job(job_id, payload=payload,
                                       next_at=time.time() + min(30, 2 ** min(attempts, 5)))
                logger.warning("Retrying .type after Telegram server error chat_id=%s code=%s attempt=%s",
                               chat_id, error.code, attempts)
                return
            logger.warning("Secretary scheduled action failed kind=%s chat_id=%s: %s", kind, chat_id, error)
            if kind == "mute":
                self.active_mutes.pop((connection_id, chat_id), None)
            await self._finish_job(job_id)
        except Exception:
            logger.exception("Secretary scheduled action failed kind=%s chat_id=%s", kind, chat_id)
            await self._update_job(job_id, next_at=time.time() + 10)

    async def _scheduler(self):
        while True:
            try:
                self.scheduler_wakeup.clear()
                next_due = await self._db(lambda conn: (conn.execute(
                    "SELECT MIN(next_at) AS due FROM secretary_jobs WHERE status='active'"
                ).fetchone() or {"due": None})["due"])
                delay = 30 if next_due is None else max(0, min(30, float(next_due) - time.time()))
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.scheduler_wakeup.wait(), timeout=delay)
                        continue
                    except asyncio.TimeoutError:
                        pass
                due = await self._db(lambda conn: [
                    _row_to_dict(row) for row in conn.execute(
                        "SELECT * FROM secretary_jobs WHERE status='active' AND next_at<=? ORDER BY next_at LIMIT 20",
                        (time.time(),),
                    ).fetchall()
                ])
                for job in due:
                    await self._run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Secretary scheduler iteration failed")
            # Timed actions schedule their own next_at. A fixed one-second sleep
            # here made every .type character wait an extra second.

    async def _poll(self, offset: int | None):
        info = await self._api("getWebhookInfo")
        if info and info.get("url"):
            raise RuntimeError("Secretary bot has a webhook configured; remove it in Telegram before enabling polling.")
        for method, data in [
            ("setMyCommands", {"commands": [
                {"command": "start", "description": "Открыть секретаря"},
                {"command": "chats", "description": "Выбрать чат"},
                {"command": "deleted", "description": "Архив удалённых по авторам"},
                {"command": "edited", "description": "Архив изменений по авторам"},
                {"command": "help", "description": "Помощь"},
                {"command": "cancel", "description": "Сбросить выбранный чат"},
            ]}),
            ("setMyDescription", {"description": "Личный секретарь для быстрых команд в выбранных Telegram-чатах."}),
            ("setMyShortDescription", {"short_description": "Личный Telegram-секретарь"}),
        ]:
            try:
                await self._api(method, data)
            except TelegramApiError as error:
                logger.warning("Secretary bot profile setup failed method=%s: %s", method, error)
        if self.scheduler_task is None or self.scheduler_task.done():
            self.scheduler_task = asyncio.create_task(self._scheduler(), name="secretary-scheduler")
        while True:
            try:
                params: dict[str, Any] = {"timeout": 25, "allowed_updates": [
                    "message", "callback_query", "business_connection", "business_message", "edited_business_message",
                    "deleted_business_messages",
                ]}
                if offset is not None:
                    params["offset"] = offset
                updates = await self._api("getUpdates", params, timeout=40)
                for update in updates or []:
                    update_id = int(update.get("update_id", 0))
                    try:
                        await self._handle_update(update)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Secretary update handler failed update_id=%s", update_id)
                    await self._save_offset(update_id)
                    offset = update_id + 1
            except asyncio.CancelledError:
                raise
            except TelegramApiError as error:
                if error.retry_after:
                    await asyncio.sleep(int(error.retry_after) + 1)
                elif error.code == 409:
                    logger.error(
                        "Secretary polling stopped: another process is using getUpdates for this bot token; "
                        "keep one Render instance and stop any local/duplicate deployment"
                    )
                    raise SecretaryPollingConflict from error
                else:
                    logger.error("Secretary polling failed code=%s: %s", error.code, error)
                    await asyncio.sleep(5)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning("Secretary polling network error; retrying")
                await asyncio.sleep(3)
            except Exception:
                logger.exception("Secretary polling loop failed")
                await asyncio.sleep(5)

    async def run(self):
        if not self.token:
            return
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
            headers={"User-Agent": "UtilBot-Secretary/1.0"},
        )
        try:
            _, offset = await self._load_startup_state()
            while True:
                try:
                    me = await self._api("getMe")
                    logger.info("Secretary bot started username=@%s owner_id=%s", me.get("username"), self.owner_id)
                    await self._poll(offset)
                except asyncio.CancelledError:
                    raise
                except SecretaryPollingConflict:
                    # Retrying would make duplicate pollers alternately evict
                    # each other and cause repeated command failures.
                    return
                except RuntimeError as error:
                    if "webhook configured" in str(error):
                        logger.error("Secretary bot cannot start polling: %s", error)
                        return
                    logger.exception("Secretary bot startup failed; retrying")
                    await asyncio.sleep(10)
                except Exception:
                    logger.exception("Secretary bot polling stopped unexpectedly; retrying")
                    await asyncio.sleep(5)
        finally:
            if self.scheduler_task:
                self.scheduler_task.cancel()
                try:
                    await self.scheduler_task
                except asyncio.CancelledError:
                    pass
            await self.session.close()
            self.session = None
