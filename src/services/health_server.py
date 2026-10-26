from __future__ import annotations

import asyncio
import logging
import hmac
from aiohttp import web

from src.services.mini_app_service import MiniAppService
from src.services.search_service import SearchService
from src.utils.filters import effective_owner_user_ids

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080, *, bot=None, settings=None):
        self.host = host
        self.port = port
        self.bot = bot
        self.settings = settings
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=8 * 1024)

        async def root(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "service": "tg-util-bot"})

        async def health(_: web.Request) -> web.Response:
            return web.json_response({"status": "healthy"})

        app.router.add_get("/", root)
        app.router.add_get("/health", health)
        if self.settings is not None and self.settings.archive_api_token:
            async def archive_search(request: web.Request) -> web.Response:
                expected = f"Bearer {self.settings.archive_api_token}"
                supplied = request.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied, expected):
                    return web.json_response({"error": "unauthorized"}, status=401)
                try:
                    body = await request.json()
                    if not isinstance(body, dict):
                        return web.json_response({"error": "invalid request"}, status=400)
                    user_id = int(body.get("user_id", 0))
                    query = str(body.get("query", "")).strip()
                    limit = max(1, min(int(body.get("limit", 6)), 8))
                except (ValueError, TypeError):
                    return web.json_response({"error": "invalid request"}, status=400)
                if user_id not in effective_owner_user_ids(self.settings):
                    return web.json_response({"error": "forbidden"}, status=403)
                if not query or len(query) > 500:
                    return web.json_response({"error": "query must contain 1-500 characters"}, status=400)

                try:
                    rows = await asyncio.to_thread(
                        SearchService.search,
                        user_id,
                        query,
                        include_archived=True,
                        deep=True,
                        limit=limit,
                    )
                except Exception:
                    logger.exception("Internal archive search failed")
                    return web.json_response({"error": "archive search failed"}, status=500)

                items = []
                for row in rows:
                    row = dict(row)
                    content = "\n".join(
                        str(row[key]).strip()
                        for key in ("content", "context_text", "transcript_text")
                        if row.get(key)
                    )
                    items.append({
                        "id": int(row["id"]),
                        "kind": str(row.get("kind") or ""),
                        "title": str(row.get("page_title") or row.get("file_name") or row.get("kind") or "Запись"),
                        "content": content[:2500],
                        "tag": str(row.get("tag") or ""),
                        "created_at": str(row.get("created_at") or ""),
                        "source": str(row.get("source_chat_title") or row.get("sender_name") or ""),
                        "url": str(row.get("link_url") or "")[:1000],
                    })
                return web.json_response({"items": items})

            app.router.add_post("/internal/archive/search", archive_search)
        if self.bot is not None and self.settings is not None:
            MiniAppService(self.bot, self.settings).register(app)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await site.start()
        logger.info("Health server started on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
