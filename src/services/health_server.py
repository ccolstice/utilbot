from __future__ import annotations

import logging
from aiohttp import web

logger = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()

        async def root(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "service": "tg-util-bot"})

        async def health(_: web.Request) -> web.Response:
            return web.json_response({"status": "healthy"})

        app.router.add_get("/", root)
        app.router.add_get("/health", health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=self.port)
        await site.start()
        logger.info("Health server started on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
