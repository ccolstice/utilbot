from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import aiohttp


class GoogleDriveService:
    """Client for the owner's Google Apps Script Drive backup bridge."""

    @classmethod
    def configured(cls) -> bool:
        return all(os.getenv(name, "").strip() for name in (
            "GDRIVE_WEBAPP_URL", "GDRIVE_SHARED_SECRET"
        ))

    @classmethod
    async def _request(cls, action: str, **fields) -> dict:
        if not cls.configured():
            raise RuntimeError(
                "Google Drive ещё не настроен: нужны GDRIVE_WEBAPP_URL и GDRIVE_SHARED_SECRET."
            )

        body = {
            "secret": os.environ["GDRIVE_SHARED_SECRET"].strip(),
            "action": action,
            **fields,
        }
        timeout = aiohttp.ClientTimeout(total=240)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                os.environ["GDRIVE_WEBAPP_URL"].strip(), json=body
            ) as response:
                raw = await response.read()
                if response.status >= 400:
                    raise RuntimeError(
                        f"Apps Script ответил HTTP {response.status}: "
                        f"{raw[:500].decode('utf-8', 'replace')}"
                    )
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Apps Script вернул не JSON. Проверь адрес опубликованного веб-приложения."
            ) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("error", "неизвестная ошибка") if isinstance(result, dict) else "неверный ответ"
            raise RuntimeError(f"Google Drive: {detail}")
        return result

    @classmethod
    async def upload_snapshot(cls, path: Path, *, item_count: int) -> dict:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        return await cls._request(
            "upload",
            name=path.name,
            data=payload,
            itemCount=int(item_count),
        )

    @classmethod
    async def list_snapshots(cls, limit: int = 10) -> list[dict]:
        result = await cls._request("list", limit=max(1, min(int(limit), 50)))
        return result.get("files", [])

    @classmethod
    async def download_snapshot(cls, file_id: str, destination: Path) -> None:
        result = await cls._request("download", fileId=file_id)
        try:
            payload = base64.b64decode(result["data"], validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Google Drive вернул повреждённую копию.") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
