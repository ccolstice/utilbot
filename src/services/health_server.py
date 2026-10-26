from __future__ import annotations

import logging
from aiohttp import web

from src.services.mini_app_service import MiniAppService

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
