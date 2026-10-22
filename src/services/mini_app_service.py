from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from src.config import Settings
from src.db.database import DATABASE_BACKEND, get_connection
from src.services.tag_service import format_tags, merge_user_tags
from src.services.vault_service import VaultService
from src.utils.filters import is_allowed_user

logger = logging.getLogger(__name__)
MINI_APP_DIR = Path(__file__).resolve().parents[2] / "miniapp"
VALID_STATES = {"inbox", "later", "saved", "done"}
VALID_KINDS = {
    "photo", "text", "link", "document", "video", "animation", "sticker",
    "voice", "audio", "video_note",
}


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
        app.router.add_get("/api/miniapp/items", self.list_items)
        app.router.add_post("/api/miniapp/items/bulk", self.bulk_action)
        app.router.add_get("/api/miniapp/items/{item_id:\\d+}/preview", self.photo_preview)

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

    async def list_items(self, request: web.Request) -> web.Response:
        user_id = self._authenticated_user_id(request)
        query = (request.query.get("q") or "").strip()[:200]
        tag = (request.query.get("tag") or "").strip()[:64]
        kind = request.query.get("kind")
        state = request.query.get("state")
        folder_raw = request.query.get("folder")
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
        # SQLite supports registering a Python unicode-aware function on each
        # connection. Remote Turso connections do not; for Turso, apply these
        # text filters to the fetched rows below so Cyrillic search still works.
        turso_backend = DATABASE_BACKEND == "turso"
        if tag and not turso_backend:
            clauses.append("unicode_lower(COALESCE(i.tag, '')) LIKE ?")
            params.append(f"%{tag.casefold().lstrip('#')}%")
        if query and not turso_backend:
            like = f"%{query.casefold()}%"
            clauses.append("(" + " OR ".join([
                "unicode_lower(COALESCE(i.content, '')) LIKE ?",
                "unicode_lower(COALESCE(i.context_text, '')) LIKE ?",
                "unicode_lower(COALESCE(i.tag, '')) LIKE ?",
                "unicode_lower(COALESCE(i.file_name, '')) LIKE ?",
                "unicode_lower(COALESCE(i.sender_name, '')) LIKE ?",
                "unicode_lower(COALESCE(i.source_chat_title, '')) LIKE ?",
                "unicode_lower(COALESCE(i.link_url, '')) LIKE ?",
                "unicode_lower(COALESCE(i.page_title, '')) LIKE ?",
                "unicode_lower(COALESCE(f.name, '')) LIKE ?",
            ]) + ")")
            params.extend([like] * 9)

        where = " AND ".join(clauses)
        conn = get_connection()
        if not turso_backend:
            conn.create_function("unicode_lower", 1, lambda value: str(value or "").casefold())
        try:
            select_sql = f"""SELECT i.id, i.kind, i.content, i.tag, i.manual_tag, i.file_name,
                               i.file_size, i.media_type, i.sender_name, i.source_chat_title, i.created_at,
                               i.item_state, i.is_favorite, i.context_text, i.telegram_file_id,
                               i.link_url, i.page_title, f.name AS folder_name, f.emoji AS folder_emoji
                        FROM vault_items i JOIN vault_folders f ON f.id = i.folder_id
                        WHERE {where} ORDER BY i.id DESC"""
            if turso_backend:
                rows = conn.execute(select_sql, params).fetchall()
                if tag:
                    tag_needle = tag.casefold().lstrip("#")
                    rows = [row for row in rows if tag_needle in str(row["tag"] or "").casefold()]
                if query:
                    query_needle = query.casefold()
                    search_fields = (
                        "content", "context_text", "tag", "file_name", "sender_name",
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
            has_photo = item["kind"] == "photo" and bool(item["telegram_file_id"])
            item.pop("telegram_file_id", None)
            item["content"] = (item["content"] or "")[:1800]
            item["context_text"] = (item["context_text"] or "")[:1200]
            item["is_favorite"] = bool(item["is_favorite"])
            item["preview_url"] = f"/api/miniapp/items/{item['id']}/preview" if has_photo else None
            items.append(item)
        return web.json_response(
            {"items": items, "total": total, "page": page, "page_size": page_size},
            headers={"Cache-Control": "no-store"},
        )

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
                          archive_message_id, context_message_id
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
                for message_id in (row["context_message_id"], row["archive_message_id"]):
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
                "SELECT telegram_file_id, kind FROM vault_items WHERE user_id = ? AND id = ?",
                (user_id, item_id),
            ).fetchone()
        finally:
            conn.close()
        if not row or row["kind"] != "photo" or not row["telegram_file_id"]:
            raise web.HTTPNotFound(text="Фото не найдено.")

        try:
            telegram_file = await self.bot.get_file(row["telegram_file_id"])
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
                            "Cache-Control": "no-store",
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                    await response.prepare(request)
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await response.write(chunk)
                    await response.write_eof()
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
