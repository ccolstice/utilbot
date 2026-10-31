
import asyncio
import json
import re
import logging
import os
import sqlite3
import uuid
import time
import hashlib
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
    MenuButtonCommands,
    Message,
    ReplyParameters,
)
from dotenv import load_dotenv

load_dotenv()

from src.config import get_settings
from src.db.database import DATABASE_FILE, DATABASE_BACKEND, get_connection, init_db, restore_from_sqlite_snapshot
from src.keyboards.vault_keyboard import (
    all_items_keyboard,
    back_keyboard,
    folder_actions_keyboard,
    folder_items_keyboard,
    folders_keyboard,
    item_view_keyboard,
    item_actions_keyboard,
    main_menu_keyboard,
    more_menu_keyboard,
    state_items_keyboard,
    item_state_buttons,
    rss_menu_keyboard,
    search_results_keyboard,
)
from src.services.vault_service import VaultService
from src.handlers.reminders import router as reminders_router, show_reminders_page
from src.handlers.knowledge import router as knowledge_router, _show_favorites_page
from src.services.reminder_service import reminder_worker
from src.services.backup_service import BackupService, backup_worker
from src.services.google_drive_service import GoogleDriveService
from src.services.health_server import HealthServer
from src.services.index_service import IndexService
from src.services.media_service import MediaService
from src.services.chatgpt_ocr_service import ChatGptOcrService
from src.services.chatgpt_ai_service import ChatGptAiService
from src.services.groq_transcription_service import GroqTranscriptionService
from src.services.link_service import LinkService
from src.services.rss_service import RSSService
from src.states.save_state import SaveStates
from src.utils.search_parser import parse_search_request
from src.utils.text_cleaner import safe_excerpt, normalize_multiline_input
from src.utils.filters import WhitelistFilter, effective_allowed_user_ids, is_allowed_user, is_owner_user

settings = get_settings()
BOT_TOKEN = settings.bot_token
STARTED_AT = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if settings.enable_file_log:
    log_dir = os.path.join("logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "bot.log"), encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.message.filter(WhitelistFilter(settings))
dp.callback_query.filter(WhitelistFilter(settings))
dp.inline_query.filter(WhitelistFilter(settings))
dp.include_router(reminders_router)
dp.include_router(knowledge_router)
capture_router = Router()
capture_router.message.filter(WhitelistFilter(settings))
dp.include_router(capture_router)

pending_media: dict[int, Message] = {}
capture_locks: dict[int, asyncio.Lock] = {}
media_group_messages: dict[tuple[int, str], list[Message]] = {}
media_group_states: dict[tuple[int, str], FSMContext] = {}
media_group_tasks: dict[tuple[int, str], asyncio.Task] = {}
search_sessions: dict[str, dict[str, Any]] = {}
ai_processing_items: set[tuple[int, int]] = set()
health_server: HealthServer | None = None
backup_recipients: tuple[int, ...] = tuple(sorted(settings.owner_user_ids or settings.allowed_user_ids))

SAVE_WORDS = ["сохрани", "save", "засейви", "сейв"]
SKIP_TAG_WORDS = {"пропустить", "skip", "нет", "-", "пусто", "none", "null"}

PAGE_SIZE = 10



class SearchStates(StatesGroup):
    waiting_for_query = State()


class ItemMoveStates(StatesGroup):
    waiting_for_folder = State()


class ItemTagStates(StatesGroup):
    waiting_for_tag = State()


class RSSStates(StatesGroup):
    waiting_for_url = State()

class FolderRenameStates(StatesGroup):
    waiting_for_new_name = State()


class FolderCreateStates(StatesGroup):
    waiting_for_name = State()


class ItemContextStates(StatesGroup):
    waiting_for_text = State()


class SaveWordFilter(Filter):
    async def __call__(self, message: Message, state: FSMContext) -> bool:
        if not message.text:
            return False
        text = message.text.casefold().strip(" \t\n\r.,!?:;")
        return text in SAVE_WORDS


def _is_owner(user_id: int | None) -> bool:
    return user_id is not None and is_owner_user(int(user_id), settings)


async def _cleanup_temporary_user_message(handler, event: Message, data: dict[str, Any]):
    """Remove commands and conversational input after their handler has used them.

    Saved content is handled by capture_incoming_message, which deletes the
    source only after its archive copy is safely stored. This middleware is
    limited to commands and active FSM input so a failed capture is preserved.
    """
    state = data.get("state")
    state_before = await state.get_state() if state is not None else None
    is_private_allowed = (
        isinstance(event, Message)
        and event.chat.type == "private"
        and event.from_user is not None
        and is_allowed_user(event.from_user.id, settings)
    )
    is_command = bool(event.text and event.text.lstrip().startswith("/"))

    result = await handler(event, data)

    if is_private_allowed and (is_command or state_before is not None):
        try:
            await event.delete()
        except Exception:
            logging.info(
                "Could not remove temporary user message %s",
                event.message_id,
                exc_info=True,
            )
    return result


dp.message.outer_middleware(_cleanup_temporary_user_message)


def _paginate(items, page: int, page_size: int = PAGE_SIZE):
    total_pages = max(1, ceil(len(items) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total_pages, page, start


def _clean_preview(value: str | None, limit: int) -> str:
    if not value:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_dt(value) -> str:
    if not value:
        return "—"
    try:
        return str(value)[:16]
    except Exception:
        return "—"


def _format_seconds(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _preview_row(row) -> str:
    kind = row["kind"]
    if kind == "photo":
        if row["tag"]:
            return f"🖼 {_clean_preview(row['tag'], 35)}"
        if row["content"]:
            return f"🖼 {_clean_preview(row['content'], 35)}"
        return "🖼 Фото"

    if kind in {"voice", "audio"}:
        label = row["tag"] or row["content"] or ("ГС" if kind == "voice" else "Аудио")
        duration = _format_seconds((row["voice_duration"] if "voice_duration" in row.keys() else None))
        icon = "🎤" if kind == "voice" else "🎵"
        if duration:
            return f"{icon} {duration} · {_clean_preview(label, 30)}"
        return f"{icon} {_clean_preview(label, 35)}"

    if kind == "video_note":
        duration = _format_seconds((row["voice_duration"] if "voice_duration" in row.keys() else None))
        label = row["tag"] or row["content"] or "Кружок"
        if duration:
            return f"🟣 {duration} · {_clean_preview(label, 30)}"
        return f"🟣 {_clean_preview(label, 35)}"

    if kind == "video":
        duration = _format_seconds((row["voice_duration"] if "voice_duration" in row.keys() else None))
        label = row["tag"] or row["content"] or "Видео"
        if duration:
            return f"🎬 {duration} · {_clean_preview(label, 30)}"
        return f"🎬 {_clean_preview(label, 35)}"

    if kind == "animation":
        label = row["tag"] or row["content"] or "GIF"
        return f"✨ {_clean_preview(label, 35)}"

    if kind == "document":
        label = row["tag"] or row["content"] or "Документ"
        return f"📎 {_clean_preview(label, 35)}"

    if kind == "sticker":
        label = row["tag"] or row["content"] or "Стикер"
        return f"🔖 {_clean_preview(label, 35)}"

    if row["tag"] and row["content"]:
        return f"📝 {_clean_preview(row['tag'], 18)} · {_clean_preview(row['content'], 40)}"
    if row["tag"]:
        return f"📝 {_clean_preview(row['tag'], 40)}"
    if row["content"]:
        return f"📝 {_clean_preview(row['content'], 45)}"
    return "📝 Текст"


def _attribution_label(row) -> str:
    sender = row["sender_name"] or "Unknown"
    return f"↪️ Переслано от {sender}" if row["is_forwarded"] else f"👤 {sender}"



async def _send_text(target, text: str, reply_markup=None):
    await render_menu_panel(target, text, reply_markup=reply_markup)
    if isinstance(target, CallbackQuery):
        await target.answer()


async def _edit_text_or_send(target, text: str, reply_markup=None):
    if isinstance(target, CallbackQuery):
        await render_menu_panel(target, text, reply_markup)
        await target.answer()
    else:
        await render_menu_panel(target, text, reply_markup=reply_markup)


async def _clear_item_preview(user_id: int) -> None:
    preview_id = _get_meta_value(f"item_preview_message_id:{user_id}")
    if preview_id:
        try:
            await bot.delete_message(user_id, int(preview_id))
        except Exception as exc:
            logging.debug("Previous item preview %s was already unavailable", preview_id)
            if "message to delete not found" not in str(exc).lower():
                return
        _set_meta_value(f"item_preview_message_id:{user_id}", "")


async def _clear_current_archive_keyboard(user_id: int) -> None:
    """Deactivate the legacy keyboard on the currently viewed archive copy."""
    raw = _get_meta_value(f"item_view_state:{user_id}")
    try:
        view = json.loads(raw) if raw else None
        item_id = int(view["item_id"]) if view and view.get("item_id") else None
    except (TypeError, ValueError, json.JSONDecodeError):
        item_id = None
    if not item_id:
        return
    cleanup_key = f"archive_keyboard_cleared:{user_id}:{item_id}"
    if _get_meta_value(cleanup_key):
        return
    item = VaultService.get_item(user_id, item_id)
    if not item or not item["archive_message_id"]:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=int(item["archive_chat_id"] or user_id),
            message_id=int(item["archive_message_id"]),
            reply_markup=None,
        )
    except Exception:
        logging.debug("Could not deactivate legacy keyboard for item %s", item_id, exc_info=True)
    finally:
        _set_meta_value(cleanup_key, "1")


async def _send_item_preview(user_id: int, row) -> None:
    """Reuse a single preview message; the archived copy remains the canonical file."""
    file_id = row["telegram_file_id"]
    if not file_id:
        return
    kind = row["kind"]
    preview_key = f"item_preview_message_id:{user_id}"
    previous_id = _get_meta_value(preview_key)
    media_types = {
        "photo": InputMediaPhoto,
        "video": InputMediaVideo,
        "animation": InputMediaAnimation,
        "document": InputMediaDocument,
        "audio": InputMediaAudio,
    }
    if previous_id:
        media_type = media_types.get(kind)
        if media_type:
            try:
                await bot.edit_message_media(
                    chat_id=user_id,
                    message_id=int(previous_id),
                    media=media_type(media=file_id),
                )
                return
            except Exception:
                logging.debug("Could not reuse media preview %s; trying to replace it", previous_id, exc_info=True)
        try:
            await bot.delete_message(user_id, int(previous_id))
        except Exception as exc:
            if "message to delete not found" not in str(exc).lower():
                logging.warning("Keeping existing preview %s to avoid creating another media copy", previous_id)
                return
        _set_meta_value(preview_key, "")

    try:
        if kind == "photo":
            sent = await bot.send_photo(user_id, file_id)
        elif kind == "video":
            sent = await bot.send_video(user_id, file_id)
        elif kind == "animation":
            sent = await bot.send_animation(user_id, file_id)
        elif kind == "document":
            sent = await bot.send_document(user_id, file_id)
        elif kind == "voice":
            sent = await bot.send_voice(user_id, file_id)
        elif kind == "audio":
            sent = await bot.send_audio(user_id, file_id)
        elif kind == "video_note":
            sent = await bot.send_video_note(user_id, file_id)
        elif kind == "sticker":
            sent = await bot.send_sticker(user_id, file_id)
        else:
            return
        _set_meta_value(preview_key, str(sent.message_id))
    except Exception:
        logging.exception("Could not show preview for item %s", row["id"])


async def render_menu_panel(target, text: str, reply_markup=None, *, keep_item_preview: bool = False):
    """Keep one live keyboard panel and put it below the message that triggered it."""
    user_id = target.from_user.id
    chat_id = target.message.chat.id if isinstance(target, CallbackQuery) else target.chat.id
    if not keep_item_preview:
        await _clear_item_preview(user_id)
    await _clear_current_archive_keyboard(user_id)

    panel_id = _menu_message_id(user_id)
    callback_message = target.message if isinstance(target, CallbackQuery) else None
    panel_is_callback_source = bool(
        callback_message and panel_id == callback_message.message_id
    )
    move_to_bottom = isinstance(target, Message) or (
        callback_message is not None and not panel_is_callback_source
    )
    if move_to_bottom:
        # Old archive cards and one-off prompts must not keep competing keyboards.
        if callback_message and callback_message.reply_markup:
            with suppress(Exception):
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=callback_message.message_id,
                    reply_markup=None,
                )
        if panel_id:
            try:
                await bot.delete_message(chat_id, panel_id)
            except Exception as exc:
                if "message to delete not found" not in str(exc).lower():
                    logging.warning("Could not remove previous menu panel %s", panel_id, exc_info=True)
            _set_meta_value(f"menu_message_id:{user_id}", "")
        panel_id = None

    if panel_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=panel_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return
            logging.debug("Saved menu panel %s cannot be edited; creating a replacement", panel_id)
            with suppress(Exception):
                await bot.delete_message(chat_id, panel_id)

    # Do not overwrite an arbitrary message that triggered a callback (for example,
    # the canonical archived copy of a user's saved photo).
    sent = await bot.send_message(
        chat_id,
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    _remember_menu_message(user_id, sent.message_id)


def _menu_message_id(user_id: int) -> int | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM bot_meta WHERE key = ?",
            (f"menu_message_id:{user_id}",),
        ).fetchone()
        return int(row["value"]) if row and row["value"] else None
    except (ValueError, sqlite3.Error):
        return None
    finally:
        conn.close()


def _remember_menu_message(user_id: int, message_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"menu_message_id:{user_id}", str(message_id)),
        )
        conn.commit()
    finally:
        conn.close()


async def _forget_menu_panel(user_id: int, chat_id: int) -> None:
    """Remove the previous navigation panel so /start can create one at the bottom of chat."""
    panel_id = _menu_message_id(user_id)
    if panel_id:
        try:
            await bot.delete_message(chat_id, panel_id)
        except Exception:
            logging.debug("Could not remove previous menu panel %s", panel_id)
    _set_meta_value(f"menu_message_id:{user_id}", "")


def _get_meta_value(key: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM bot_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def _set_meta_value(key: str, value: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bot_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


async def _save_text_payload(
    *,
    user_id: int,
    folder_id: int,
    text: str,
    tag: str | None = None,
    sender_name: str | None = None,
    sender_username: str | None = None,
    is_forwarded: bool = False,
    source_chat_title: str | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
    favorite: bool = False,
    archive_chat_id: int | None = None,
    archive_message_id: int | None = None,
    item_state: str = "saved",
    dedupe_key: str | None = None,
    link_url_hint: str | None = None,
) -> int:
    link = await LinkService.enrich_first_link(text)
    if not link and link_url_hint:
        link = await LinkService.enrich_first_link(link_url_hint)
    if dedupe_key is None:
        normalized = f"url:{link.url}" if link else f"text:{' '.join(text.casefold().split())}"
        dedupe_key = "text:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    item_id = VaultService.save_item(
        user_id=user_id,
        folder_id=folder_id,
        kind="link" if link else "text",
        content=text,
        tag=tag,
        manual_tag=tag,
        archive_chat_id=archive_chat_id,
        archive_message_id=archive_message_id,
        item_state=item_state,
        dedupe_key=dedupe_key,
        sender_name=sender_name,
        sender_username=sender_username,
        is_forwarded=is_forwarded,
        source_chat_title=source_chat_title,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        media_type="link" if link else "text",
        link_url=link.url if link else None,
        link_domain=link.domain if link else None,
        page_title=link.title if link else None,
        is_favorite=1 if favorite else 0,
        favorited_at=datetime.utcnow().replace(microsecond=0).isoformat(sep=" ") if favorite else None,
    )
    return item_id


def _message_attribution(message: Message) -> tuple[str | None, str | None, str | None]:
    """Return the original author for forwarded messages, otherwise the sender."""
    origin = getattr(message, "forward_origin", None)
    origin_type = getattr(origin, "type", None)

    if origin_type == "user":
        sender = getattr(origin, "sender_user", None)
        if sender:
            return sender.full_name, sender.username, None
    elif origin_type == "hidden_user":
        return getattr(origin, "sender_user_name", None), None, None
    elif origin_type == "chat":
        chat = getattr(origin, "sender_chat", None)
        if chat:
            return (
                getattr(origin, "author_signature", None) or chat.title,
                chat.username,
                chat.title,
            )
    elif origin_type == "channel":
        chat = getattr(origin, "chat", None)
        if chat:
            author = getattr(origin, "author_signature", None)
            return author or chat.title, chat.username, chat.title

    sender = message.from_user
    return (
        sender.full_name if sender else None,
        sender.username if sender else None,
        message.chat.title if message.chat else None,
    )


def _message_link_url(message: Message) -> str | None:
    entities = message.entities if message.text else message.caption_entities
    for entity in entities or []:
        entity_type = getattr(getattr(entity, "type", None), "value", getattr(entity, "type", None))
        if entity_type == "text_link" and getattr(entity, "url", None):
            return str(entity.url)
    return None


async def _archive_message(message: Message, destination_chat_id: int):
    """Keep Telegram's native forwarded-from label when the source is forwarded."""
    if getattr(message, "forward_origin", None):
        try:
            return await bot.forward_message(
                chat_id=destination_chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except TelegramBadRequest:
            # Protected or otherwise non-forwardable messages can still be copied;
            # the saved row keeps the sender attribution for the archive card.
            logging.warning(
                "Could not preserve native forward header for message %s; falling back to copy",
                message.message_id,
                exc_info=True,
            )
    return await bot.copy_message(
        chat_id=destination_chat_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )


def _save_media_payload(
    user_id: int,
    folder_id: int,
    message: Message,
    *,
    tag: str | None = None,
    archive_chat_id: int | None = None,
    archive_message_id: int | None = None,
    item_state: str = "saved",
    dedupe_key: str | None = None,
    payload_override=None,
    media_group_json: str | None = None,
    archive_message_ids: str | None = None,
):
    sender_name, sender_username, source_chat_title = _message_attribution(message)
    is_forwarded = bool(getattr(message, "forward_origin", None))
    source_chat_id = message.chat.id if message.chat else None
    source_message_id = message.message_id

    payload = payload_override or MediaService.from_message(message)
    if payload is None:
        return None
    if dedupe_key is None:
        dedupe_key = _capture_dedupe_key(message, payload)

    link = LinkService.first_link(payload.content) if payload.kind == "text" else None
    if not link and _message_link_url(message):
        link = LinkService.first_link(_message_link_url(message))
    is_link_item = payload.kind == "text" and link is not None
    kind = "link" if is_link_item else payload.kind
    media_type = "link" if is_link_item else payload.media_type

    return VaultService.save_item(
        user_id=user_id,
        folder_id=folder_id,
        kind=kind,
        content=payload.content,
        tag=tag,
        manual_tag=tag,
        telegram_file_id=payload.telegram_file_id,
        media_group_json=media_group_json,
        archive_chat_id=archive_chat_id,
        archive_message_id=archive_message_id,
        archive_message_ids=archive_message_ids,
        item_state=item_state,
        dedupe_key=dedupe_key,
        voice_duration=payload.duration,
        sender_name=sender_name,
        sender_username=sender_username,
        is_forwarded=is_forwarded,
        source_chat_title=source_chat_title,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        file_name=payload.file_name,
        file_size=payload.file_size,
        mime_type=payload.mime_type,
        width=payload.width,
        height=payload.height,
        duration=payload.duration,
        media_type=media_type,
        link_url=link.url if link else None,
        link_domain=link.domain if link else None,
    )


async def _begin_save_flow(
    target,
    state: FSMContext,
    *,
    payload_type: str,
    replied_message: Message | None = None,
    note_text: str | None = None,
):
    data = {
        "payload_type": payload_type,
    }
    if replied_message is not None:
        data["replied_message"] = replied_message
        pending_media[target.from_user.id if isinstance(target, Message) else target.from_user.id] = replied_message
    if note_text is not None:
        data["note_text"] = note_text

    await state.update_data(**data)
    await state.set_state(SaveStates.waiting_for_tag)
    await _send_text(target, "🏷 Введи тег или напиши 'пропустить'.")

def _all_folders_for_user(user_id: int):
    folders = VaultService.list_folders(user_id)
    if not folders:
        folders = [VaultService.get_or_create_default_folder(user_id)]
    return folders


def _find_folder_by_name(user_id: int, name: str):
    name = (name or "").strip().lower()
    for folder in VaultService.list_folders(user_id, include_archived=True):
        if folder["name"].lower() == name:
            return folder
    for folder in VaultService.list_folders(user_id, include_archived=True):
        if name in folder["name"].lower():
            return folder
    return None


def _find_item(user_id: int, item_id: int):
    return VaultService.get_item(user_id, item_id)


def _capture_dedupe_key(message: Message, payload) -> str:
    media_objects = [message.document, message.video, message.audio, message.voice,
                     message.video_note, message.animation, message.sticker]
    if message.photo:
        media_objects.append(message.photo[-1])
    for media in media_objects:
        unique_id = getattr(media, "file_unique_id", None) if media else None
        if unique_id:
            return f"file:{unique_id}"

    return _text_dedupe_key(payload.content or "")


def _text_dedupe_key(content: str) -> str:
    content = (content or "").strip()
    link = LinkService.first_link(content)
    normalized = f"url:{link.url}" if link else f"text:{' '.join(content.casefold().split())}"
    return "text:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _duplicate_item_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Открыть сохранённое", callback_data=f"duplicate_open_{item_id}")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="go_home")],
    ])


async def _show_duplicate_item(target, item, *, incoming_removed: bool = False) -> None:
    item_id = int(item["id"])
    note = "Повторное сообщение убрал из чата." if incoming_removed else "Повторное сообщение оставил в чате."
    await render_menu_panel(
        target,
        f"♻️ Похоже, это уже есть в архиве — новый экземпляр не добавил. {note}\n\n"
        f"Нашёл запись #{item_id}:\n{_preview_row(item)}",
        reply_markup=_duplicate_item_keyboard(item_id),
    )


def _parse_search_request(user_id: int, raw: str, *, deep: bool = False):
    parsed = parse_search_request(raw, deep_default=deep)

    folder_id = None
    if parsed.folder_name:
        folder = _find_folder_by_name(user_id, parsed.folder_name)
        if folder:
            folder_id = int(folder["id"])
        elif parsed.query:
            parsed.query = f"{parsed.query} {parsed.folder_name}".strip()
        else:
            parsed.query = parsed.folder_name

    return {
        "query": parsed.query,
        "folder_id": folder_id,
        "kind": parsed.kind,
        "deep": parsed.deep,
        "tags": parsed.tags,
        "domain": parsed.domain,
        "sender": parsed.sender,
        "before": parsed.before,
        "after": parsed.after,
        "today": parsed.today,
        "untagged": parsed.untagged,
    }


def _search_key(user_id: int, query: str, folder_id: int | None, kind: str | None, deep: bool):
    return f"{user_id}:{folder_id}:{kind}:{int(deep)}:{query.lower().strip()}"


def _store_search_session(user_id: int, query: str, folder_id: int | None, kind: str | None, deep: bool):
    sid = uuid.uuid4().hex[:10]
    search_sessions[sid] = {
        "user_id": user_id,
        "query": query,
        "folder_id": folder_id,
        "kind": kind,
        "deep": deep,
        "tags": None,
        "domain": None,
        "sender": None,
        "before": None,
        "after": None,
        "today": False,
        "untagged": False,
        "cache_key": _search_key(user_id, query, folder_id, kind, deep),
    }
    return sid


def _get_search_rows(session_id: str):
    session = search_sessions.get(session_id)
    if not session:
        return None, None

    rows = IndexService.search(
        user_id=session["user_id"],
        query=session["query"],
        folder_id=session["folder_id"],
        kind=session["kind"],
        deep=session.get("deep", False),
        tags=session.get("tags"),
        domain=session.get("domain"),
        sender=session.get("sender"),
        before=session.get("before"),
        after=session.get("after"),
        today=session.get("today", False),
        untagged=session.get("untagged", False),
        limit=200,
    )
    return session, rows


async def run_search_query(message: Message, raw_query: str, state: FSMContext):
    """Parse a user's search request, save its paging session and show page one."""
    user_id = message.from_user.id
    parsed = _parse_search_request(user_id, raw_query)
    sid = _store_search_session(
        user_id,
        parsed["query"],
        parsed["folder_id"],
        parsed["kind"],
        parsed["deep"],
    )
    search_sessions[sid].update({
        "tags": parsed.get("tags"),
        "domain": parsed.get("domain"),
        "sender": parsed.get("sender"),
        "before": parsed.get("before"),
        "after": parsed.get("after"),
        "today": parsed.get("today", False),
        "untagged": parsed.get("untagged", False),
    })
    await state.clear()
    await show_search_results_page(message, sid, page=1)


async def show_home(target):
    text = (
        "🏠 Твоё личное хранилище\n\n"
        "Отправь сюда текст, ссылку, фото или файл — я сохраню его во Входящие.\n\n"
        "Разбирай новые материалы, откладывай важное на потом и возвращайся к старым через поиск или случайную находку."
    )
    await render_menu_panel(target, text, reply_markup=main_menu_keyboard())
    if isinstance(target, CallbackQuery):
        await target.answer()


async def show_rss_page(target, user_id: int):
    feeds = RSSService.list_for_user(user_id)
    text = "📡 RSS и Atom-ленты\n\n"
    if feeds:
        text += f"Подписок: {len(feeds)}. Новые записи бот проверяет примерно раз в 15 минут."
    else:
        text += "Подписок пока нет. Добавь ленту кнопкой ниже."
    await render_menu_panel(target, text, rss_menu_keyboard(feeds))


async def show_state_items_page(target, user_id: int, item_state: str, page: int, *, edit: bool = False):
    rows = VaultService.list_items_by_state(user_id, item_state)
    page_rows, total_pages, page, start = _paginate(rows, page)
    title = "📥 Входящие" if item_state == "inbox" else "⏳ На потом"
    if not rows:
        text = f"{title}\n\nПока пусто. Просто отправь боту текст, ссылку, фото или файл — новый материал появится здесь."
    else:
        text = f"{title} · {len(rows)}\nСтраница {page}/{total_pages}\n\n"
        for index, row in enumerate(page_rows, start + 1):
            text += f"{index}. {_preview_row(row)} · { _format_dt(row['created_at']) }\n"
    markup = state_items_keyboard(page_rows, state=item_state, page=page, total_pages=total_pages)
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


async def show_next_inbox_item(target, user_id: int, *, skip_item_id: int | None = None):
    rows = VaultService.list_items_by_state(user_id, "inbox")
    if skip_item_id is not None:
        current_index = next((i for i, row in enumerate(rows) if int(row["id"]) == skip_item_id), None)
        if current_index is not None:
            rows = rows[current_index + 1:] + rows[:current_index]
        else:
            rows = [row for row in rows if int(row["id"]) != skip_item_id]

    if not rows and skip_item_id is not None and VaultService.count_items_by_state(user_id, "inbox"):
        await render_menu_panel(
            target,
            "⏭ Других входящих пока нет. Этот материал пропущен и остался во входящих.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Вернуться к разбору", callback_data="inbox_review_start")],
                [InlineKeyboardButton(text="📥 К списку входящих", callback_data="menu:inbox")],
            ]),
        )
        return

    if not rows:
        await render_menu_panel(
            target,
            "✅ Входящие разобраны. Больше новых материалов нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📥 К списку входящих", callback_data="menu:inbox")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="go_home")],
            ]),
        )
        return

    row = rows[0]
    await show_item_view(
        target,
        user_id,
        int(row["id"]),
        origin="review",
        page=1,
        answer_callback=False,
        show_media_preview=True,
    )


async def show_folders_page(target, user_id: int, page: int, *, edit: bool = False):
    folders = VaultService.list_folders(user_id)
    if not folders:
        markup = folders_keyboard([], page=1, total_pages=1, mode="browse")
        await render_menu_panel(
            target,
            "📁 Папок пока нет. Всё новое сначала сохраняется во Входящие. Создай папку, когда захочешь разложить материалы.",
            reply_markup=markup,
        )
        if isinstance(target, CallbackQuery):
            await target.answer()
        return

    page_folders, total_pages, page, _ = _paginate(folders, page)
    text = (
        "📁 Папки\n"
        f"Страница {page}/{total_pages}\n\n"
        "Выбери папку:"
    )
    markup = folders_keyboard(page_folders, page=page, total_pages=total_pages, mode="browse")
    await render_menu_panel(target, text, reply_markup=markup)
    if isinstance(target, CallbackQuery):
        await target.answer()


async def show_save_folders_page(target, user_id: int, page: int, *, edit: bool = False):
    folders = _all_folders_for_user(user_id)
    page_folders, total_pages, page, _ = _paginate(folders, page)
    text = (
        "📁 Выбери папку для сохранения\n"
        f"Страница {page}/{total_pages}"
    )
    markup = folders_keyboard(page_folders, page=page, total_pages=total_pages, mode="save")
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


async def show_move_folders_page(target, user_id: int, page: int, *, edit: bool = False):
    folders = _all_folders_for_user(user_id)
    page_folders, total_pages, page, _ = _paginate(folders, page)
    text = (
        "📁 Куда переместить item?\n"
        f"Страница {page}/{total_pages}"
    )
    markup = folders_keyboard(page_folders, page=page, total_pages=total_pages, mode="move")
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


async def show_folder_items_page(
    target,
    user_id: int,
    folder_id: int,
    folders_page: int,
    page: int,
    *,
    edit: bool = False,
):
    folder = VaultService.get_folder_by_id(user_id=user_id, folder_id=folder_id)
    if not folder:
        await _edit_text_or_send(target, "❌ Папка не найдена", reply_markup=back_keyboard(f"vault_folders_page_{folders_page}")) if edit else await _send_text(target, "❌ Папка не найдена", reply_markup=main_menu_keyboard())
        return

    rows = VaultService.list_items(user_id=user_id, folder_id=folder_id, include_archived=True)
    if not rows:
        text = f"{folder['emoji'] or '📁'} {folder['name']}\n\n📭 Папка пуста"
        markup = back_keyboard(f"vault_folders_page_{folders_page}")
        if edit:
            await _edit_text_or_send(target, text, reply_markup=markup)
        else:
            await _send_text(target, text, reply_markup=markup)
        return

    page_rows, total_pages, page, start = _paginate(rows, page)
    text = f"{folder['emoji'] or '📁'} {folder['name']}\nСтраница {page}/{total_pages}\n\n"

    visible_index = start + 1
    for row in page_rows:
        sender = _attribution_label(row)
        dt = _format_dt(row["created_at"])
        text += (
            f"#{visible_index} {_preview_row(row)}\n"
            f"{sender} • {dt}\n\n"
        )
        visible_index += 1

    markup = folder_items_keyboard(
        page_rows,
        folder_id=folder_id,
        folders_page=folders_page,
        page=page,
        total_pages=total_pages,
    )
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


async def show_all_files_page(target, user_id: int, page: int, *, edit: bool = False):
    rows = VaultService.list_items(user_id=user_id, include_archived=True)
    if not rows:
        if edit:
            await _edit_text_or_send(target, "🗂 Все файлы\n\n📭 Пока пусто", reply_markup=main_menu_keyboard())
        else:
            await _send_text(target, "🗂 Все файлы\n\n📭 Пока пусто", reply_markup=main_menu_keyboard())
        return

    folders = VaultService.list_folders(user_id, include_archived=True)
    folder_map = {folder["id"]: f"{folder['emoji'] or '📁'} {folder['name']}" for folder in folders}

    page_rows, total_pages, page, start = _paginate(rows, page)
    text = f"🗂 Все файлы\nСтраница {page}/{total_pages}\n\n"

    visible_index = start + 1
    for row in page_rows:
        sender = _attribution_label(row)
        dt = _format_dt(row["created_at"])
        folder_name = folder_map.get(row["folder_id"], "Без папки")
        text += (
            f"#{visible_index} {_preview_row(row)}\n"
            f"{folder_name} • {sender} • {dt}\n\n"
        )
        visible_index += 1

    markup = all_items_keyboard(page_rows, page=page, total_pages=total_pages)
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


async def show_search_results_page(target, session_id: str, page: int, *, edit: bool = False):
    session, rows = _get_search_rows(session_id)
    if not session:
        if edit:
            await _edit_text_or_send(target, "❌ Сессия поиска потеряна", reply_markup=main_menu_keyboard())
        else:
            await _send_text(target, "❌ Сессия поиска потеряна", reply_markup=main_menu_keyboard())
        return

    if not rows:
        query_label = f" по запросу «{session['query']}»" if session.get("query") else ""
        if edit:
            await _edit_text_or_send(target, f"🔎 Поиск\n\n📭 Ничего не найдено{query_label}", reply_markup=back_keyboard("go_home"))
        else:
            await _send_text(target, f"🔎 Поиск\n\n📭 Ничего не найдено{query_label}", reply_markup=main_menu_keyboard())
        return

    page_rows, total_pages, page, start = _paginate(rows, page)
    text = f"🔎 Поиск\nЗапрос: {session['query'] or '—'}\nСтраница {page}/{total_pages}\n\n"

    visible_index = start + 1
    folders = VaultService.list_folders(session["user_id"], include_archived=True)
    folder_map = {folder["id"]: f"{folder['emoji'] or '📁'} {folder['name']}" for folder in folders}

    for row in page_rows:
        sender = _attribution_label(row)
        dt = _format_dt(row["created_at"])
        folder_name = folder_map.get(row["folder_id"], "Без папки")
        text += (
            f"#{visible_index} {_preview_row(row)}\n"
            f"{folder_name} • {sender} • {dt}\n\n"
        )
        visible_index += 1

    markup = search_results_keyboard(page_rows, session_id=session_id, page=page, total_pages=total_pages)
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


def _item_image_entries(item) -> list[dict[str, Any]]:
    kind = str(item["kind"] or "")
    if kind == "photo":
        try:
            media_group = json.loads(item["media_group_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError, KeyError):
            media_group = []
        if isinstance(media_group, list):
            images = [
                {"index": index, **entry}
                for index, entry in enumerate(media_group)
                if isinstance(entry, dict) and entry.get("kind") == "photo" and entry.get("telegram_file_id")
            ]
            if images:
                return images
    if kind == "photo" or (
        kind == "document" and item["mime_type"] in {"image/jpeg", "image/png", "image/webp"}
    ):
        file_id = item["telegram_file_id"]
        return [{"index": 0, "kind": kind, "telegram_file_id": file_id}] if file_id else []
    return []


async def show_item_view(
    target,
    user_id: int,
    item_id: int,
    *,
    origin: str,
    folder_id: int | None = None,
    folders_page: int | None = None,
    page: int | None = None,
    session_id: str | None = None,
    answer_callback: bool = True,
    back_callback_override: str | None = None,
    show_media_preview: bool = True,
):
    row = _find_item(user_id, item_id)
    if not row:
        if isinstance(target, CallbackQuery):
            await target.answer("Материал уже удалён или недоступен", show_alert=True)
            return
        await _send_text(target, "❌ Item не найден", reply_markup=main_menu_keyboard())
        return

    folder = VaultService.get_folder_by_id(user_id=user_id, folder_id=row["folder_id"])
    folder_name = f"{folder['emoji'] or '📁'} {folder['name']}" if folder else "Без папки"
    attribution = _attribution_label(row)
    dt = _format_dt(row["created_at"])
    tag = row["tag"] or "—"

    if origin == "review":
        back_callback = "state_page_inbox_1"
        delete_callback = f"review_delete_{item_id}"
        move_callback = f"item_move_r_{item_id}"
        tag_callback = f"item_tag_a_1_{item_id}"
    elif origin == "folder":
        back_callback = f"vault_items_page_{folder_id}_{folders_page}_{page}"
        delete_callback = f"delete_item_f_{folder_id}_{folders_page}_{page}_{item_id}"
        move_callback = f"item_move_f_{folder_id}_{folders_page}_{page}_{item_id}"
        tag_callback = f"item_tag_f_{folder_id}_{folders_page}_{page}_{item_id}"
    elif origin == "search":
        back_callback = f"search_page_{session_id}_{page}"
        delete_callback = f"delete_item_s_{session_id}_{page}_{item_id}"
        move_callback = f"item_move_s_{session_id}_{page}_{item_id}"
        tag_callback = f"item_tag_s_{session_id}_{page}_{item_id}"
    elif origin == "state":
        state_name = folder_id
        back_callback = f"state_page_{state_name}_{page or 1}"
        delete_callback = f"delete_item_a_{page or 1}_{item_id}"
        move_callback = f"item_move_a_{page or 1}_{item_id}"
        tag_callback = f"item_tag_a_{page or 1}_{item_id}"
    else:
        back_callback = f"vault_all_page_{page}"
        delete_callback = f"delete_item_a_{page}_{item_id}"
        move_callback = f"item_move_a_{page}_{item_id}"
        tag_callback = f"item_tag_a_{page}_{item_id}"

    if back_callback_override:
        back_callback = back_callback_override

    remind_callback = f"item_remind_{item_id}"
    favorite_callback = f"fav_toggle_{item_id}"
    context_callback = f"item_context_{item_id}"
    view_state = {
        "item_id": item_id,
        "origin": origin,
        "show_media_preview": show_media_preview,
        "folder_id": folder_id,
        "folders_page": folders_page,
        "page": page,
        "session_id": session_id,
        "back_callback": back_callback,
        "delete_callback": delete_callback,
        "move_callback": move_callback,
        "tag_callback": tag_callback,
        "remind_callback": remind_callback,
        "context_callback": context_callback,
    }
    _set_meta_value(f"item_view_state:{user_id}", json.dumps(view_state))
    image_entries = _item_image_entries(row)
    markup = item_view_keyboard(
        back_callback,
        favorite_callback=favorite_callback,
        favorite=bool(row["is_favorite"]),
        actions_callback=f"item_more_{item_id}",
        delete_callback=delete_callback,
        review_next_callback=f"review_skip_{item_id}" if origin == "review" else None,
        review_move_callback=move_callback if origin == "review" else None,
        ocr_callback=(
            f"item_ocr_{item_id}"
            if image_entries
            and _is_owner(user_id) and settings.chatgpt_ocr_url and settings.chatgpt_ocr_token
            else None
        ),
        ai_callback=(
            f"item_ai_{item_id}"
            if _is_owner(user_id) and settings.chatgpt_ai_url and settings.chatgpt_ai_token
            and (row["kind"] != "voice" or settings.groq_api_key)
            and not row["ai_analysis_active"]
            else None
        ),
        ai_retry_callback=(
            f"item_ai_retry_{item_id}"
            if _is_owner(user_id) and settings.chatgpt_ai_url and settings.chatgpt_ai_token
            and (row["kind"] != "voice" or settings.groq_api_key)
            and row["ai_analysis_active"]
            else None
        ),
        ai_undo_callback=(
            f"item_ai_undo_{item_id}"
            if _is_owner(user_id) and settings.chatgpt_ai_url and settings.chatgpt_ai_token
            and row["ai_analysis_active"]
            else None
        ),
        ai_accept_callback=(f"item_ai_accept_{item_id}" if row["ai_pending_description"] and row["ai_pending_tags"] else None),
        ai_cancel_callback=(f"item_ai_cancel_{item_id}" if row["ai_pending_description"] and row["ai_pending_tags"] else None),
        ocr_accept_callback=(f"item_ocr_accept_{item_id}" if row["ocr_pending_text"] else None),
        ocr_cancel_callback=(f"item_ocr_cancel_{item_id}" if row["ocr_pending_text"] else None),
        ai_callback_label=(
            "🎙 Расшифровать и добавить теги" if row["kind"] == "voice"
            else "✨ Обработать через ChatGPT"
        ),
    )
    current_state = row["item_state"] if "item_state" in row.keys() else "saved"
    markup.inline_keyboard.insert(0, item_state_buttons(item_id, current_state))

    media_kinds = {"photo", "video_note", "video", "animation", "document", "sticker", "voice", "audio"}
    kind_labels = {
        "photo": "Фото", "video_note": "Кружок", "video": "Видео",
        "animation": "GIF", "document": "Файл", "sticker": "Стикер",
        "voice": "Голосовое", "audio": "Аудио", "text": "Текст",
    }
    content = row["content"] or ""
    is_forwarded = bool(row["is_forwarded"])
    if row["ai_description"] and not is_forwarded and row["kind"] != "voice":
        content = row["ai_description"]
    header = (
        f"{folder_name}\n{attribution}\n🕒 {dt}\n🏷 {tag}\n"
        f"#{item_id} · {kind_labels.get(row['kind'], row['kind'].title())}"
    )
    if origin == "review":
        header = "⚡ Разбор входящих\n\n" + header
    if row["context_text"]:
        header += "\n\n🗒 Контекст: " + safe_excerpt(row["context_text"], 700)
    if row["ai_description"] and is_forwarded:
        header += "\n\n✨ Кратко: " + safe_excerpt(row["ai_description"], 900)
    if row["kind"] == "voice" and row["transcript_text"]:
        header += "\n\n📝 Расшифровка:\n" + safe_excerpt(row["transcript_text"], 2200)
    if row["ocr_text"]:
        header += "\n\n🧾 Текст с изображений:\n" + safe_excerpt(row["ocr_text"], 2400)
    if row["ocr_pending_text"]:
        image_number = int(row["ocr_pending_image_index"] or 0) + 1
        header += f"\n\n🔎 Предложение OCR · фото {image_number} (ещё не сохранено):\n" + safe_excerpt(row["ocr_pending_text"], 1800)
    if row["link_url"] and row["link_url"] not in content:
        header += "\n\n🔗 " + str(row["link_url"])[:1400]
    if row["kind"] in media_kinds:
        if content:
            header += "\n\n" + safe_excerpt(content, 1200)
        if show_media_preview:
            await _send_item_preview(user_id, row)
            # A media preview is a separate chat message; replace the old panel
            # after it so the only live keyboard is always the bottom-most message.
            chat_id = target.message.chat.id if isinstance(target, CallbackQuery) else target.chat.id
            await _forget_menu_panel(user_id, chat_id)
        await render_menu_panel(
            target,
            header,
            markup,
            keep_item_preview=show_media_preview and row["kind"] in media_kinds,
        )
        if answer_callback and isinstance(target, CallbackQuery):
            await target.answer()
        return

    if len(content) > 2800:
        header += f"\n\nТекст длинный ({len(content)} символов).\n\n{safe_excerpt(content, 2800)}"
    else:
        header += "\n\n" + content
    await render_menu_panel(target, header, markup)
    if answer_callback and isinstance(target, CallbackQuery):
        await target.answer()
    return


def _image_picker_markup(item_id: int, images: list[dict[str, Any]], action: str) -> InlineKeyboardMarkup:
    rows = []
    pending_row = []
    for image_number, image in enumerate(images, start=1):
        pending_row.append(InlineKeyboardButton(
            text=f"🖼 Фото {image_number}",
            callback_data=f"item_{action}_image_{item_id}_{image['index']}",
        ))
        if len(pending_row) == 2:
            rows.append(pending_row)
            pending_row = []
    if pending_row:
        rows.append(pending_row)
    rows.append([InlineKeyboardButton(text="⬅️ К материалу", callback_data=f"item_return_{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_image_picker(callback: CallbackQuery, item_id: int, images: list[dict[str, Any]], action: str):
    verb = "расшифровать" if action == "ocr" else "передать ChatGPT"
    state = _saved_item_view_state(callback.from_user.id, item_id) or {}
    await render_menu_panel(
        callback,
        f"Выбери фото, которое нужно {verb} ({len(images)} в этом материале):",
        _image_picker_markup(item_id, images, action),
        keep_item_preview=bool(state.get("show_media_preview", True)),
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^item_ocr_\d+$"))
async def item_ocr_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _is_owner(user_id):
        await callback.answer("Доступно только владельцу бота", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Открой фото в личном чате с ботом", show_alert=True)
        return
    item_id = int(callback.data.rsplit("_", 1)[1])
    item = VaultService.get_item(user_id, item_id)
    images = _item_image_entries(item) if item else []
    if not item or not images:
        await callback.answer("Фото не найдено", show_alert=True)
        return
    if len(images) > 1:
        await _show_image_picker(callback, item_id, images, "ocr")
        return
    await _run_item_ocr(callback, item_id, images[0]["index"])


@dp.callback_query(F.data.regexp(r"^item_ocr_image_\d+_\d+$"))
async def item_ocr_image_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    await _run_item_ocr(callback, int(parts[3]), int(parts[4]))


async def _run_item_ocr(callback: CallbackQuery, item_id: int, image_index: int):
    user_id = callback.from_user.id
    if not _is_owner(user_id):
        await callback.answer("Доступно только владельцу бота", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Открой фото в личном чате с ботом", show_alert=True)
        return
    item = VaultService.get_item(user_id, item_id)
    images = _item_image_entries(item) if item else []
    selected = next((image for image in images if image["index"] == image_index), None)
    if not item or not selected:
        await callback.answer("Фото не найдено", show_alert=True)
        return
    if not settings.chatgpt_ocr_url or not settings.chatgpt_ocr_token:
        await callback.answer("OCR пока не настроен", show_alert=True)
        return
    await callback.answer("Распознаю текст…")
    view_state = _saved_item_view_state(user_id, item_id) or {}
    await render_menu_panel(
        callback,
        f"⏳ Распознаю фото {images.index(selected) + 1}…",
        keep_item_preview=bool(view_state.get("show_media_preview", True)),
    )
    try:
        text = await ChatGptOcrService.transcribe_file(bot, settings, user_id, selected["telegram_file_id"])
        if not VaultService.propose_item_ocr(user_id, item_id, text, image_index):
            raise RuntimeError("Материал уже удалён.")
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить текст к материалу", callback_data=f"item_ocr_accept_{item_id}")],
            [InlineKeyboardButton(text="✖️ Оставить без изменений", callback_data=f"item_ocr_cancel_{item_id}")],
        ])
        await render_menu_panel(
            callback,
            f"🔎 Распознанный текст · фото {images.index(selected) + 1}\n\n{safe_excerpt(text, 2800)}\n\nДобавить текст в карточку этого материала? Он сохранится вместе с фото и будет удалён вместе с ним.",
            markup,
            keep_item_preview=bool(view_state.get("show_media_preview", True)),
        )
    except Exception as exc:
        logging.warning("OCR failed for owner=%s item=%s photo=%s (%s)", user_id, item_id, image_index + 1, type(exc).__name__)
        await render_menu_panel(
            callback,
            f"❌ {str(exc).strip()[:900] or 'Не удалось распознать текст.'}",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ К материалу", callback_data=f"item_return_{item_id}")]]),
            keep_item_preview=bool(view_state.get("show_media_preview", True)),
        )


@dp.callback_query(F.data.regexp(r"^item_ocr_(?:accept|cancel)_\d+$"))
async def item_ocr_proposal_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _is_owner(user_id):
        await callback.answer("Доступно только владельцу бота", show_alert=True)
        return
    action, item_id_text = callback.data.rsplit("_", 1)
    item_id = int(item_id_text)
    if action == "item_ocr_accept":
        item = VaultService.accept_item_ocr(user_id, item_id)
        if not item:
            await callback.answer("Предложение OCR уже закрыто", show_alert=True)
            return
        await callback.answer("Текст добавлен в материал")
    else:
        item = VaultService.cancel_item_ocr(user_id, item_id)
        if not item:
            await callback.answer("Материал не найден", show_alert=True)
            return
        await callback.answer("Текст не сохранял")
    await _return_to_item_card(callback, user_id, item_id)


@dp.callback_query(F.data.regexp(r"^item_ai_(?:(?:(?:retry|undo)_)?\d+|image_\d+_\d+)$"))
async def item_ai_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _is_owner(user_id):
        await callback.answer("Доступно только владельцу бота", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Открой запись в личном чате с ботом", show_alert=True)
        return
    if not settings.chatgpt_ai_url or not settings.chatgpt_ai_token:
        await callback.answer("AI-обработка пока не настроена", show_alert=True)
        return

    if callback.data.startswith("item_ai_image_"):
        parts = callback.data.split("_")
        action = "process"
        item_id = int(parts[3])
        selected_image_index = int(parts[4])
    else:
        parts = callback.data.split("_")
        action = parts[2] if len(parts) == 4 else "process"
        item_id = int(parts[-1])
        selected_image_index = None
    key = (user_id, item_id)
    if action == "undo":
        if key in ai_processing_items:
            await callback.answer("Сначала дождись окончания обработки", show_alert=True)
            return
        restored = VaultService.undo_item_ai_metadata(user_id, item_id)
        if not restored:
            await callback.answer("Нет анализа, который можно отменить", show_alert=True)
            return
        await callback.answer("Анализ отменён")
        await _return_to_item_card(callback, user_id, item_id)
        return

    if key in ai_processing_items:
        await callback.answer("Эта запись уже обрабатывается")
        return
    item = VaultService.get_item(user_id, item_id)
    if not item:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    images = _item_image_entries(item) if item["kind"] == "photo" else []
    if action in {"process", "retry"} and selected_image_index is None and len(images) > 1:
        await _show_image_picker(callback, item_id, images, "ai")
        return
    if selected_image_index is not None and not any(image["index"] == selected_image_index for image in images):
        await callback.answer("Фото не найдено", show_alert=True)
        return
    if images and selected_image_index is None:
        selected_image_index = images[0]["index"]
    if item["kind"] == "voice" and not settings.groq_api_key:
        await callback.answer("Добавь GROQ_API_KEY в Environment UtilBot", show_alert=True)
        return

    ai_processing_items.add(key)
    await callback.answer("Распознаю голосовое…" if item["kind"] == "voice" else ("Отправляю запись в ChatGPT…" if action == "process" else "Повторно обрабатываю запись…"))
    progress_text = "⏳ Распознаю голосовое и подбираю теги…" if item["kind"] == "voice" else "⏳ Готовлю предложение по описанию и тегам…"
    if action == "retry" and item["ai_pending_description"]:
        progress = callback.message
        await progress.edit_text(progress_text)
    else:
        progress = await callback.message.answer(progress_text)
    try:
        transcript = None
        if item["kind"] == "voice":
            transcript = await GroqTranscriptionService.transcribe_item(bot, settings, item)
            result = await ChatGptAiService.process_voice_transcript(settings, user_id, item, transcript)
        else:
            result = await ChatGptAiService.process_item(
                bot, settings, user_id, item, image_index=selected_image_index,
            )
        updated = VaultService.propose_item_ai_metadata(
            user_id, item_id, result["description"], result["tags"], transcript=transcript,
        )
        if not updated:
            raise RuntimeError("Запись уже удалена.")
        description = safe_excerpt(result["description"], 2500)
        tags = result["tags"]
        if item["kind"] == "voice":
            note = "После принятия полная расшифровка сохранится в карточке, а оригинальное голосовое останется доступно для прослушивания."
        elif bool(item["is_forwarded"]):
            note = "Исходное пересланное сообщение, его ссылка и автор останутся без изменений; добавится только краткая пометка."
        else:
            note = "После принятия это описание заменит старый текст, а теги будут заменены новыми."
        if item["kind"] == "voice":
            preview = f"📝 Полная расшифровка голосового\n\n{result['description']}\n\n🏷 Новые теги: {', '.join(tags)}\n\n{note}"
        else:
            original = str(item["content"] or "").strip()
            if item["ai_analysis_active"] and item["ai_previous_content"] is not None:
                original = str(item["ai_previous_content"] or "").strip()
            before = f"Было:\n{safe_excerpt(original, 650)}\n\n" if original and not item["is_forwarded"] else ""
            preview = f"📝 Предложение ChatGPT\n\n{before}{description}\n\n🏷 Новые теги: {', '.join(tags)}\n\n{note}"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принять замену", callback_data=f"item_ai_accept_{item_id}")],
            [InlineKeyboardButton(text="🔁 Попробовать ещё", callback_data=f"item_ai_retry_{item_id}")],
            [InlineKeyboardButton(text="✖️ Оставить как есть", callback_data=f"item_ai_cancel_{item_id}")],
        ])
        if item["kind"] == "voice" and len(preview) > 3900:
            chunks = [preview[index:index + 3500] for index in range(0, len(preview), 3500)]
            await progress.edit_text(chunks[0])
            for chunk in chunks[1:-1]:
                await callback.message.answer(chunk)
            await callback.message.answer(chunks[-1], reply_markup=markup)
        else:
            await progress.edit_text(preview[:4000], reply_markup=markup)
    except Exception as exc:
        logging.warning("ChatGPT archive processing failed for owner=%s item=%s (%s)", user_id, item_id, type(exc).__name__)
        message = str(exc).strip() or "Не удалось обработать запись. Попробуй ещё раз."
        await progress.edit_text(f"❌ {message[:900]}")
    finally:
        ai_processing_items.discard(key)


@dp.callback_query(F.data.regexp(r"^item_ai_(?:accept|cancel)_\d+$"))
async def item_ai_proposal_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not _is_owner(user_id):
        await callback.answer("Доступно только владельцу бота", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Открой запись в личном чате с ботом", show_alert=True)
        return
    action, item_id_text = callback.data.rsplit("_", 1)
    item_id = int(item_id_text)
    if (user_id, item_id) in ai_processing_items:
        await callback.answer("Запись ещё обрабатывается", show_alert=True)
        return
    if action == "item_ai_accept":
        item = VaultService.accept_item_ai_proposal(user_id, item_id)
        if not item:
            await callback.answer("Предложение уже закрыто", show_alert=True)
            return
        await callback.answer("Замену применил")
        with suppress(Exception):
            await callback.message.edit_text("✅ Предложение применено. Карточка обновлена.")
    else:
        item = VaultService.cancel_item_ai_proposal(user_id, item_id)
        if not item:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        await callback.answer("Оставил исходный материал")
        with suppress(Exception):
            await callback.message.edit_text("✖️ Предложение отменено. Исходный материал оставлен без изменений.")
    await _return_to_item_card(callback, user_id, item_id)


def _saved_item_view_state(user_id: int, item_id: int) -> dict[str, Any] | None:
    raw = _get_meta_value(f"item_view_state:{user_id}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if data.get("item_id") == item_id else None


async def _return_to_item_card(target, user_id: int, item_id: int):
    data = _saved_item_view_state(user_id, item_id) or {"origin": "all", "page": 1}
    await show_item_view(
        target,
        user_id,
        item_id,
        origin=data.get("origin", "all"),
        folder_id=data.get("folder_id"),
        folders_page=data.get("folders_page"),
        page=data.get("page", 1),
        session_id=data.get("session_id"),
        show_media_preview=data.get("show_media_preview", True),
    )


def _action_preview_preference(callback: CallbackQuery, item_id: int) -> bool:
    """Keep the preview only when the action came from an already-open item card."""
    item = VaultService.get_item(callback.from_user.id, item_id)
    message = callback.message
    if (
        item
        and message
        and item["archive_message_id"]
        and int(item["archive_message_id"]) == message.message_id
        and int(item["archive_chat_id"] or message.chat.id) == message.chat.id
    ):
        # The canonical archived message already contains this media. Quick
        # actions on it must not generate a second photo just to show the card.
        return False
    view_state = _saved_item_view_state(callback.from_user.id, item_id)
    return bool(view_state.get("show_media_preview", True)) if view_state else True


@dp.callback_query(F.data.startswith("item_more_"))
async def item_more_actions_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    item = VaultService.get_item(callback.from_user.id, item_id)
    data = _saved_item_view_state(callback.from_user.id, item_id)
    if not item or not data:
        await callback.answer("Карточка устарела — открой материал заново", show_alert=True)
        return

    markup = item_actions_keyboard(
        move_callback=None if data.get("origin") == "review" else data["move_callback"],
        tag_callback=data["tag_callback"],
        remind_callback=data["remind_callback"],
        context_callback=data.get("context_callback"),
        share_query=f"item:{item_id}",
        card_callback=f"item_return_{item_id}",
        has_context=bool(item["context_text"]),
    )
    show_media_preview = bool(data.get("show_media_preview", True))
    await render_menu_panel(
        callback,
        f"⚙️ Действия с материалом #{item_id}\n\n{_preview_row(item)}",
        reply_markup=markup,
        keep_item_preview=show_media_preview and item["kind"] in {"photo", "video_note", "video", "animation", "document", "sticker", "voice", "audio"},
    )
    if show_media_preview and item["kind"] in {"photo", "video_note", "video", "animation", "document", "sticker", "voice", "audio"}:
        await _send_item_preview(callback.from_user.id, item)
    await callback.answer()


@dp.callback_query(F.data.startswith("item_return_"))
async def item_return_to_card_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    await _return_to_item_card(callback, callback.from_user.id, item_id)
    await callback.answer()



@dp.callback_query(F.data.startswith("menu:"))
async def root_menu_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await state.clear()

    if action == "vault":
        await show_folders_page(callback, user_id, page=1, edit=True)
    elif action == "all":
        await show_all_files_page(callback, user_id, page=1, edit=True)
    elif action in {"inbox", "later"}:
        await show_state_items_page(callback, user_id, action, page=1, edit=True)
    elif action == "more":
        await render_menu_panel(callback, "⚙️ Дополнительные инструменты", reply_markup=more_menu_keyboard())
        await callback.answer()
    elif action == "random":
        row = VaultService.random_item(user_id)
        if not row:
            await render_menu_panel(callback, "🎲 Архив пока пуст. Отправь мне что-нибудь — и я это сохраню.", reply_markup=main_menu_keyboard())
            await callback.answer()
        else:
            await show_item_view(callback, user_id, int(row["id"]), origin="all", page=1)
    elif action == "search":
        await state.update_data(search_mode="normal")
        await state.set_state(SearchStates.waiting_for_query)
        await render_menu_panel(callback, "🔎 Введи запрос для поиска по сохранённому.", reply_markup=main_menu_keyboard())
        await callback.answer()
    elif action == "favorites":
        await _show_favorites_page(callback, user_id, 1, edit=True)
    elif action == "rss":
        await show_rss_page(callback, user_id)
        await callback.answer()
    elif action == "reminders":
        await show_reminders_page(callback, user_id, 1, edit=True)
    elif action == "help":
        await render_menu_panel(
            callback,
            "📚 Выбери действие кнопками меню.\n\n"
            "/backup — сохранить снимок базы в этот чат\n"
            "/restore — ответить на сообщение со снимком и восстановить базу\n"
            "/status — состояние бота",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
    else:
        await callback.answer("Неизвестный раздел", show_alert=True)


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    logging.info("Received /start from an authorized user")
    await state.clear()
    await _forget_menu_panel(message.from_user.id, message.chat.id)
    payload = (message.text or "").split(maxsplit=1)
    if len(payload) == 2:
        match = re.fullmatch(r"item_(\d+)", payload[1].strip())
        if match:
            await show_item_view(message, message.from_user.id, int(match.group(1)), origin="all", page=1)
            return

    text = (
        "🏠 Твоё личное хранилище\n\n"
        "Отправь сюда текст, ссылку, фото или файл — я сохраню его во Входящие.\n\n"
        "Разбирай новые материалы, откладывай важное на потом и возвращайся к старым через поиск или случайную находку."
    )
    try:
        await render_menu_panel(message, text, reply_markup=main_menu_keyboard())
    except Exception:
        logging.exception("Could not render menu for /start; sending a fresh menu message")
        await message.answer(text, reply_markup=main_menu_keyboard())


@dp.inline_query()
async def inline_archive_search(inline_query: InlineQuery):
    """Search privately; selecting a cached result sends that saved item into the current chat."""
    query = (inline_query.query or "").strip()
    direct_match = re.fullmatch(r"item:(\d+)", query, flags=re.IGNORECASE)
    if direct_match:
        row = VaultService.get_item(inline_query.from_user.id, int(direct_match.group(1)))
        rows = [row] if row else []
    elif query:
        rows = IndexService.search(
            inline_query.from_user.id,
            query,
            limit=20,
            deep=True,
        )
    else:
        # The "share from archive" button opens inline mode with an empty query.
        # Show recent items immediately; requiring a search term made the button
        # look broken and hid the archive until the user guessed what to type.
        rows = VaultService.list_items(inline_query.from_user.id)[:20]
    if not rows:
        await inline_query.answer([], cache_time=0, is_personal=True)
        return

    me = await bot.get_me()
    results = []
    for row in rows:
        title = (row["page_title"] or row["file_name"] or row["tag"] or row["content"] or f"Материал #{row['id']}")
        title = " ".join(str(title).split())[:120]
        description = " ".join(str(row["content"] or row["link_url"] or row["link_domain"] or "Сохранённый материал").split())[:180]
        deep_link = f"https://t.me/{me.username}?start=item_{row['id']}"
        callback_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔐 Открыть в личном архиве", url=deep_link)
        ]])
        caption = f"{title}\n\n{safe_excerpt(description, 700)}"
        kind = row["kind"]
        file_id = row["telegram_file_id"]
        if file_id and kind == "photo":
            result = InlineQueryResultCachedPhoto(
                id=str(row["id"]), photo_file_id=file_id,
                caption=caption, reply_markup=callback_markup,
            )
        elif file_id and kind == "document":
            result = InlineQueryResultCachedDocument(
                id=str(row["id"]), title=title, document_file_id=file_id,
                description=description, caption=caption, reply_markup=callback_markup,
            )
        elif file_id and kind == "video":
            result = InlineQueryResultCachedVideo(
                id=str(row["id"]), video_file_id=file_id, title=title,
                description=description, caption=caption, reply_markup=callback_markup,
            )
        elif file_id and kind == "audio":
            result = InlineQueryResultCachedAudio(
                id=str(row["id"]), audio_file_id=file_id, caption=caption,
                reply_markup=callback_markup,
            )
        elif file_id and kind == "voice":
            result = InlineQueryResultCachedVoice(
                id=str(row["id"]), voice_file_id=file_id, title=title,
                caption=caption, reply_markup=callback_markup,
            )
        elif file_id and kind == "animation":
            result = InlineQueryResultCachedGif(
                id=str(row["id"]), gif_file_id=file_id, title=title,
                caption=caption, reply_markup=callback_markup,
            )
        else:
            result = InlineQueryResultArticle(
                id=str(row["id"]),
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=(row["content"] or row["link_url"] or f"Сохранённый материал: {deep_link}")
                ),
                reply_markup=callback_markup,
            )
        results.append(result)
    await inline_query.answer(results, cache_time=0, is_personal=True)


@dp.callback_query(F.data == "rss_add_start")
async def rss_add_start_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(RSSStates.waiting_for_url)
    await render_menu_panel(
        callback,
        "📡 Пришли ссылку на RSS или Atom-ленту. Для отмены нажми «Ещё».",
        reply_markup=back_keyboard("menu:rss"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rss_remove_"))
async def rss_remove_callback(callback: CallbackQuery):
    feed_id = int(callback.data.rsplit("_", 1)[1])
    removed = RSSService.remove(callback.from_user.id, feed_id)
    await show_rss_page(callback, callback.from_user.id)
    await callback.answer("Лента удалена" if removed else "Лента не найдена")


@dp.message(RSSStates.waiting_for_url)
async def rss_add_url_handler(message: Message, state: FSMContext):
    raw_url = (message.text or "").strip()
    if raw_url == "/cancel":
        await state.clear()
        await show_rss_page(message, message.from_user.id)
        return
    parsed = LinkService.first_link(raw_url)
    url = parsed.url if parsed else raw_url
    if not url.startswith(("https://", "http://")):
        await render_menu_panel(message, "Пришли полный URL, начинающийся с https:// или http://", reply_markup=back_keyboard("menu:rss"))
        return
    try:
        title, entries = await RSSService.fetch(url)
        if not entries:
            await render_menu_panel(message, "Не нашёл публикации в ленте. Проверь ссылку — нужны RSS или Atom XML.", reply_markup=back_keyboard("menu:rss"))
            return
        feed = RSSService.add(message.from_user.id, url, title, entries[0]["guid"])
    except Exception:
        logging.exception("Could not add RSS feed %s", url)
        await render_menu_panel(message, "Не смог прочитать эту ленту. Проверь ссылку и попробуй ещё раз.", reply_markup=back_keyboard("menu:rss"))
        return
    await state.clear()
    await show_rss_page(message, message.from_user.id)


@dp.message(Command("help"))
async def help_handler(message: Message):
    await render_menu_panel(
        message,
        "📚 Просто отправь текст, ссылку, фото или файл — сохраню во Входящие.\n"
        "Исходник удалю после сохранения копии; при ошибке оставлю его.\n\n"
        "Кнопки меню ведут по одному экрану: переходы обновляют его, а не создают цепочку новых сообщений.\n\n"
        "Папки создаются из раздела «Папки». RSS находится в «Ещё».\n\n"
        "/backup — создать снимок базы в этом чате.\n"
        "/restore — ответить этой командой на сообщение со снимком.\n"
        "/drive_backup — сохранить снимок базы в Google Drive.\n"
        "/drive_backups — список копий Drive и восстановление.\n"
        "/status — проверить состояние и дату последнего снимка.",
        reply_markup=main_menu_keyboard(),
    )

@dp.message(Command("status"))
async def status_command_handler(message: Message):
    health = health_server is not None
    last_backup = BackupService.last_backup_at() or "ещё не создавалась"
    item_count = VaultService.count_items(message.from_user.id)
    text = (
        "📊 Статус бота\n\n"
        f"• Health server: {'on' if health else 'off'}\n"
        f"• Каталог: {item_count} материалов\n"
        f"• База: {'Turso Cloud' if DATABASE_BACKEND == 'turso' else DATABASE_FILE}\n"
        f"• Backup recipients: {len(backup_recipients)}\n"
        f"• Автобэкап: раз в {settings.backup_interval_hours} ч.\n"
        f"• Последний бэкап: {last_backup}"
    )
    if settings.render_free_mode and DATABASE_BACKEND == "sqlite":
        text += "\n\n⚠️ Render Free удаляет локальную базу при redeploy/restart. Для постоянного хранения нужен Persistent Disk на платном плане."
    if not backup_recipients:
        text += "\n\n❌ Автобэкап выключен: укажи свой Telegram ID в OWNER_USER_IDS или ALLOWED_USER_IDS в Render."
    if item_count == 0:
        text += "\n\nЕсли архив раньше был непустым, ответь командой /restore на последнее сообщение с резервной копией."
    await render_menu_panel(message, text, reply_markup=main_menu_keyboard())


@dp.message(Command("backup"))
async def backup_command_handler(message: Message):
    if not backup_recipients:
        await _send_text(message, "❌ Некому отправлять бэкап", reply_markup=main_menu_keyboard())
        return

    await render_menu_panel(message, "🗄 Готовлю резервную копию…", reply_markup=main_menu_keyboard())
    path = await BackupService.send_backup(bot, backup_recipients)
    if path:
        await render_menu_panel(message, f"✅ Резервная копия отправлена: {path.name}\n\nФайл выше оставь в чате — он нужен для восстановления.", reply_markup=main_menu_keyboard())
    elif not BackupService.has_user_data():
        await render_menu_panel(message, "⚠️ База пустая, поэтому пустую резервную копию не отправляю. Если Render потерял каталог, ответь /restore на предыдущую копию.", reply_markup=main_menu_keyboard())
    else:
        await render_menu_panel(message, "❌ Не удалось отправить снимок. Проверь доступ бота к чату и повтори /backup.", reply_markup=main_menu_keyboard())


@dp.message(Command("restore"))
async def restore_command_handler(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        await _send_text(message, "⛔ Восстановление доступно только владельцу.")
        return

    backup_message = message.reply_to_message
    document = backup_message.document if backup_message else None
    caption = (backup_message.caption or "") if backup_message else ""
    if not document or not caption.startswith("🗄 Резервная копия базы"):
        await _send_text(
            message,
            "Ответь командой /restore на сообщение с резервной копией базы в этом чате.",
        )
        return

    restore_dir = Path("data/import_tmp")
    restore_dir.mkdir(parents=True, exist_ok=True)
    restore_path = restore_dir / f"restore-{uuid.uuid4().hex}.sqlite3"
    try:
        await bot.download(document, destination=restore_path)
        source = sqlite3.connect(str(restore_path))
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            tables = {
                row[0]
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = {"vault_items", "vault_folders", "reminders", "bot_meta"}
            if not integrity or integrity[0] != "ok" or not required.issubset(tables):
                raise ValueError("Файл не похож на полноценную базу этого бота")

            restore_from_sqlite_snapshot(restore_path)
        finally:
            source.close()

        init_db()
        await _send_text(message, "✅ База восстановлена из снимка в этом чате.")
    except Exception as exc:
        logging.exception("Database restore failed")
        await _send_text(message, f"❌ Не удалось восстановить базу: {exc}")
    finally:
        restore_path.unlink(missing_ok=True)


async def _upload_drive_database_snapshot() -> dict:
    if not BackupService.has_user_data():
        raise RuntimeError("Текущая база пустая — пустой снимок не загружаю.")
    path = BackupService.create_snapshot(prefix="utilbot-db")
    try:
        count_db = sqlite3.connect(str(path))
        try:
            item_count = int(count_db.execute("SELECT COUNT(*) FROM vault_items").fetchone()[0])
        finally:
            count_db.close()
        return await GoogleDriveService.upload_snapshot(path, item_count=item_count)
    finally:
        path.unlink(missing_ok=True)


@dp.message(Command("drive_backup"))
async def drive_backup_command_handler(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        await _send_text(message, "⛔ Выгрузка в Google Drive доступна только владельцу.")
        return
    if not GoogleDriveService.configured():
        await _send_text(message, "Google Drive ещё не настроен. Сначала добавь адрес Apps Script и общий ключ в Render.")
        return
    await render_menu_panel(message, "☁️ Готовлю копию базы и загружаю её в Google Drive…", reply_markup=main_menu_keyboard())
    try:
        uploaded = await _upload_drive_database_snapshot()
        await render_menu_panel(
            message,
            f"✅ Копия базы сохранена в Google Drive.\n\nФайл: {uploaded.get('name', 'utilbot backup')}\nРазмер: {uploaded.get('size', '—')} байт",
            reply_markup=main_menu_keyboard(),
        )
    except Exception as exc:
        logging.exception("Google Drive backup failed")
        await render_menu_panel(message, f"❌ Не удалось сохранить копию в Google Drive: {str(exc)[:700]}", reply_markup=main_menu_keyboard())


@dp.message(Command("drive_backups"))
async def drive_backups_command_handler(message: Message):
    if not _is_owner(message.from_user.id if message.from_user else None):
        await _send_text(message, "⛔ Просмотр копий доступен только владельцу.")
        return
    if not GoogleDriveService.configured():
        await _send_text(message, "Google Drive ещё не настроен. Сначала добавь адрес Apps Script и общий ключ в Render.")
        return
    try:
        files = await GoogleDriveService.list_snapshots(limit=10)
    except Exception as exc:
        logging.exception("Could not list Google Drive backups")
        await render_menu_panel(message, f"❌ Не удалось получить список копий: {str(exc)[:700]}", reply_markup=main_menu_keyboard())
        return
    if not files:
        await render_menu_panel(message, "В папке Google Drive пока нет копий базы. Создай первую командой /drive_backup.", reply_markup=main_menu_keyboard())
        return

    buttons = []
    lines = ["☁️ Копии базы в Google Drive", "", "Выбери копию для восстановления:"]
    for index, item in enumerate(files, 1):
        name = str(item.get("name", "Резервная копия"))
        created = str(item.get("createdTime", ""))[:16].replace("T", " ")
        lines.append(f"{index}. {created or name} ({item.get('size', '—')} байт)")
        buttons.append([InlineKeyboardButton(text=f"↩️ Восстановить #{index}", callback_data=f"gdrive_restore_{item['id']}")])
    await render_menu_panel(message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("gdrive_restore_"))
async def drive_restore_select_handler(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Только владелец может восстанавливать базу.", show_alert=True)
        return
    file_id = callback.data.removeprefix("gdrive_restore_")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Да, заменить базу", callback_data=f"gdrive_confirm_{file_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data="gdrive_restore_cancel")],
    ])
    await render_menu_panel(
        callback,
        "⚠️ Восстановить эту копию? Текущие данные Turso будут заменены. Перед этим бот попробует сохранить текущую непустую базу в Google Drive.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data == "gdrive_restore_cancel")
async def drive_restore_cancel_handler(callback: CallbackQuery):
    await render_menu_panel(callback, "Восстановление отменено.", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("gdrive_confirm_"))
async def drive_restore_confirm_handler(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Только владелец может восстанавливать базу.", show_alert=True)
        return
    file_id = callback.data.removeprefix("gdrive_confirm_")
    restore_dir = Path("data/import_tmp")
    restore_dir.mkdir(parents=True, exist_ok=True)
    restore_path = restore_dir / f"gdrive-restore-{uuid.uuid4().hex}.sqlite3"
    await callback.answer("Восстанавливаю базу…")
    try:
        if BackupService.has_user_data():
            safety = await _upload_drive_database_snapshot()
            logging.info("Saved pre-restore Google Drive snapshot %s", safety.get("id"))
        await GoogleDriveService.download_snapshot(file_id, restore_path)
        source = sqlite3.connect(str(restore_path))
        try:
            integrity = source.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            required = {"vault_items", "vault_folders", "reminders", "bot_meta"}
            if not integrity or integrity[0] != "ok" or not required.issubset(tables):
                raise ValueError("Файл не похож на полноценную базу этого бота")
            restore_from_sqlite_snapshot(restore_path)
        finally:
            source.close()
        init_db()
        await render_menu_panel(callback, "✅ База восстановлена из Google Drive.", reply_markup=main_menu_keyboard())
    except Exception as exc:
        logging.exception("Google Drive restore failed")
        await render_menu_panel(callback, f"❌ Не удалось восстановить базу: {str(exc)[:700]}", reply_markup=main_menu_keyboard())
    finally:
        restore_path.unlink(missing_ok=True)


@dp.callback_query(F.data == "folder_create")
async def folder_create_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FolderCreateStates.waiting_for_name)
    await render_menu_panel(
        callback,
        "📁 Напиши название новой папки. Отмена — кнопкой ниже.",
        reply_markup=back_keyboard("menu:vault"),
    )
    await callback.answer()


@dp.message(FolderCreateStates.waiting_for_name)
async def folder_create_name_handler(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name == "/cancel":
        await state.clear()
        await show_folders_page(message, message.from_user.id, page=1)
        return
    if not name or len(name) > 60:
        await render_menu_panel(message, "Название папки должно быть от 1 до 60 символов.", reply_markup=back_keyboard("menu:vault"))
        return
    folder = VaultService.create_folder(message.from_user.id, name)
    await state.clear()
    if not folder:
        await render_menu_panel(message, "Не получилось создать папку. Попробуй другое название.", reply_markup=back_keyboard("menu:vault"))
        return
    await show_folders_page(message, message.from_user.id, page=1)


@dp.message(Command("save"))
async def save_command_handler(message: Message, state: FSMContext):
    if message.reply_to_message:
        await _begin_save_flow(
            message,
            state,
            payload_type="reply",
            replied_message=message.reply_to_message,
        )
        return

    if not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await _send_text(
            message,
            "❌ Использование:\n/save текст\n\nили reply /save на сообщение",
            reply_markup=main_menu_keyboard(),
        )
        return

    await _begin_save_flow(
        message,
        state,
        payload_type="note",
        note_text=parts[1].strip(),
    )


@dp.message(SaveWordFilter())
async def keyword_save_handler(message: Message, state: FSMContext):
    if message.reply_to_message:
        await _begin_save_flow(
            message,
            state,
            payload_type="reply",
            replied_message=message.reply_to_message,
        )
        return

    if message.text and len(message.text.split(maxsplit=1)) > 1:
        await _begin_save_flow(
            message,
            state,
            payload_type="note",
            note_text=message.text.split(maxsplit=1)[1].strip(),
        )
        return

    await _send_text(
        message,
        "❌ Ответь на сообщение или напиши: save текст",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(F.reply_to_message, F.text)
async def quick_tag_reply_handler(message: Message):
    replied = message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return

    reply_text = replied.caption or replied.text or ""
    match = re.search(r"#(\d+)", reply_text)
    if not match:
        return

    tag_input = message.text.strip()
    if not tag_input or tag_input.startswith("/") or tag_input.lower() in SAVE_WORDS:
        return

    item_id = int(match.group(1))
    item = VaultService.update_item_tags(message.from_user.id, item_id, tag_input)
    with suppress(Exception):
        await message.delete()
    if not item:
        await _send_text(message, "❌ Item не найден", reply_markup=main_menu_keyboard())
        return

    await _send_text(
        message,
        f"🏷 Тег обновлён: #{item_id} → {item['tag'] or '—'}",
        reply_markup=main_menu_keyboard(),
    )


@dp.callback_query(F.data == "cancel_save")

async def cancel_save_handler(callback: CallbackQuery, state: FSMContext):
    pending_media.pop(callback.from_user.id, None)
    await state.clear()
    await show_home(callback)


@dp.callback_query(F.data == "confirm_save")
async def confirm_save_handler(callback: CallbackQuery, state: FSMContext):
    replied = pending_media.get(callback.from_user.id)
    if not replied:
        await callback.answer("Сообщение потеряно", show_alert=True)
        return

    await _begin_save_flow(
        callback,
        state,
        payload_type="reply",
        replied_message=replied,
    )


@dp.message(SaveStates.waiting_for_tag)
async def save_tag_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await _send_text(message, "🏷 Введи тег или напиши 'пропустить'.")
        return

    tag_input = message.text.strip()
    tag = None if tag_input.lower() in SKIP_TAG_WORDS else tag_input

    await state.update_data(tag=tag)
    await state.set_state(SaveStates.waiting_for_folder)
    await show_save_folders_page(message, message.from_user.id, page=1)


@dp.callback_query(F.data.startswith("save_folders_page_"))
async def save_folders_page_handler(callback: CallbackQuery):
    page = int(callback.data.replace("save_folders_page_", ""))
    await show_save_folders_page(callback, callback.from_user.id, page=page, edit=True)


@dp.callback_query(F.data.startswith("save_folder_"))
async def save_folder_handler(callback: CallbackQuery, state: FSMContext):
    folder_id = int(callback.data.replace("save_folder_", ""))
    folder = VaultService.get_folder_by_id(user_id=callback.from_user.id, folder_id=folder_id)
    if not folder:
        await callback.answer("Папка не найдена", show_alert=True)
        return

    data = await state.get_data()
    replied = data.get("replied_message")
    payload_type = data.get("payload_type")
    tag = data.get("tag")

    if payload_type == "note":
        note_text = data.get("note_text")
        if not note_text:
            await callback.answer("Заметка потеряна", show_alert=True)
            return

        dedupe_key = _text_dedupe_key(note_text)
        duplicate = VaultService.find_duplicate_item(callback.from_user.id, dedupe_key)
        if duplicate:
            await state.clear()
            await callback.answer("Этот текст уже есть в архиве")
            await show_item_view(
                callback,
                callback.from_user.id,
                int(duplicate["id"]),
                origin="all",
                page=1,
                show_media_preview=False,
            )
            return

        try:
            archive_msg = await bot.send_message(
                chat_id=callback.message.chat.id,
                text=note_text,
            )
        except Exception:
            logging.exception("Could not archive a text item in the bot chat")
            await callback.answer("Не получилось сохранить копию в чате", show_alert=True)
            return

        saved_note_id = VaultService.save_item(
            user_id=callback.from_user.id,
            folder_id=folder_id,
            kind="text",
            content=note_text,
            tag=tag,
            archive_chat_id=archive_msg.chat.id,
            archive_message_id=archive_msg.message_id,
            sender_name=callback.from_user.full_name,
            sender_username=callback.from_user.username,
            source_chat_title=callback.message.chat.title if callback.message and callback.message.chat else None,
            source_chat_id=callback.message.chat.id if callback.message and callback.message.chat else None,
            source_message_id=callback.message.message_id if callback.message else None,
            dedupe_key=dedupe_key,
        )
        item_id = saved_note_id
    else:
        if not replied:
            await callback.answer("Сообщение потеряно", show_alert=True)
            return

        payload = MediaService.from_message(replied)
        if not payload:
            await callback.answer("Не удалось прочитать сообщение для проверки повтора", show_alert=True)
            return
        dedupe_key = _capture_dedupe_key(replied, payload)
        duplicate = VaultService.find_duplicate_item(callback.from_user.id, dedupe_key)
        if duplicate:
            pending_media.pop(callback.from_user.id, None)
            await state.clear()
            await callback.answer("Этот материал уже есть в архиве")
            await show_item_view(
                callback,
                callback.from_user.id,
                int(duplicate["id"]),
                origin="all",
                page=1,
                show_media_preview=False,
            )
            return

        try:
            archived = await _archive_message(replied, callback.message.chat.id)
        except Exception:
            logging.exception("Could not archive a media item in the bot chat")
            await callback.answer("Не получилось создать копию записи в этом чате", show_alert=True)
            return

        saved = _save_media_payload(
            callback.from_user.id,
            folder_id,
            replied,
            tag=tag,
            archive_chat_id=callback.message.chat.id,
            archive_message_id=archived.message_id,
            dedupe_key=dedupe_key,
        )
        if not saved:
            await callback.answer("Неподдерживаемый тип сообщения", show_alert=True)
            return
        item_id = saved

    pending_media.pop(callback.from_user.id, None)
    await state.clear()
    if (
        replied
        and replied.chat.type == "private"
        and replied.chat.id == callback.message.chat.id
        and replied.from_user
        and replied.from_user.id == callback.from_user.id
    ):
        try:
            await replied.delete()
        except Exception:
            logging.info("Saved replied message %s, but could not delete its original", replied.message_id)
    await _forget_menu_panel(callback.from_user.id, callback.message.chat.id)
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )


@dp.message(FolderRenameStates.waiting_for_new_name)
async def folder_rename_text_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    folder_id = data.get("folder_id")
    folders_page = data.get("folders_page", 1)
    page = data.get("page", 1)

    if not folder_id or not message.text or not message.text.strip():
        await _send_text(message, "✏️ Введи новое имя папки.")
        return

    folder = VaultService.rename_folder(message.from_user.id, folder_id, message.text.strip())
    await state.clear()
    if not folder:
        await _send_text(message, "❌ Не удалось переименовать.")
        return

    await show_folder_items_page(message, message.from_user.id, folder_id, folders_page, page, edit=True)


@dp.callback_query(F.data.startswith("vault_folders_page_"))
async def vault_folders_page_handler(callback: CallbackQuery):
    page = int(callback.data.replace("vault_folders_page_", ""))
    await show_folders_page(callback, callback.from_user.id, page=page, edit=True)


@dp.callback_query(F.data.startswith("open_folder_"))
async def open_folder_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    await show_folder_items_page(
        callback,
        callback.from_user.id,
        folder_id=folder_id,
        folders_page=folders_page,
        page=1,
        edit=True,
    )


@dp.callback_query(F.data.startswith("vault_items_page_"))
async def vault_items_page_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[3])
    folders_page = int(parts[4])
    page = int(parts[5])
    await show_folder_items_page(
        callback,
        callback.from_user.id,
        folder_id=folder_id,
        folders_page=folders_page,
        page=page,
        edit=True,
    )


@dp.callback_query(F.data.startswith("vault_all_page_"))
async def vault_all_page_handler(callback: CallbackQuery):
    page = int(callback.data.replace("vault_all_page_", ""))
    await show_all_files_page(callback, callback.from_user.id, page=page, edit=True)


@dp.callback_query(F.data == "search_prompt")
async def search_prompt_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_query)
    await _edit_text_or_send(callback, "🔎 Введи запрос поиска.")


@dp.callback_query(F.data.startswith("search_page_"))
async def search_page_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = parts[2]
    page = int(parts[3])
    await show_search_results_page(callback, session_id, page=page, edit=True)


@dp.message(SearchStates.waiting_for_query)
async def search_query_state_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await _send_text(message, "🔎 Введи запрос поиска.")
        return
    await run_search_query(message, message.text, state)


@dp.callback_query(F.data.startswith("open_item_f_"))
async def open_item_folder_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[3])
    folders_page = int(parts[4])
    page = int(parts[5])
    item_id = int(parts[6])
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="folder",
        folder_id=folder_id,
        folders_page=folders_page,
        page=page,
    )


@dp.callback_query(F.data.startswith("open_item_a_"))
async def open_item_all_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    page = int(parts[3])
    item_id = int(parts[4])
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=page,
    )


@dp.callback_query(F.data.startswith("open_item_s_"))
async def open_item_search_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = parts[3]
    page = int(parts[4])
    item_id = int(parts[5])
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="search",
        page=page,
        session_id=session_id,
    )


@dp.callback_query(F.data.startswith("item_move_f_"))
async def item_move_folder_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    folder_id = int(parts[3])
    folders_page = int(parts[4])
    page = int(parts[5])
    item_id = int(parts[6])

    await state.update_data(
        item_id=item_id,
        origin="folder",
        folder_id=folder_id,
        folders_page=folders_page,
        page=page,
    )
    await state.set_state(ItemMoveStates.waiting_for_folder)
    await show_move_folders_page(callback, callback.from_user.id, page=1, edit=True)


@dp.callback_query(F.data.startswith("item_move_a_"))
async def item_move_all_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    page = int(parts[3])
    item_id = int(parts[4])

    await state.update_data(
        item_id=item_id,
        origin="all",
        page=page,
        show_media_preview=_action_preview_preference(callback, item_id),
    )
    await state.set_state(ItemMoveStates.waiting_for_folder)
    await show_move_folders_page(callback, callback.from_user.id, page=1, edit=True)


@dp.callback_query(F.data.startswith("item_move_r_"))
async def item_move_review_callback(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.rsplit("_", 1)[1])
    await state.update_data(item_id=item_id, origin="review", show_media_preview=True)
    await state.set_state(ItemMoveStates.waiting_for_folder)
    await show_move_folders_page(callback, callback.from_user.id, page=1, edit=True)


@dp.callback_query(F.data.startswith("item_move_s_"))
async def item_move_search_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    session_id = parts[3]
    page = int(parts[4])
    item_id = int(parts[5])

    await state.update_data(
        item_id=item_id,
        origin="search",
        session_id=session_id,
        page=page,
    )
    await state.set_state(ItemMoveStates.waiting_for_folder)
    await show_move_folders_page(callback, callback.from_user.id, page=1, edit=True)


@dp.callback_query(F.data.startswith("item_tag_f_"))
async def item_tag_folder_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    folder_id = int(parts[3])
    folders_page = int(parts[4])
    page = int(parts[5])
    item_id = int(parts[6])

    await state.update_data(
        item_id=item_id,
        origin="folder",
        folder_id=folder_id,
        folders_page=folders_page,
        page=page,
    )
    await state.set_state(ItemTagStates.waiting_for_tag)
    await _edit_text_or_send(callback, "🏷 Введи новый тег или напиши 'пропустить'.")


@dp.callback_query(F.data.startswith("item_tag_a_"))
async def item_tag_all_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    page = int(parts[3])
    item_id = int(parts[4])

    await state.update_data(
        item_id=item_id,
        origin="all",
        page=page,
        show_media_preview=_action_preview_preference(callback, item_id),
    )
    await state.set_state(ItemTagStates.waiting_for_tag)
    await _edit_text_or_send(callback, "🏷 Введи новый тег или напиши 'пропустить'.")


@dp.callback_query(F.data.startswith("item_tag_s_"))
async def item_tag_search_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    session_id = parts[3]
    page = int(parts[4])
    item_id = int(parts[5])

    await state.update_data(
        item_id=item_id,
        origin="search",
        session_id=session_id,
        page=page,
    )
    await state.set_state(ItemTagStates.waiting_for_tag)
    await _edit_text_or_send(callback, "🏷 Введи новый тег или напиши 'пропустить'.")


@dp.message(ItemTagStates.waiting_for_tag)
async def item_tag_text_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await _send_text(message, "🏷 Введи новый тег или напиши 'пропустить'.")
        return

    data = await state.get_data()
    item_id = data.get("item_id")
    origin = data.get("origin")
    tag_input = message.text.strip()
    tag = None if tag_input.lower() in SKIP_TAG_WORDS else tag_input

    item = VaultService.update_item_tag(message.from_user.id, item_id, tag)
    if not item:
        await state.clear()
        await _send_text(message, "❌ Item не найден.")
        return

    await state.clear()

    if origin == "folder":
        await show_item_view(
            message,
            message.from_user.id,
            item_id,
            origin="folder",
            folder_id=data["folder_id"],
            folders_page=data["folders_page"],
            page=data["page"],
            show_media_preview=data.get("show_media_preview", True),
        )
    elif origin == "search":
        await show_item_view(
            message,
            message.from_user.id,
            item_id,
            origin="search",
            page=data["page"],
            session_id=data["session_id"],
            show_media_preview=data.get("show_media_preview", True),
        )
    else:
        await show_item_view(
            message,
            message.from_user.id,
            item_id,
            origin="all",
            page=data["page"],
            show_media_preview=data.get("show_media_preview", True),
        )


@dp.callback_query(F.data.regexp(r"^item_context_\d+$"))
async def item_context_start_handler(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.rsplit("_", 1)[1])
    item = VaultService.get_item(callback.from_user.id, item_id)
    if not item or not item["archive_message_id"]:
        await callback.answer("Не нашёл архивную копию для этой записи", show_alert=True)
        return

    view_data = _saved_item_view_state(callback.from_user.id, item_id)
    back_callback = (view_data or {}).get("back_callback", "vault_all_page_1")
    if not view_data and callback.message and callback.message.reply_markup:
        for row in callback.message.reply_markup.inline_keyboard:
            for button in row:
                if button.text.startswith("⬅️") and button.callback_data:
                    back_callback = button.callback_data
                    break

    await state.clear()
    await state.update_data(
        item_id=item_id,
        back_callback=back_callback,
        show_media_preview=(view_data or {}).get("show_media_preview", True),
    )
    await state.set_state(ItemContextStates.waiting_for_text)
    await render_menu_panel(
        callback,
        "🗒 Напиши короткий контекст к материалу (до 1200 символов). "
        "Текст будет сохранён ответом на архивную копию в этом чате.\n\n"
        "Отмена: /cancel",
        reply_markup=back_keyboard(f"item_context_cancel_{item_id}"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("item_context_cancel_"))
async def item_context_cancel_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    item_id = int(callback.data.rsplit("_", 1)[1])
    await state.clear()
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        back_callback_override=data.get("back_callback"),
        show_media_preview=data.get("show_media_preview", True),
    )


@dp.message(ItemContextStates.waiting_for_text)
async def item_context_text_handler(message: Message, state: FSMContext):
    context_text = (message.text or "").strip()
    data = await state.get_data()
    item_id = data.get("item_id")
    if context_text == "/cancel":
        await state.clear()
        await show_item_view(
            message,
            message.from_user.id,
            int(item_id),
            origin="all",
            page=1,
            back_callback_override=data.get("back_callback"),
            show_media_preview=data.get("show_media_preview", True),
        )
        return
    if not context_text:
        await render_menu_panel(message, "🗒 Контекст должен быть текстом. Отправь текст или /cancel.")
        return
    if len(context_text) > 1200:
        await render_menu_panel(message, "Слишком длинно: максимум 1200 символов. Сократи контекст и отправь ещё раз.")
        return

    item = VaultService.get_item(message.from_user.id, int(item_id))
    if not item or not item["archive_message_id"]:
        await state.clear()
        await render_menu_panel(message, "❌ Материал или его копия не найдены.", reply_markup=main_menu_keyboard())
        return

    chat_id = int(item["archive_chat_id"] or message.chat.id)
    context_message_id = item["context_message_id"]
    try:
        if context_message_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(context_message_id),
                text=context_text,
            )
        else:
            note = await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_parameters=ReplyParameters(message_id=int(item["archive_message_id"])),
            )
            context_message_id = note.message_id
    except Exception:
        logging.exception("Could not save context for item %s in Telegram", item_id)
        await render_menu_panel(message, "❌ Не получилось сохранить контекст ответом на архивную копию. Твой текст оставил.")
        return

    updated = VaultService.update_item_context(
        message.from_user.id,
        int(item_id),
        context_text,
        int(context_message_id),
    )
    if not updated:
        await state.clear()
        await render_menu_panel(message, "Контекст остался в чате, но запись каталога не обновилась. Повтори попытку через карточку материала.")
        return

    with suppress(Exception):
        await bot.delete_message(message.chat.id, message.message_id)
    await state.clear()
    await show_item_view(
        message,
        message.from_user.id,
        int(item_id),
        origin="all",
        page=1,
        back_callback_override=data.get("back_callback"),
        show_media_preview=data.get("show_media_preview", True),
    )


@dp.message(ItemMoveStates.waiting_for_folder)
async def item_move_folder_text_handler(message: Message, state: FSMContext):
    await _send_text(message, "📁 Выбери папку кнопкой ниже.")


@dp.callback_query(F.data == "move_cancel")
async def move_cancel_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(callback)


@dp.callback_query(F.data.startswith("move_folders_page_"))
async def move_folders_page_handler(callback: CallbackQuery):
    page = int(callback.data.replace("move_folders_page_", ""))
    await show_move_folders_page(callback, callback.from_user.id, page=page, edit=True)


@dp.callback_query(F.data.startswith("move_folder_"))
async def move_folder_select_handler(callback: CallbackQuery, state: FSMContext):
    folder_id = int(callback.data.replace("move_folder_", ""))
    data = await state.get_data()
    item_id = data.get("item_id")
    origin = data.get("origin")

    if not item_id:
        await state.clear()
        await callback.answer("Item потерян", show_alert=True)
        return

    moved = VaultService.move_item(callback.from_user.id, item_id, folder_id)
    if not moved:
        await state.clear()
        await callback.answer("Не удалось переместить", show_alert=True)
        return

    await state.clear()
    if origin == "review":
        VaultService.set_item_state(callback.from_user.id, item_id, "saved")
        await callback.answer("Перемещено в папку")
        await show_next_inbox_item(callback, callback.from_user.id, skip_item_id=item_id)
    elif origin == "folder":
        await show_item_view(
            callback,
            callback.from_user.id,
            item_id,
            origin="folder",
            folder_id=data["folder_id"],
            folders_page=data["folders_page"],
            page=data["page"],
            show_media_preview=data.get("show_media_preview", True),
        )
    elif origin == "search":
        await show_item_view(
            callback,
            callback.from_user.id,
            item_id,
            origin="search",
            page=data["page"],
            session_id=data["session_id"],
            show_media_preview=data.get("show_media_preview", True),
        )
    else:
        await show_item_view(
            callback,
            callback.from_user.id,
            item_id,
            origin="all",
            page=data["page"],
            show_media_preview=data.get("show_media_preview", True),
        )


async def _delete_archived_item(user_id: int, item_id: int) -> tuple[bool, bool]:
    item = VaultService.get_item(user_id, item_id)
    if not item:
        return False, False

    chat_id = int(item["archive_chat_id"] or user_id)
    archive_removed = False
    archive_message_id = item["archive_message_id"]
    context_message_id = item["context_message_id"]
    try:
        archive_message_ids = [int(value) for value in json.loads(item["archive_message_ids"] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        archive_message_ids = []
    if not archive_message_ids and archive_message_id:
        archive_message_ids = [int(archive_message_id)]

    for message_id in ([context_message_id] if context_message_id else []) + archive_message_ids:
        if not message_id:
            continue
        try:
            await bot.delete_message(chat_id, int(message_id))
            if message_id in archive_message_ids:
                archive_removed = True
        except Exception:
            logging.info(
                "Could not delete Telegram archive message %s for item %s",
                message_id,
                item_id,
                exc_info=True,
            )

    VaultService.delete_item(user_id, item_id)

    preview_id = _get_meta_value(f"item_preview_message_id:{user_id}")
    if preview_id:
        with suppress(Exception):
            await bot.delete_message(user_id, int(preview_id))
        _set_meta_value(f"item_preview_message_id:{user_id}", "")

    return True, archive_removed


@dp.callback_query(F.data.startswith("delete_item_f_"))
async def delete_item_folder_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[3])
    folders_page = int(parts[4])
    page = int(parts[5])
    item_id = int(parts[6])

    await _delete_archived_item(callback.from_user.id, item_id)
    await show_folder_items_page(callback, callback.from_user.id, folder_id, folders_page, page, edit=True)


@dp.callback_query(F.data.startswith("review_delete_"))
async def review_delete_item_callback(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    _, archive_removed = await _delete_archived_item(callback.from_user.id, item_id)
    await callback.answer("Материал и копия удалены" if archive_removed else "Запись удалена из каталога")
    await show_next_inbox_item(callback, callback.from_user.id, skip_item_id=item_id)


@dp.callback_query(F.data.startswith("delete_item_a_"))
async def delete_item_all_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    page = int(parts[3])
    item_id = int(parts[4])

    await _delete_archived_item(callback.from_user.id, item_id)
    await show_all_files_page(callback, callback.from_user.id, page, edit=True)


@dp.callback_query(F.data.startswith("delete_item_s_"))
async def delete_item_search_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    session_id = parts[3]
    page = int(parts[4])
    item_id = int(parts[5])

    await _delete_archived_item(callback.from_user.id, item_id)
    await show_search_results_page(callback, session_id, page, edit=True)


@dp.callback_query(F.data.startswith("folder_manage_"))
async def folder_manage_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    page = int(parts[4])

    folder = VaultService.get_folder_by_id(callback.from_user.id, folder_id)
    if not folder:
        await callback.answer("Папка не найдена", show_alert=True)
        return

    text = (
        f"⚙️ Папка: {folder['emoji'] or '📁'} {folder['name']}\n"
        f"ID: {folder['id']}\n"
        f"Статус: {'архив' if folder['archived_at'] else 'активна'}"
    )
    markup = folder_actions_keyboard(folder, folders_page=folders_page, page=page)
    await _edit_text_or_send(callback, text, reply_markup=markup)


@dp.callback_query(F.data.startswith("folder_rename_"))
async def folder_rename_handler(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    page = int(parts[4])

    await state.update_data(folder_id=folder_id, folders_page=folders_page, page=page)
    await state.set_state(FolderRenameStates.waiting_for_new_name)
    await _edit_text_or_send(callback, "✏️ Введи новое имя папки.")


@dp.callback_query(F.data.startswith("folder_archive_"))
async def folder_archive_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    page = int(parts[4])

    folder = VaultService.get_folder_by_id(callback.from_user.id, folder_id)
    if not folder:
        await callback.answer("Папка не найдена", show_alert=True)
        return
    if folder["name"] == VaultService.DEFAULT_FOLDER_NAME:
        await callback.answer("Inbox нельзя архивировать", show_alert=True)
        return

    VaultService.archive_folder(callback.from_user.id, folder_id)
    await show_folders_page(callback, callback.from_user.id, folders_page, edit=True)


@dp.callback_query(F.data.startswith("folder_restore_"))
async def folder_restore_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    page = int(parts[4])

    VaultService.restore_folder(callback.from_user.id, folder_id)
    await show_folders_page(callback, callback.from_user.id, folders_page, edit=True)


@dp.callback_query(F.data.startswith("folder_delete_"))
async def folder_delete_handler(callback: CallbackQuery):
    parts = callback.data.split("_")
    folder_id = int(parts[2])
    folders_page = int(parts[3])
    page = int(parts[4])

    folder = VaultService.get_folder_by_id(callback.from_user.id, folder_id)
    if not folder:
        await callback.answer("Папка не найдена", show_alert=True)
        return
    if folder["name"] == VaultService.DEFAULT_FOLDER_NAME:
        await callback.answer("Inbox нельзя удалять", show_alert=True)
        return

    VaultService.delete_folder(callback.from_user.id, folder_id)
    await show_folders_page(callback, callback.from_user.id, folders_page, edit=True)


@dp.callback_query(F.data.startswith("state_page_"))
async def state_page_handler(callback: CallbackQuery):
    _, _, item_state, page = callback.data.split("_", 3)
    await show_state_items_page(callback, callback.from_user.id, item_state, int(page), edit=True)


@dp.callback_query(F.data.startswith("open_state_"))
async def state_open_item_handler(callback: CallbackQuery):
    _, _, item_state, page, item_id = callback.data.split("_", 4)
    await show_item_view(
        callback,
        callback.from_user.id,
        int(item_id),
        origin="state",
        folder_id=item_state,
        page=int(page),
    )


@dp.callback_query(F.data == "inbox_review_start")
async def inbox_review_start_handler(callback: CallbackQuery):
    await show_next_inbox_item(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("review_skip_"))
async def inbox_review_skip_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    await show_next_inbox_item(callback, callback.from_user.id, skip_item_id=item_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("captured_open_"))
async def captured_open_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )


@dp.callback_query(F.data.startswith("rss_save_"))
async def rss_save_entry_callback(callback: CallbackQuery):
    entry_id = int(callback.data.rsplit("_", 1)[1])
    entry = RSSService.get_entry(callback.from_user.id, entry_id)
    if not entry:
        await callback.answer("Публикация не найдена", show_alert=True)
        return
    if entry["saved_item_id"]:
        await callback.answer("Уже сохранено в архив")
        return

    dedupe_key = f"rss:{entry['feed_id']}:{entry['guid']}"
    archive = None
    saved = False
    try:
        archive = await bot.copy_message(
            chat_id=callback.message.chat.id,
            from_chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
        )
        folder = VaultService.get_or_create_default_folder(callback.from_user.id)
        content = f"{entry['title']}\n{entry['url']}\n\n{entry['summary']}".strip()
        item_id = await _save_text_payload(
            user_id=callback.from_user.id,
            folder_id=int(folder["id"]),
            text=content,
            tag="rss",
            sender_name="RSS",
            source_chat_title="RSS",
            archive_chat_id=callback.message.chat.id,
            archive_message_id=archive.message_id,
            item_state="inbox",
            dedupe_key=dedupe_key,
        )
        saved = True
        RSSService.set_saved_item(entry_id, callback.from_user.id, item_id)
    except Exception:
        logging.exception("Could not save RSS entry %s", entry_id)
        if archive and not saved:
            with suppress(Exception):
                await bot.delete_message(callback.message.chat.id, archive.message_id)
        await callback.answer("Не удалось сохранить; попробуй ещё раз", show_alert=True)
        return
    await callback.answer("Сохранено во Входящие")
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )


@dp.callback_query(F.data.startswith("captured_favorite_"))
async def captured_favorite_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    item = VaultService.toggle_favorite(callback.from_user.id, item_id)
    if not item:
        await callback.answer("Материал не найден", show_alert=True)
        return
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )
    await callback.answer("Добавлено в избранное" if item["is_favorite"] else "Убрано из избранного")


@dp.callback_query(F.data.startswith("captured_state_"))
@dp.callback_query(F.data.startswith("view_state_"))
async def item_state_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    new_state = parts[2]
    item_id = int(parts[3])
    item = VaultService.set_item_state(callback.from_user.id, item_id, new_state)
    if not item:
        await callback.answer("Материал не найден", show_alert=True)
        return

    view_data = _saved_item_view_state(callback.from_user.id, item_id)
    if view_data and view_data.get("origin") == "review":
        status_messages = {
            "later": "Отложено на потом",
            "done": "Отмечено как готовое",
            "saved": "Разобрано",
            "inbox": "Возвращено во входящие",
        }
        await callback.answer(status_messages.get(new_state, "Готово"))
        await show_next_inbox_item(callback, callback.from_user.id, skip_item_id=item_id)
        return

    if callback.message:
        if callback.data.startswith("captured_state_"):
            await show_item_view(
                callback,
                callback.from_user.id,
                item_id,
                origin="all",
                page=1,
                show_media_preview=False,
            )
        else:
            existing_rows = callback.message.reply_markup.inline_keyboard if callback.message.reply_markup else []
            rows = []
            for row in existing_rows:
                updated = []
                for button in row:
                    if button.callback_data and button.callback_data.startswith("view_state_"):
                        continue
                    else:
                        updated.append(button)
                if any(button.callback_data and button.callback_data.startswith("view_state_") for button in row):
                    updated.extend(item_state_buttons(item_id, new_state))
                rows.append(updated)
            markup = InlineKeyboardMarkup(inline_keyboard=rows)
            await callback.message.edit_reply_markup(reply_markup=markup)
    status_messages = {
        "later": "Отложено на потом",
        "done": "Отмечено как готовое",
        "saved": "Возвращено в библиотеку",
        "inbox": "Перемещено во входящие",
    }
    await callback.answer(status_messages.get(new_state, "Готово"))


@dp.callback_query(F.data == "inbox_bulk_confirm")
async def inbox_bulk_confirm(callback: CallbackQuery):
    count = VaultService.count_items_by_state(callback.from_user.id, "inbox")
    if not count:
        await callback.answer("Входящие уже пусты")
        await show_state_items_page(callback, callback.from_user.id, "inbox", page=1, edit=True)
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Да, разобрать {count}", callback_data="inbox_bulk_apply")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="menu:inbox")],
    ])
    await render_menu_panel(
        callback,
        f"Разобрать все {count} материалов во «Входящих»? Они останутся в архиве и избранном.",
        reply_markup=markup,
    )
    await callback.answer()


@dp.callback_query(F.data == "inbox_bulk_apply")
async def inbox_bulk_apply(callback: CallbackQuery):
    changed = VaultService.set_all_items_by_state(callback.from_user.id, "inbox", "saved")
    await callback.answer(f"Разобрано: {changed}")
    await show_state_items_page(callback, callback.from_user.id, "inbox", page=1, edit=True)


@capture_router.message()
async def capture_incoming_message(message: Message, state: FSMContext):
    if not message.from_user:
        return
    if message.media_group_id:
        key = (message.chat.id, str(message.media_group_id))
        media_group_messages.setdefault(key, []).append(message)
        media_group_states[key] = state
        previous = media_group_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()
        media_group_tasks[key] = asyncio.create_task(_flush_media_group(key))
        return
    lock = capture_locks.setdefault(message.from_user.id, asyncio.Lock())
    async with lock:
        await _capture_incoming_message_locked(message, state)


async def _flush_media_group(key: tuple[int, str]) -> None:
    try:
        # Telegram delivers each album element as a separate update. A short
        # quiet window lets all parts arrive before we create the archive row.
        await asyncio.sleep(1.5)
        pending = media_group_messages.get(key, [])
        if not pending or not pending[0].from_user:
            return
        user_id = pending[0].from_user.id
        lock = capture_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            # If another album part arrived while waiting for this user's
            # capture lock, that part replaced this timer and will flush later.
            if media_group_tasks.get(key) is not asyncio.current_task():
                return
            messages = sorted(media_group_messages.pop(key, []), key=lambda message: message.message_id)
            state = media_group_states.pop(key, None)
            media_group_tasks.pop(key, None)
            if not messages or state is None:
                return
            await _capture_media_group_locked(messages, state)
    except asyncio.CancelledError:
        return
    except Exception:
        logging.exception("Could not collect Telegram media group %s", key[1])


async def _capture_media_group_locked(messages: list[Message], state: FSMContext) -> None:
    first = messages[0]
    if (
        not first.from_user
        or first.chat.type != "private"
        or await state.get_state() is not None
        or (first.text or "").lstrip().startswith("/")
    ):
        return

    parts = [(message, MediaService.from_message(message)) for message in messages]
    parts = [(message, payload) for message, payload in parts if payload and payload.telegram_file_id]
    if not parts:
        return

    # Telegram media groups carry their caption on one element in the normal
    # case; retain every non-empty caption if an album contains more than one.
    captions = list(dict.fromkeys(
        "\n".join((message.caption or "").strip().splitlines()).strip()
        for message, _ in parts if (message.caption or "").strip()
    ))
    representative, payload = next(
        ((message, part) for message, part in parts if (message.caption or "").strip()),
        parts[0],
    )
    if captions:
        payload.content = "\n".join(captions)

    group_files = []
    unique_keys = []
    for message, part in parts:
        file_obj = (
            message.photo[-1] if message.photo else message.video or message.document
            or message.animation or message.audio or message.voice
        )
        unique_id = getattr(file_obj, "file_unique_id", None)
        if unique_id:
            unique_keys.append(str(unique_id))
        group_files.append({
            "kind": part.kind,
            "telegram_file_id": part.telegram_file_id,
            "mime_type": part.mime_type or part.media_type,
            "file_name": part.file_name,
            "file_size": part.file_size,
            "width": part.width,
            "height": part.height,
            "duration": part.duration,
        })
    if not group_files:
        return

    if len(group_files) == 1:
        await _capture_incoming_message_locked(representative, state)
        return

    user_id = first.from_user.id
    dedupe_key = "album:" + hashlib.sha256("|".join(unique_keys).encode("utf-8")).hexdigest()
    duplicate = VaultService.find_duplicate_item(user_id, dedupe_key)
    if duplicate:
        removed = True
        for message in messages:
            try:
                await message.delete()
            except Exception:
                removed = False
                logging.warning("Could not remove duplicate album part %s", message.message_id, exc_info=True)
        await _show_duplicate_item(representative, duplicate, incoming_removed=removed)
        return

    archived_ids: list[int] = []
    item_id = None
    try:
        source_ids = [message.message_id for message in messages]
        forwarded = all(getattr(message, "forward_origin", None) for message, _ in parts)
        copy_method = bot.forward_messages if forwarded else bot.copy_messages
        try:
            copied = await copy_method(
                chat_id=first.chat.id,
                from_chat_id=first.chat.id,
                message_ids=source_ids,
            )
        except TelegramBadRequest:
            if forwarded:
                copied = await bot.copy_messages(
                    chat_id=first.chat.id,
                    from_chat_id=first.chat.id,
                    message_ids=source_ids,
                )
            else:
                raise
        archived_ids = [int(message.message_id) for message in copied]
        if not archived_ids:
            raise RuntimeError("Telegram did not return copied album message IDs")

        folder = VaultService.get_or_create_default_folder(user_id)
        item_id = _save_media_payload(
            user_id,
            int(folder["id"]),
            representative,
            payload_override=payload,
            media_group_json=json.dumps(group_files, ensure_ascii=False),
            archive_chat_id=first.chat.id,
            archive_message_id=archived_ids[0],
            archive_message_ids=json.dumps(archived_ids),
            item_state="inbox",
            dedupe_key=dedupe_key,
        )
        if not item_id:
            raise ValueError("Unsupported Telegram media group")
    except Exception:
        logging.exception("Automatic capture failed for media group %s", first.media_group_id)
        for message_id in archived_ids:
            with suppress(Exception):
                await bot.delete_message(first.chat.id, message_id)
        await render_menu_panel(
            representative,
            "⚠️ Не удалось сохранить альбом целиком. Исходные сообщения оставил на месте — попробуй ещё раз.",
            reply_markup=main_menu_keyboard(),
        )
        return

    for message_id in archived_ids:
        with suppress(Exception):
            await bot.edit_message_reply_markup(chat_id=first.chat.id, message_id=message_id, reply_markup=None)
    source_removed = True
    for message in messages:
        try:
            await message.delete()
        except Exception:
            source_removed = False
            logging.warning("Saved album %s, but source part %s remains", item_id, message.message_id, exc_info=True)
    await show_item_view(
        representative,
        user_id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )
    if not source_removed:
        logging.info("Album %s was saved, but one or more source messages remain visible", item_id)


async def _capture_incoming_message_locked(message: Message, state: FSMContext):
    if (
        not message.from_user
        or message.chat.type != "private"
        or (message.text or "").lstrip().startswith("/")
    ):
        return
    if await state.get_state() is not None:
        return

    payload = MediaService.from_message(message)
    if not payload:
        return

    dedupe_key = _capture_dedupe_key(message, payload)
    duplicate = VaultService.find_duplicate_item(message.from_user.id, dedupe_key)
    if duplicate:
        incoming_removed = False
        try:
            await message.delete()
            incoming_removed = True
        except Exception:
            logging.warning(
                "Duplicate item %s detected; could not remove incoming message %s",
                duplicate["id"],
                message.message_id,
                exc_info=True,
            )
        await _show_duplicate_item(message, duplicate, incoming_removed=incoming_removed)
        return

    archive = None
    item_id = None
    try:
        archive = await _archive_message(message, message.chat.id)
        folder = VaultService.get_or_create_default_folder(message.from_user.id)
        if payload.kind == "text":
            sender_name, sender_username, source_chat_title = _message_attribution(message)
            item_id = await _save_text_payload(
                user_id=message.from_user.id,
                folder_id=int(folder["id"]),
                text=payload.content or "",
                sender_name=sender_name,
                sender_username=sender_username,
                is_forwarded=bool(getattr(message, "forward_origin", None)),
                source_chat_title=source_chat_title,
                source_chat_id=message.chat.id,
                source_message_id=message.message_id,
                archive_chat_id=message.chat.id,
                archive_message_id=archive.message_id,
                item_state="inbox",
                dedupe_key=dedupe_key,
                link_url_hint=_message_link_url(message),
            )
        else:
            item_id = _save_media_payload(
                message.from_user.id,
                int(folder["id"]),
                message,
                archive_chat_id=message.chat.id,
                archive_message_id=archive.message_id,
                item_state="inbox",
                dedupe_key=dedupe_key,
            )
            if not item_id:
                raise ValueError("Неподдерживаемый тип сообщения")
    except Exception:
        logging.exception("Automatic capture failed for message %s", message.message_id)
        if archive:
            try:
                await bot.delete_message(message.chat.id, archive.message_id)
            except Exception:
                logging.warning("Could not remove incomplete archive copy %s", archive.message_id)
        await render_menu_panel(message, "⚠️ Не удалось сохранить материал. Исходное сообщение оставил на месте — попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return

    # Keep the permanent media copy clean. Controls live in the single menu panel.
    with suppress(Exception):
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=archive.message_id,
            reply_markup=None,
        )
    source_removed = False
    try:
        await message.delete()
        source_removed = True
    except Exception:
        logging.warning("Saved item %s, but the original message could not be deleted", item_id, exc_info=True)
    await show_item_view(
        message,
        message.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )
    if not source_removed:
        logging.info("The archived copy is safe, but source message %s remains visible", message.message_id)


@dp.callback_query(F.data.startswith("duplicate_open_"))
async def duplicate_open_handler(callback: CallbackQuery):
    item_id = int(callback.data.rsplit("_", 1)[1])
    item = VaultService.get_item(callback.from_user.id, item_id)
    if not item:
        await callback.answer("Эта запись уже удалена", show_alert=True)
        return
    await show_item_view(
        callback,
        callback.from_user.id,
        item_id,
        origin="all",
        page=1,
        show_media_preview=False,
    )


async def rss_feed_worker():
    """Poll lightweight RSS/Atom feeds without downloading linked media."""
    while True:
        try:
            for feed in RSSService.get_all():
                try:
                    _, entries = await RSSService.fetch(feed["url"])
                    RSSService.store_new_entries(int(feed["id"]), int(feed["user_id"]), entries)
                    for entry in RSSService.pending_notifications(int(feed["id"]), int(feed["user_id"])):
                        clean_summary = re.sub(r"<[^>]+>", " ", entry["summary"] or "")
                        clean_summary = " ".join(clean_summary.split())[:700]
                        text = f"📡 {feed['title']}\n\n{entry['title']}\n\n{clean_summary or entry['url']}"
                        await bot.send_message(
                            int(feed["user_id"]),
                            text[:3800],
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="💾 В архив", callback_data=f"rss_save_{entry['id']}"),
                                InlineKeyboardButton(text="🔗 Открыть", url=entry["url"]),
                            ]]),
                        )
                        RSSService.mark_notified(int(entry["id"]))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("RSS poll failed for feed_id=%s", feed["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("RSS worker iteration failed")
        await asyncio.sleep(900)


@dp.callback_query(F.data == "go_home")
async def go_home_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(callback)


async def main():
    global health_server
    init_db()

    try:
        webhook = await bot.get_webhook_info()
        if webhook.url:
            logging.warning("A webhook is configured; removing it before starting long polling")
            await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([
            BotCommand(command="start", description="Открыть личный архив"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="backup", description="Сделать резервную копию"),
            BotCommand(command="restore", description="Восстановить базу из ответа"),
            BotCommand(command="drive_backup", description="Копия базы в Google Drive"),
            BotCommand(command="drive_backups", description="Копии и восстановление из Drive"),
            BotCommand(command="status", description="Состояние бота и резервных копий"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logging.exception("Could not configure Telegram command menu")

    health_server = HealthServer(host=settings.host, port=settings.port, bot=bot, settings=settings)
    await health_server.start()

    reminder_task = asyncio.create_task(reminder_worker(bot))
    rss_task = asyncio.create_task(rss_feed_worker())
    backup_task = asyncio.create_task(
        backup_worker(
            bot,
            backup_recipients,
            interval_seconds=settings.backup_interval_hours * 3600,
        )
    )

    logging.info("Starting Telegram polling for %d allowed user IDs...", len(effective_allowed_user_ids(settings)))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        rss_task.cancel()
        backup_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task
        with suppress(asyncio.CancelledError):
            await rss_task
        with suppress(asyncio.CancelledError):
            await backup_task
        if health_server is not None:
            with suppress(Exception):
                await health_server.stop()
            health_server = None


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
