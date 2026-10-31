from __future__ import annotations

from io import BytesIO

import aiohttp

from src.config import Settings

API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_SECONDS = 5 * 60


class GroqTranscriptionService:
    @staticmethod
    async def transcribe_item(bot, settings: Settings, item) -> str:
        if not settings.groq_api_key:
            raise RuntimeError("Расшифровка не настроена: добавь GROQ_API_KEY в Environment UtilBot.")
        item = dict(item)
        if item.get("kind") != "voice" or not item.get("telegram_file_id"):
            raise ValueError("Не нашёл голосовое сообщение в архиве.")
        duration = int(item.get("voice_duration") or 0)
        if duration > MAX_AUDIO_SECONDS:
            raise ValueError("Пока принимаю голосовые до 5 минут.")

        telegram_file = await bot.get_file(item["telegram_file_id"])
        if telegram_file.file_size and telegram_file.file_size > MAX_AUDIO_BYTES:
            raise ValueError("Голосовое больше 20 МБ.")
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        audio = buffer.getvalue()
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("Голосовое пустое или больше 20 МБ.")

        form = aiohttp.FormData()
        form.add_field("file", audio, filename="telegram-voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3-turbo")
        form.add_field("language", "ru")
        form.add_field("response_format", "json")
        form.add_field("temperature", "0")
        timeout = aiohttp.ClientTimeout(total=90, connect=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    API_URL,
                    data=form,
                    headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                ) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        payload = {}
                    if response.status in {401, 403}:
                        raise RuntimeError("Groq отклонил ключ. Проверь GROQ_API_KEY в Environment UtilBot.")
                    if response.status == 429:
                        raise RuntimeError("Достигнут лимит бесплатного распознавания Groq. Попробуй позже.")
                    if response.status != 200:
                        error_payload = payload.get("error")
                        detail = error_payload.get("message") if isinstance(error_payload, dict) else error_payload
                        detail = str(detail or f"HTTP {response.status}")
                        raise RuntimeError(f"Groq не распознал голосовое: {detail[:240]}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RuntimeError("Не удалось связаться с Groq или истекло время ожидания.") from exc

        transcript = str(payload.get("text") or "").strip()
        if not transcript:
            raise RuntimeError("В голосовом не удалось распознать речь.")
        return transcript[:20_000]
