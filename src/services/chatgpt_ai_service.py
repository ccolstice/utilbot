from __future__ import annotations

import base64
import json
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
    async def generate_secretary_reply(settings: Settings, user_id: int, mode: str, context: str,
                                       *, image_base64: str = "", mime_type: str = "") -> str:
        if mode not in {"bydlo", "troll", "office", "reshala"}:
            raise ValueError("Неизвестный режим ИИ-секретаря.")
        if not settings.chatgpt_ai_url or not settings.chatgpt_ai_token:
            raise RuntimeError("Связь секретаря с SIWC не настроена.")
        timeout = aiohttp.ClientTimeout(total=210, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.chatgpt_ai_url,
                headers={"Authorization": f"Bearer {settings.chatgpt_ai_token}"},
                json={
                    "user_id": user_id,
                    "record": {
                        "content": context[:9_000],
                        "image_base64": image_base64,
                        "mime_type": mime_type,
                    },
                    "mode": f"secretary_{mode}",
                },
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = {}
                if response.status != 200:
                    message = str(payload.get("error") or "ChatGPT не смог подготовить ответ.")
                    raise RuntimeError(message[:500])
                reply = str(payload.get("reply") or "").strip()
                if not reply:
                    raise RuntimeError("ChatGPT вернул пустой ответ.")
                return reply[:3_900]

    @staticmethod
    def _record(item) -> dict:
        item = dict(item)
        content = item.get("content")
        if item.get("ai_analysis_active") and item.get("ai_previous_content_set"):
            content = item["ai_previous_content"]
        return {
            "kind": str(item.get("kind") or ""),
            "title": str(item.get("page_title") or item.get("file_name") or "")[:500],
            "content": "\n\n".join(
                text for text in (
                    str(content or "").strip(),
                    str(item.get("transcript_text") or "").strip(),
                    str(item.get("context_text") or "").strip(),
                    str(item.get("ocr_text") or "").strip(),
                ) if text
            )[:20_000],
            "source": str(item.get("source_chat_title") or item.get("sender_name") or "")[:500],
            "url": str(item.get("link_url") or "")[:1500],
            "is_forwarded": bool(item.get("is_forwarded")),
        }

    @staticmethod
    async def process_item(bot, settings: Settings, user_id: int, item, *, image_index: int | None = None) -> dict:
        item = dict(item)
        record = ChatGptAiService._record(item)
        image_kinds = item.get("kind") == "photo" or (
            item.get("kind") == "document"
            and item.get("mime_type") in {"image/jpeg", "image/png", "image/webp"}
        )
        file_id = item.get("telegram_file_id")
        if image_kinds and item.get("kind") == "photo" and item.get("media_group_json"):
            try:
                media_group = json.loads(item["media_group_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                media_group = []
            images = [
                (index, entry.get("telegram_file_id"))
                for index, entry in enumerate(media_group)
                if isinstance(entry, dict) and entry.get("kind") == "photo" and entry.get("telegram_file_id")
            ]
            if images:
                selected_index = image_index if image_index is not None else images[0][0]
                selected_file = next((file_id for index, file_id in images if index == selected_index), None)
                if not selected_file:
                    raise ValueError("Не нашёл выбранное фото в альбоме.")
                file_id = selected_file
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
        return await ChatGptAiService._request(settings, user_id, record)

    @staticmethod
    async def process_voice_transcript(settings: Settings, user_id: int, item, transcript: str) -> dict:
        record = ChatGptAiService._record(item)
        caption = str(record.get("content") or "").strip()
        record["content"] = "\n\n".join(part for part in (caption, transcript[:20_000]) if part)
        record["kind"] = "voice"
        result = await ChatGptAiService._request(settings, user_id, record, mode="voice_tags")
        return {"description": transcript[:20_000], "tags": result["tags"], "transcript": transcript[:20_000]}

    @staticmethod
    async def _request(settings: Settings, user_id: int, record: dict, *, mode: str = "process") -> dict:
        if not settings.chatgpt_ai_url or not settings.chatgpt_ai_token:
            raise RuntimeError("AI-обработка не настроена.")
        timeout = aiohttp.ClientTimeout(total=240, connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                settings.chatgpt_ai_url,
                headers={"Authorization": f"Bearer {settings.chatgpt_ai_token}"},
                json={"user_id": user_id, "record": record, "mode": mode},
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
                if not isinstance(tags, list) or (mode != "voice_tags" and not description):
                    raise RuntimeError("ChatGPT вернул некорректный результат.")
                return {"description": description[:1200], "tags": [str(tag)[:40] for tag in tags[:8]]}
