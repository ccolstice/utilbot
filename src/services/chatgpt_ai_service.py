from __future__ import annotations

import base64
from io import BytesIO

import aiohttp

from src.config import Settings

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _image_mime(data: bytes, declared_type: str = "") -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    declared_type = declared_type.split(";", 1)[0].strip().lower()
    if declared_type in {"image/jpeg", "image/png", "image/webp"}:
        return declared_type
    raise ValueError("Файл не распознан как изображение JPEG, PNG или WebP.")


class ChatGptAiService:
    @staticmethod
    async def process_item(bot, settings: Settings, user_id: int, item) -> dict:
        if not settings.chatgpt_ai_url or not settings.chatgpt_ai_token:
            raise RuntimeError("AI-обработка не настроена.")

        item = dict(item)
        record = {
            "kind": str(item.get("kind") or ""),
            "title": str(item.get("page_title") or item.get("file_name") or "")[:500],
            "content": "\n\n".join(
                text for text in (
                    str(item.get("content") or "").strip(),
                    str(item.get("transcript_text") or "").strip(),
                    str(item.get("context_text") or "").strip(),
                ) if text
            )[:20_000],
            "source": str(item.get("source_chat_title") or item.get("sender_name") or "")[:500],
            "url": str(item.get("link_url") or "")[:1500],
        }

        image_kinds = item.get("kind") == "photo" or (
            item.get("kind") == "document"
            and item.get("mime_type") in {"image/jpeg", "image/png", "image/webp"}
        )
        file_id = item.get("telegram_file_id")
        if image_kinds and file_id:
            telegram_file = await bot.get_file(file_id)
            if telegram_file.file_size and telegram_file.file_size > MAX_IMAGE_BYTES:
                raise ValueError("Размер изображения должен быть не больше 15 МБ.")
            buffer = BytesIO()
            await bot.download_file(telegram_file.file_path, destination=buffer)
            image = buffer.getvalue()
            if not image or len(image) > MAX_IMAGE_BYTES:
                raise ValueError("Размер изображения должен быть не больше 15 МБ.")
            record["mime_type"] = _image_mime(image, str(item.get("mime_type") or ""))
            record["image_base64"] = base64.b64encode(image).decode("ascii")

        timeout = aiohttp.ClientTimeout(total=240, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.chatgpt_ai_url,
                headers={"Authorization": f"Bearer {settings.chatgpt_ai_token}"},
                json={"user_id": user_id, "record": record},
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = {}
                if response.status != 200:
                    message = str(payload.get("error") or "ChatGPT не смог обработать запись.")
                    raise RuntimeError(message[:500])
                description = str(payload.get("description") or "").strip()
                tags = payload.get("tags")
                if not description or not isinstance(tags, list):
                    raise RuntimeError("ChatGPT вернул некорректный результат.")
                return {"description": description[:1200], "tags": [str(tag)[:40] for tag in tags[:8]]}
