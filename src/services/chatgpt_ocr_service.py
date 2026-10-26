from __future__ import annotations

import base64
from io import BytesIO

import aiohttp

from src.config import Settings

MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Файл не распознан как изображение JPEG, PNG или WebP.")


class ChatGptOcrService:
    @staticmethod
    async def transcribe_file(bot, settings: Settings, user_id: int, file_id: str) -> str:
        if not settings.chatgpt_ocr_url or not settings.chatgpt_ocr_token:
            raise RuntimeError("OCR пока не настроен.")
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            raise RuntimeError("Telegram не вернул путь к изображению.")
        if telegram_file.file_size and telegram_file.file_size > MAX_IMAGE_BYTES:
            raise ValueError("Размер изображения должен быть не больше 15 МБ.")
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        image = buffer.getvalue()
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ValueError("Размер изображения должен быть не больше 15 МБ.")
        mime_type = _image_mime(image)
        timeout = aiohttp.ClientTimeout(total=240, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.chatgpt_ocr_url,
                headers={"Authorization": f"Bearer {settings.chatgpt_ocr_token}"},
                json={"user_id": user_id, "mime_type": mime_type, "image_base64": base64.b64encode(image).decode("ascii")},
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = {}
                if response.status != 200:
                    message = str(payload.get("error") or "ChatGPT не смог распознать изображение.")
                    raise RuntimeError(message[:500])
                text = str(payload.get("text") or "").strip()
                if not text:
                    raise RuntimeError("ChatGPT вернул пустой результат.")
                return text
