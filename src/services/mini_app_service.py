from __future__ import annotations

import hashlib
import hmac
import asyncio
import json
import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.parse import quote

import aiohttp
from aiohttp import web

from src.config import Settings
from src.db.database import DATABASE_BACKEND, get_connection
from src.services.tag_service import format_tags, merge_user_tags
from src.services.reminder_service import ReminderService
from src.services.vault_service import VaultService
from src.utils.filters import is_allowed_user, is_owner_user
from src.services.chatgpt_ocr_service import ChatGptOcrService

logger = logging.getLogger(__name__)

_EGRESS_WINDOW_SECONDS = 300
_egress_window_started = time.monotonic()
_egress_stats = {"preview_requests": 0, "preview_bytes": 0, "media_requests": 0, "media_bytes": 0}


def _record_miniapp_egress(kind: str, byte_count: int) -> None:
    global _egress_window_started
    request_key, bytes_key = (("preview_requests", "preview_bytes") if kind == "preview"
                              else ("media_requests", "media_bytes"))
    _egress_stats[request_key] += 1
    _egress_stats[bytes_key] += max(0, int(byte_count))
    now = time.monotonic()
    elapsed = now - _egress_window_started
    if elapsed >= _EGRESS_WINDOW_SECONDS:
        logger.info(
            "Mini App egress window_seconds=%s preview_requests=%s preview_bytes=%s media_requests=%s media_bytes=%s",
            int(elapsed), _egress_stats["preview_requests"], _egress_stats["preview_bytes"],
            _egress_stats["media_requests"], _egress_stats["media_bytes"],
        )
        for key in _egress_stats:
            _egress_stats[key] = 0
        _egress_window_started = now
MINI_APP_DIR = Path(__file__).resolve().parents[2] / "miniapp"
VALID_STATES = {"inbox", "later", "saved", "done"}
VALID_KINDS = {
    "photo", "text", "link", "document", "video", "animation", "sticker",
    "voice", "audio", "video_note",
}


def _media_group(row) -> list[dict]:
    try:
        value = json.loads(row["media_group_json"] or "[]")
        if isinstance(value, list):
            entries = [entry for entry in value if isinstance(entry, dict) and entry.get("telegram_file_id")]
            if entries:
                return entries
    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
        pass
    file_id = row["telegram_file_id"] if "telegram_file_id" in row.keys() else None
    if not file_id:
        return []
    return [{
        "kind": row["kind"], "telegram_file_id": file_id,
        "mime_type": row["mime_type"] if "mime_type" in row.keys() else None,
        "file_name": row["file_name"] if "file_name" in row.keys() else None,
    }]


def _is_image_media(media: dict) -> bool:
    return media.get("kind") == "photo" or (
        media.get("kind") == "document"
        and media.get("mime_type") in {"image/jpeg", "image/png", "image/webp"}
    )


class MiniAppService:
    """Authenticated HTTP API and static UI for the Telegram Mini App."""

    def __init__(self, bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    def register(self, app: web.Application) -> None:
        app.router.add_get("/miniapp", self.redirect_to_app)
        app.router.add_get("/miniapp/", self.serve_index)
        app.router.add_static("/miniapp/", str(MINI_APP_DIR), show_index=False)
        app.router.add_get("/api/miniapp/folders", self.list_folders)
        app.router.add_post("/api/miniapp/folders", self.create_folder)
        app.router.add_get("/api/miniapp/sources", self.list_sources)
        app.router.add_get("/api/miniapp/items", self.list_items)
        app.router.add_get("/api/miniapp/items/{item_id:\\d+}", self.get_item)
        app.router.add_get("/api/miniapp/items/{item_id:\\d+}/media-link", self.media_link)
        app.router.add_post("/api/miniapp/items/bulk", self.bulk_action)
        app.router.add_get("/api/miniapp/items/{item_id:\\d+}/preview", self.photo_preview)
        app.router.add_post("/api/miniapp/items/{item_id:\\d+}/ocr", self.transcribe_photo)
        app.router.add_post("/api/miniapp/items/{item_id:\\d+}/ocr/accept", self.accept_ocr)
        app.router.add_post("/api/miniapp/items/{item_id:\\d+}/ocr/cancel", self.cancel_ocr)
        app.router.add_get("/api/miniapp/media/{user_id:\\d+}/{item_id:\\d+}", self.stream_media)
        app.router.add_get("/api/miniapp/reminders", self.list_reminders)
        app.router.add_post("/api/miniapp/reminders", self.create_reminder)
        app.router.add_post("/api/miniapp/reminders/{reminder_id:\\d+}/done", self.complete_reminder)
        app.router.add_post("/api/miniapp/reminders/{reminder_id:\\d+}/snooze", self.snooze_reminder)

    async def redirect_to_app(self, request: web.Request) -> web.Response:
        raise web.HTTPFound("/miniapp/")

    async def serve_index(self, request: web.Request) -> web.Response:
        return web.FileResponse(MINI_APP_DIR / "index.html")

    def _authenticated_user_id(self, request: web.Request) -> int:
        raw = request.headers.get("X-Telegram-Init-Data", "")
        if not raw or len(raw) > 8192:
            raise web.HTTPUnauthorized(text="Открой приложение из меню бота в Telegram.")

        try:
            values = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
            received_hash = values.pop("hash")
            auth_date = int(values["auth_date"])
            user = json.loads(values["user"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise web.HTTPUnauthorized(text="Не удалось проверить авторизацию Telegram.")

        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", self.settings.bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            raise web.HTTPUnauthorized(text="Подпись Telegram не прошла проверку.")

        now = int(time.time())
        if auth_date > now + 30 or now - auth_date > 86400:
            raise web.HTTPUnauthorized(text="Сеанс Telegram устарел. Закрой и снова открой мини‑апп.")

        try:
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPUnauthorized(text="В данных Telegram нет пользователя.")
        if not is_allowed_user(user_id, self.settings):
            raise web.HTTPForbidden(text="У этого аккаунта нет доступа к архиву.")
        request["telegram_user"] = user
        return user_id

    async def list_folders(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT f.id, f.name, f.emoji, COUNT(i.id) AS item_count
                   FROM vault_folders f
                   LEFT JOIN vault_items i ON i.folder_id = f.id AND i.user_id = f.user_id
                   WHERE f.user_id = ? AND f.archived_at IS NULL
                   GROUP BY f.id ORDER BY f.id ASC""",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
        return web.json_response(
            {"folders": [dict(row) for row in rows]},
            headers={"Cache-Control": "no-store"},
        )

    async def create_folder(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="Некорректный запрос.")
        name = str(body.get("name") or "").strip()[:40] if isinstance(body, dict) else ""
        if not name:
            raise web.HTTPBadRequest(text="Введи название папки.")
        try:
            exists = next((folder for folder in VaultService.list_folders(user_id, include_archived=True)
                           if folder["name"].casefold() == name.casefold()), None)
            if exists:
                raise web.HTTPConflict(text="Папка с таким названием уже есть.")
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("Mini App folder creation failed for user %s", user_id)
            raise web.HTTPInternalServerError(text="Не удалось создать папку.")
        try:
            folder = VaultService.create_folder(user_id, name)
        except Exception:
            logger.exception("Mini App folder creation failed for user %s", user_id)
            raise web.HTTPInternalServerError(text="Не удалось создать папку.")
        return web.json_response({"folder": {**dict(folder), "item_count": 0}}, status=201,
                                 headers={"Cache-Control": "no-store"})

    async def list_sources(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        telegram_user = request.get("telegram_user", {})
        own_username = str(telegram_user.get("username") or "").casefold()
        own_name = " ".join(filter(None, [telegram_user.get("first_name"), telegram_user.get("last_name")])).casefold()
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT source_chat_title, sender_username, sender_name, link_domain,
                          COUNT(*) AS item_count, MAX(created_at) AS last_at
                   FROM vault_items WHERE user_id = ?
                   GROUP BY source_chat_title, sender_username, sender_name, link_domain""",
                (user_id,),
            ).fetchall()
        finally:
            conn.close()

        sources: dict[tuple[str, str], dict] = {}
        for row in rows:
            if row["source_chat_title"]:
                source_type, value, label = "channel", row["source_chat_title"], row["source_chat_title"]
            elif row["sender_username"] and str(row["sender_username"]).casefold() != own_username:
                source_type, value, label = "username", row["sender_username"], "@" + row["sender_username"]
            elif row["link_domain"]:
                source_type, value, label = "site", row["link_domain"], row["link_domain"]
            elif (row["sender_username"] and str(row["sender_username"]).casefold() == own_username) or (
                row["sender_name"] and str(row["sender_name"]).casefold() == own_name
            ):
                source_type = "own"
                value = row["sender_username"] or row["sender_name"]
                label = "Личные сообщения"
            elif row["sender_name"]:
                source_type, value, label = "person", row["sender_name"], row["sender_name"]
            else:
                continue
            key = (source_type, str(value))
            if key not in sources:
                sources[key] = {"type": source_type, "value": str(value), "label": str(label),
                                "item_count": 0, "last_at": row["last_at"]}
            sources[key]["item_count"] += int(row["item_count"] or 0)
            if row["last_at"] and (not sources[key]["last_at"] or row["last_at"] > sources[key]["last_at"]):
                sources[key]["last_at"] = row["last_at"]
        ordered = sorted(sources.values(), key=lambda source: (-source["item_count"], source["label"].casefold()))[:150]
        return web.json_response({"sources": ordered}, headers={"Cache-Control": "no-store"})

    async def list_reminders(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        rows = await asyncio.to_thread(ReminderService.list_reminders, user_id, include_done=False, limit=100)
        return web.json_response({"reminders": [dict(row) for row in rows]},
                                 headers={"Cache-Control": "no-store"})

    async def create_reminder(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        try:
            body = await request.json()
            item_id = int(body.get("source_item_id"))
            due_at = datetime.fromisoformat(str(body.get("due_at", "")).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="Укажи материал и корректное время напоминания.")
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        due_at = due_at.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
        now = datetime.utcnow().replace(microsecond=0)
        if due_at <= now:
            raise web.HTTPBadRequest(text="Время напоминания должно быть в будущем.")
        if (due_at - now).total_seconds() > 366 * 24 * 60 * 60:
            raise web.HTTPBadRequest(text="Можно поставить напоминание не дальше чем на год.")
        conn = get_connection()
        try:
            item = conn.execute(
                "SELECT id, kind, content, page_title, file_name, tag FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        if not item:
            raise web.HTTPNotFound(text="Материал не найден.")
        title = str(body.get("title") or item["tag"] or item["content"] or item["page_title"] or item["file_name"] or {
            "photo": "Фото", "video": "Видео", "voice": "Голосовое", "audio": "Аудио",
            "video_note": "Видеосообщение", "document": "Файл", "link": "Ссылка",
        }.get(item["kind"], "Материал")).strip()[:120]
        if not title:
            raise web.HTTPBadRequest(text="Напиши, о чём напомнить.")
        reminder = await asyncio.to_thread(ReminderService.create_reminder,
            user_id=user_id, title=title, due_at=due_at, source_item_id=item_id,
            note=f"Напоминание по материалу #{item_id}",
        )
        return web.json_response({"reminder": dict(reminder)}, status=201,
                                 headers={"Cache-Control": "no-store"})

    async def complete_reminder(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        reminder_id = int(request.match_info["reminder_id"])
        reminder = await asyncio.to_thread(ReminderService.get_reminder, user_id, reminder_id)
        if not reminder:
            raise web.HTTPNotFound(text="Напоминание не найдено.")
        updated = await asyncio.to_thread(ReminderService.mark_done, user_id, reminder_id)
        return web.json_response({"reminder": dict(updated)}, headers={"Cache-Control": "no-store"})

    async def snooze_reminder(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        reminder_id = int(request.match_info["reminder_id"])
        reminder = await asyncio.to_thread(ReminderService.get_reminder, user_id, reminder_id)
        if not reminder:
            raise web.HTTPNotFound(text="Напоминание не найдено.")
        updated = await asyncio.to_thread(ReminderService.snooze_reminder, user_id, reminder_id, 60)
        return web.json_response({"reminder": dict(updated)}, headers={"Cache-Control": "no-store"})

    async def list_items(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        query = (request.query.get("q") or "").strip()[:200]
        tag = (request.query.get("tag") or "").strip()[:64]
        kind = request.query.get("kind")
        state = request.query.get("state")
        folder_raw = request.query.get("folder")
        source_type = request.query.get("source_type", "")
        source_value = (request.query.get("source_value") or "")[:160]
        favorite = request.query.get("favorite") == "1"
        try:
            page = max(1, min(int(request.query.get("page", "1")), 100000))
        except ValueError:
            raise web.HTTPBadRequest(text="Некорректный номер страницы.")
        page_size = 24

        clauses = ["i.user_id = ?", "f.archived_at IS NULL"]
        params: list[object] = [user_id]
        if folder_raw:
            try:
                folder_id = int(folder_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="Некорректная папка.")
            clauses.append("i.folder_id = ?")
            params.append(folder_id)
        if kind and kind != "all":
            if kind not in VALID_KINDS:
                raise web.HTTPBadRequest(text="Неизвестный тип материала.")
            if kind == "link":
                clauses.append("(i.kind = 'link' OR i.link_url IS NOT NULL)")
            else:
                clauses.append("i.kind = ?")
                params.append(kind)
        if state and state != "all":
            if state not in VALID_STATES:
                raise web.HTTPBadRequest(text="Неизвестный статус.")
            clauses.append("i.item_state = ?")
            params.append(state)
        if favorite:
            clauses.append("COALESCE(i.is_favorite, 0) = 1")
        turso_backend = DATABASE_BACKEND == "turso"
        source_columns = {"channel": "source_chat_title", "username": "sender_username",
                          "person": "sender_name", "site": "link_domain"}
        if source_type:
            if source_type not in {*source_columns, "own"} or not source_value:
                raise web.HTTPBadRequest(text="Некорректный источник.")
            if not turso_backend:
                if source_type == "own":
                    telegram_user = request.get("telegram_user", {})
                    own_username = str(telegram_user.get("username") or "").casefold()
                    own_name = " ".join(filter(None, [telegram_user.get("first_name"), telegram_user.get("last_name")])).casefold()
                    clauses.append("(unicode_lower(COALESCE(i.sender_username, '')) = ? OR unicode_lower(COALESCE(i.sender_name, '')) = ?)")
                    params.extend([own_username or source_value.casefold(), own_name or source_value.casefold()])
                else:
                    clauses.append(f"unicode_lower(COALESCE(i.{source_columns[source_type]}, '')) = ?")
                    params.append(source_value.casefold())
        # SQLite supports registering a Python unicode-aware function on each
        # connection. Remote Turso connections do not; for Turso, apply these
        # text filters to the fetched rows below so Cyrillic search still works.
        if tag and not turso_backend:
            clauses.append("unicode_lower(COALESCE(i.tag, '')) LIKE ?")
            params.append(f"%{tag.casefold().lstrip('#')}%")
        if query and not turso_backend:
            like = f"%{query.casefold()}%"
            clauses.append("(" + " OR ".join([
                "unicode_lower(COALESCE(i.content, '')) LIKE ?",
                "unicode_lower(COALESCE(i.context_text, '')) LIKE ?",
                "unicode_lower(COALESCE(i.ocr_text, '')) LIKE ?",
                "unicode_lower(COALESCE(i.tag, '')) LIKE ?",
                "unicode_lower(COALESCE(i.file_name, '')) LIKE ?",
                "unicode_lower(COALESCE(i.sender_name, '')) LIKE ?",
                "unicode_lower(COALESCE(i.source_chat_title, '')) LIKE ?",
                "unicode_lower(COALESCE(i.link_url, '')) LIKE ?",
                "unicode_lower(COALESCE(i.page_title, '')) LIKE ?",
                "unicode_lower(COALESCE(f.name, '')) LIKE ?",
            ]) + ")")
            params.extend([like] * 10)

        where = " AND ".join(clauses)
        conn = get_connection()
        if not turso_backend:
            conn.create_function("unicode_lower", 1, lambda value: str(value or "").casefold())
        try:
            select_sql = f"""SELECT i.id, i.kind, i.content, i.tag, i.manual_tag, i.file_name,
                               i.file_size, i.media_type, i.sender_name, i.source_chat_title, i.created_at,
                               i.sender_username, i.is_forwarded, i.link_domain, i.mime_type, i.duration, i.width, i.height,
                               i.item_state, i.is_favorite, i.context_text, i.telegram_file_id,
                               i.media_group_json, i.ocr_text, i.ocr_pending_text, i.ocr_pending_image_index,
                               i.link_url, i.page_title, f.name AS folder_name, f.emoji AS folder_emoji
                        FROM vault_items i JOIN vault_folders f ON f.id = i.folder_id
                        WHERE {where} ORDER BY i.id DESC"""
            if turso_backend:
                rows = conn.execute(select_sql, params).fetchall()
                if source_type:
                    source_needle = source_value.casefold()
                    if source_type == "own":
                        telegram_user = request.get("telegram_user", {})
                        own_username = str(telegram_user.get("username") or source_value).casefold()
                        own_name = " ".join(filter(None, [telegram_user.get("first_name"), telegram_user.get("last_name")])).casefold()
                        rows = [row for row in rows if str(row["sender_username"] or "").casefold() == own_username or str(row["sender_name"] or "").casefold() == own_name]
                    else:
                        field = source_columns[source_type]
                        rows = [row for row in rows if str(row[field] or "").casefold() == source_needle]
                if tag:
                    tag_needle = tag.casefold().lstrip("#")
                    rows = [row for row in rows if tag_needle in str(row["tag"] or "").casefold()]
                if query:
                    query_needle = query.casefold()
                    search_fields = (
                        "content", "context_text", "ocr_text", "tag", "file_name", "sender_name",
                        "source_chat_title", "link_url", "page_title", "folder_name",
                    )
                    rows = [
                        row for row in rows
                        if any(query_needle in str(row[field] or "").casefold() for field in search_fields)
                    ]
                total = len(rows)
                offset = (page - 1) * page_size
                rows = rows[offset:offset + page_size]
            else:
                total = int(conn.execute(
                    f"SELECT COUNT(*) AS n FROM vault_items i JOIN vault_folders f ON f.id = i.folder_id WHERE {where}",
                    params,
                ).fetchone()["n"])
                rows = conn.execute(
                    select_sql + " LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                ).fetchall()
        finally:
            conn.close()

        items = []
        for row in rows:
            item = dict(row)
            media_group = _media_group(item)
            has_photo = any(_is_image_media(media) for media in media_group)
            item["has_media"] = bool(media_group)
            item.pop("telegram_file_id", None)
            item.pop("media_group_json", None)
            item["content"] = (item["content"] or "")[:1800]
            item["context_text"] = (item["context_text"] or "")[:1200]
            item["ocr_text"] = (item.get("ocr_text") or "")[:6000]
            item["ocr_pending_text"] = (item.get("ocr_pending_text") or "")[:6000]
            item["is_favorite"] = bool(item["is_favorite"])
            item["preview_url"] = f"/api/miniapp/items/{item['id']}/preview" if has_photo else None
            item["media_count"] = len(media_group)
            item["media_items"] = [
                {"index": index, "kind": media["kind"], "mime_type": media.get("mime_type"), "file_name": media.get("file_name"),
                 "preview_url": f"/api/miniapp/items/{item['id']}/preview?index={index}" if _is_image_media(media) else None}
                for index, media in enumerate(media_group)
            ] if len(media_group) > 1 else []
            is_image = has_photo or (item["kind"] == "document" and item.get("mime_type") in {"image/jpeg", "image/png", "image/webp"})
            item["ocr_available"] = bool(is_image and is_owner_user(user_id, self.settings) and self.settings.chatgpt_ocr_url and self.settings.chatgpt_ocr_token)
            if item["kind"] == "voice" and media_group:
                item["media_url"] = self._signed_media_url(user_id, int(item["id"]), 0)
            items.append(item)
        return web.json_response(
            {"items": items, "total": total, "page": page, "page_size": page_size},
            headers={"Cache-Control": "no-store"},
        )

    async def get_item(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        item_id = int(request.match_info["item_id"])
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT i.id, i.kind, i.content, i.tag, i.manual_tag, i.file_name,
                          i.file_size, i.media_type, i.mime_type, i.duration, i.width, i.height,
                          i.sender_name, i.sender_username, i.is_forwarded, i.source_chat_title, i.created_at,
                          i.item_state, i.is_favorite, i.context_text, i.telegram_file_id, i.media_group_json,
                          i.ocr_text, i.ocr_pending_text, i.ocr_pending_image_index,
                          i.link_url, i.page_title, i.link_domain,
                          f.name AS folder_name, f.emoji AS folder_emoji
                   FROM vault_items i JOIN vault_folders f ON f.id = i.folder_id
                   WHERE i.user_id = ? AND i.id = ? AND f.archived_at IS NULL""",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise web.HTTPNotFound(text="Материал не найден.")
        item = dict(row)
        media_group = _media_group(item)
        has_photo = any(_is_image_media(media) for media in media_group)
        item["has_media"] = bool(media_group)
        item["preview_url"] = f"/api/miniapp/items/{item_id}/preview" if has_photo else None
        item["media_count"] = len(media_group)
        item["media_items"] = [
            {"index": index, "kind": media["kind"], "mime_type": media.get("mime_type"), "file_name": media.get("file_name"),
             "preview_url": f"/api/miniapp/items/{item_id}/preview?index={index}" if _is_image_media(media) else None}
            for index, media in enumerate(media_group)
        ] if len(media_group) > 1 else []
        if item["kind"] in {"voice", "audio"} and media_group:
            item["media_url"] = self._signed_media_url(user_id, item_id, 0)
        is_image = has_photo or (item["kind"] == "document" and item.get("mime_type") in {"image/jpeg", "image/png", "image/webp"})
        item["ocr_available"] = bool(is_image and is_owner_user(user_id, self.settings) and self.settings.chatgpt_ocr_url and self.settings.chatgpt_ocr_token)
        item["content"] = (item["content"] or "")[:1800]
        item["context_text"] = (item["context_text"] or "")[:1200]
        item["ocr_text"] = (item.get("ocr_text") or "")[:6000]
        item["ocr_pending_text"] = (item.get("ocr_pending_text") or "")[:6000]
        item["is_favorite"] = bool(item["is_favorite"])
        item.pop("telegram_file_id", None)
        item.pop("media_group_json", None)
        return web.json_response({"item": item}, headers={"Cache-Control": "no-store"})

    async def bulk_action(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="Некорректный запрос.")
        ids = body.get("ids") if isinstance(body, dict) else None
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            raise web.HTTPBadRequest(text="Выбери от 1 до 100 материалов.")
        try:
            item_ids = list(dict.fromkeys(int(value) for value in ids))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="В списке есть некорректный материал.")

        conn = get_connection()
        telegram_messages_total = 0
        telegram_messages_removed = 0
        try:
            placeholders = ",".join("?" for _ in item_ids)
            rows = conn.execute(
                f"""SELECT id, folder_id, tag, manual_tag, archive_chat_id,
                          archive_message_id, archive_message_ids, context_message_id
                   FROM vault_items WHERE user_id = ? AND id IN ({placeholders})""",
                [user_id, *item_ids],
            ).fetchall()
            if len(rows) != len(item_ids):
                raise web.HTTPNotFound(text="Часть выбранных материалов больше недоступна.")

            if action == "move":
                try:
                    folder_id = int(body.get("folder_id"))
                except (TypeError, ValueError):
                    raise web.HTTPBadRequest(text="Выбери папку.")
                folder = conn.execute(
                    "SELECT id FROM vault_folders WHERE id = ? AND user_id = ? AND archived_at IS NULL",
                    (folder_id, user_id),
                ).fetchone()
                if not folder:
                    raise web.HTTPBadRequest(text="Папка недоступна.")
                conn.execute(
                    f"UPDATE vault_items SET folder_id = ? WHERE user_id = ? AND id IN ({placeholders})",
                    [folder_id, user_id, *item_ids],
                )
            elif action == "state":
                state = body.get("state")
                if state not in VALID_STATES:
                    raise web.HTTPBadRequest(text="Неизвестный статус.")
                conn.execute(
                    f"UPDATE vault_items SET item_state = ? WHERE user_id = ? AND id IN ({placeholders})",
                    [state, user_id, *item_ids],
                )
            elif action == "favorite":
                value = 1 if body.get("favorite") else 0
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) if value else None
                conn.execute(
                    f"UPDATE vault_items SET is_favorite = ?, favorited_at = ? WHERE user_id = ? AND id IN ({placeholders})",
                    [value, stamp, user_id, *item_ids],
                )
            elif action == "delete":
                if len(item_ids) > 25:
                    raise web.HTTPBadRequest(text="Для удаления можно выбрать не больше 25 материалов за раз.")
                conn.execute(
                    f"DELETE FROM reminders WHERE user_id = ? AND source_item_id IN ({placeholders})",
                    [user_id, *item_ids],
                )
                conn.execute(
                    f"DELETE FROM vault_items WHERE user_id = ? AND id IN ({placeholders})",
                    [user_id, *item_ids],
                )
            elif action == "tag":
                tag = format_tags(str(body.get("tag") or "").strip()[:64])
                if not tag:
                    raise web.HTTPBadRequest(text="Введи тег минимум из одного символа.")
                mode = body.get("mode", "add")
                if mode not in {"add", "replace"}:
                    raise web.HTTPBadRequest(text="Неизвестный режим работы с тегом.")
                for row in rows:
                    if mode == "replace":
                        manual = tag
                        merged = tag
                    else:
                        manual = merge_user_tags(row["manual_tag"], [tag])
                        merged = merge_user_tags(row["tag"], [tag])
                    conn.execute(
                        "UPDATE vault_items SET tag = ?, manual_tag = ? WHERE user_id = ? AND id = ?",
                        (merged, manual, user_id, row["id"]),
                    )
            else:
                raise web.HTTPBadRequest(text="Неизвестное групповое действие.")
            conn.commit()
        except web.HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            logger.exception("Mini App bulk action failed for user %s", user_id)
            raise web.HTTPInternalServerError(text="Не удалось обновить материалы.")
        finally:
            conn.close()
        if action == "delete":
            for row in rows:
                chat_id = int(row["archive_chat_id"] or user_id)
                try:
                    archive_ids = [int(value) for value in json.loads(row["archive_message_ids"] or "[]")]
                except (TypeError, ValueError, json.JSONDecodeError):
                    archive_ids = []
                if not archive_ids and row["archive_message_id"]:
                    archive_ids = [int(row["archive_message_id"])]
                for message_id in ([row["context_message_id"]] if row["context_message_id"] else []) + archive_ids:
                    if not message_id:
                        continue
                    telegram_messages_total += 1
                    try:
                        await self.bot.delete_message(chat_id, int(message_id))
                        telegram_messages_removed += 1
                    except Exception as exc:
                        logger.info(
                            "Mini App could not delete Telegram message %s for item %s (%s)",
                            message_id,
                            row["id"],
                            type(exc).__name__,
                        )
        result = {"updated": len(item_ids)}
        if action == "delete":
            result["telegram_messages_total"] = telegram_messages_total
            result["telegram_messages_removed"] = telegram_messages_removed
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    async def photo_preview(self, request: web.Request) -> web.StreamResponse:
        user_id = self._authenticated_user_id(request)
        item_id = int(request.match_info["item_id"])
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT telegram_file_id, media_group_json, kind, mime_type FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        try:
            index = max(0, min(int(request.query.get("index", "0")), 9))
        except ValueError:
            raise web.HTTPBadRequest(text="Номер фото некорректен.")
        media_group = _media_group(row) if row else []
        if not row or index >= len(media_group) or not _is_image_media(media_group[index]):
            raise web.HTTPNotFound(text="Фото не найдено.")

        try:
            file_id = str(media_group[index]["telegram_file_id"])
            etag = '"' + hashlib.sha256(file_id.encode("utf-8")).hexdigest() + '"'
            cache_headers = {
                "Cache-Control": "private, max-age=300, must-revalidate",
                "ETag": etag,
                "Vary": "X-Telegram-Init-Data",
                "X-Content-Type-Options": "nosniff",
            }
            if request.headers.get("If-None-Match") == etag:
                _record_miniapp_egress("preview", 0)
                return web.Response(status=304, headers=cache_headers)
            telegram_file = await self.bot.get_file(media_group[index]["telegram_file_id"])
            if not telegram_file.file_path:
                raise RuntimeError("Telegram returned no file path")
            url = f"https://api.telegram.org/file/bot{self.settings.bot_token}/{telegram_file.file_path}"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(url) as upstream:
                    if upstream.status != 200:
                        raise RuntimeError(f"Telegram file server returned {upstream.status}")
                    response = web.StreamResponse(
                        status=200,
                        headers={
                            "Content-Type": upstream.headers.get("Content-Type", "image/jpeg"),
                            **cache_headers,
                        },
                    )
                    await response.prepare(request)
                    transferred = 0
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                        transferred += len(chunk)
                    await response.write_eof()
                    _record_miniapp_egress("preview", transferred)
                    return response
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Mini App photo preview failed for user=%s item=%s (%s)",
                user_id,
                item_id,
                type(exc).__name__,
            )
            raise web.HTTPBadGateway(text="Не удалось загрузить фото из Telegram.")

    async def transcribe_photo(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        if not is_owner_user(user_id, self.settings):
            raise web.HTTPForbidden(text="Распознавание доступно только владельцу бота.")
        item_id = int(request.match_info["item_id"])
        try:
            body = await request.json()
            image_index = int(body.get("index", 0)) if isinstance(body, dict) else 0
        except (ValueError, TypeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="Некорректный номер картинки.")
        if image_index < 0:
            raise web.HTTPBadRequest(text="Некорректный номер картинки.")
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT telegram_file_id, media_group_json, kind, mime_type, file_name FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        media_group = _media_group(row) if row else []
        if not row or image_index >= len(media_group):
            raise web.HTTPNotFound(text="Фото не найдено.")
        media = media_group[image_index]
        is_image = media.get("kind") == "photo" or (media.get("kind") == "document" and media.get("mime_type") in {"image/jpeg", "image/png", "image/webp"})
        if not is_image:
            raise web.HTTPNotFound(text="В этом материале нет выбранного фото.")
        try:
            text = await ChatGptOcrService.transcribe_file(self.bot, self.settings, user_id, media["telegram_file_id"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        except RuntimeError as exc:
            raise web.HTTPBadGateway(text=str(exc))
        except Exception as exc:
            logger.warning("Mini App OCR failed for user=%s item=%s (%s)", user_id, item_id, type(exc).__name__)
            raise web.HTTPBadGateway(text="Не удалось распознать текст. Попробуй ещё раз.")
        if not VaultService.propose_item_ocr(user_id, item_id, text, image_index):
            raise web.HTTPNotFound(text="Материал не найден.")
        return web.json_response({"text": text, "image_index": image_index}, headers={"Cache-Control": "no-store"})

    async def accept_ocr(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        if not is_owner_user(user_id, self.settings):
            raise web.HTTPForbidden(text="Доступно только владельцу бота.")
        item_id = int(request.match_info["item_id"])
        item = VaultService.accept_item_ocr(user_id, item_id)
        if not item:
            raise web.HTTPNotFound(text="Предложение OCR уже закрыто или материал удалён.")
        return web.json_response({"ocr_text": item["ocr_text"] or ""}, headers={"Cache-Control": "no-store"})

    async def cancel_ocr(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        if not is_owner_user(user_id, self.settings):
            raise web.HTTPForbidden(text="Доступно только владельцу бота.")
        item_id = int(request.match_info["item_id"])
        item = VaultService.cancel_item_ocr(user_id, item_id)
        if not item:
            raise web.HTTPNotFound(text="Предложение OCR уже закрыто или материал удалён.")
        return web.json_response({"ok": True}, headers={"Cache-Control": "no-store"})

    def _signed_media_url(self, user_id: int, item_id: int, index: int = 0) -> str:
        expires = int(time.time()) + 900
        payload = f"{user_id}:{item_id}:{index}:{expires}".encode()
        signature = hmac.new(self.settings.bot_token.encode(), payload, hashlib.sha256).hexdigest()
        return f"/api/miniapp/media/{user_id}/{item_id}?index={index}&expires={expires}&signature={signature}"

    async def media_link(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        item_id = int(request.match_info["item_id"])
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT telegram_file_id, media_group_json, kind, mime_type, file_name FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        media_group = _media_group(row) if row else []
        if not row or not media_group:
            raise web.HTTPNotFound(text="Файл у этого материала не найден.")
        urls = [self._signed_media_url(user_id, item_id, index) for index in range(len(media_group))]
        return web.json_response({"url": urls[0], "urls": urls}, headers={"Cache-Control": "no-store"})

    async def stream_media(self, request: web.Request) -> web.StreamResponse:
        try:
            user_id = int(request.match_info["user_id"])
            item_id = int(request.match_info["item_id"])
            expires = int(request.query["expires"])
            signature = request.query["signature"]
            index = max(0, int(request.query.get("index", "0")))
        except (ValueError, KeyError):
            raise web.HTTPUnauthorized(text="Ссылка на файл некорректна.")
        now = int(time.time())
        if expires < now or expires > now + 900:
            raise web.HTTPUnauthorized(text="Ссылка на файл устарела. Открой материал заново.")
        payload = f"{user_id}:{item_id}:{index}:{expires}".encode()
        expected = hmac.new(self.settings.bot_token.encode(), payload, hashlib.sha256).hexdigest()
        # Accept previously issued single-file links for the rest of their short TTL.
        legacy_payload = f"{user_id}:{item_id}:{expires}".encode()
        legacy_expected = hmac.new(self.settings.bot_token.encode(), legacy_payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature) and not ("index" not in request.query and hmac.compare_digest(legacy_expected, signature)):
            raise web.HTTPUnauthorized(text="Ссылка на файл недействительна.")

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT telegram_file_id, media_group_json, kind, mime_type, file_name FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        media_group = _media_group(row) if row else []
        if not row or index >= len(media_group):
            raise web.HTTPNotFound(text="Файл не найден.")

        try:
            media_file = media_group[index]
            telegram_file = await self.bot.get_file(media_file["telegram_file_id"])
            if not telegram_file.file_path:
                raise RuntimeError("Telegram returned no file path")
            url = f"https://api.telegram.org/file/bot{self.settings.bot_token}/{telegram_file.file_path}"
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=60)
            upstream_headers = {"Range": request.headers["Range"]} if request.headers.get("Range") else {}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=upstream_headers) as upstream:
                    if upstream.status not in {200, 206}:
                        raise RuntimeError(f"Telegram file server returned {upstream.status}")
                    content_type = upstream.headers.get("Content-Type") or ""
                    if not content_type or content_type.split(";", 1)[0].lower() == "application/octet-stream":
                        content_type = media_file.get("mime_type") or row["mime_type"] or mimetypes.guess_type(telegram_file.file_path)[0] or content_type
                    if row["kind"] == "voice" and content_type.split(";", 1)[0].lower() not in {"audio/ogg", "audio/opus"}:
                        content_type = "audio/ogg; codecs=opus"
                    content_type = content_type or "application/octet-stream"
                    inline_types = {
                        "application/pdf", "audio/aac", "audio/mp4", "audio/mpeg", "audio/ogg",
                        "audio/wav", "audio/webm", "image/gif", "image/jpeg", "image/png",
                        "image/webp", "video/mp4", "video/quicktime", "video/webm",
                    }
                    filename = str(row["file_name"] or Path(telegram_file.file_path).name or f"material-{item_id}")
                    disposition = "inline" if content_type.split(";", 1)[0].lower() in inline_types else "attachment"
                    headers = {
                        "Content-Type": content_type,
                        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
                        "Cache-Control": "private, no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Accept-Ranges": upstream.headers.get("Accept-Ranges", "bytes"),
                    }
                    for name in ("Content-Length", "Content-Range"):
                        if upstream.headers.get(name):
                            headers[name] = upstream.headers[name]
                    response = web.StreamResponse(status=upstream.status, headers=headers)
                    await response.prepare(request)
                    transferred = 0
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                        transferred += len(chunk)
                    await response.write_eof()
                    _record_miniapp_egress("media", transferred)
                    return response
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Mini App media stream failed for user=%s item=%s (%s)",
                user_id,
                item_id,
                type(exc).__name__,
            )
            raise web.HTTPBadGateway(text="Не удалось открыть файл из Telegram.")
