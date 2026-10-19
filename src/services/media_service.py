from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiogram.types import Message

from src.utils.text_cleaner import normalize_multiline_input

@dataclass(slots=True)
class MediaPayload:
    kind: str
    telegram_file_id: str | None
    content: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    media_type: str | None = None

class MediaService:
    @staticmethod
    def from_message(message: Message) -> MediaPayload | None:
        caption = normalize_multiline_input(message.caption).strip() or None
        text = normalize_multiline_input(message.text).strip() or None

        if message.photo:
            photo = message.photo[-1]
            return MediaPayload(
                kind="photo",
                telegram_file_id=photo.file_id,
                content=caption,
                file_size=getattr(photo, "file_size", None),
                width=getattr(photo, "width", None),
                height=getattr(photo, "height", None),
                media_type="image/jpeg",
            )

        if message.video_note:
            return MediaPayload(
                kind="video_note",
                telegram_file_id=message.video_note.file_id,
                content=caption,
                duration=getattr(message.video_note, "duration", None),
                media_type="video/mp4",
            )

        if message.voice:
            return MediaPayload(
                kind="voice",
                telegram_file_id=message.voice.file_id,
                duration=getattr(message.voice, "duration", None),
                media_type="audio/ogg",
            )

        if message.audio:
            return MediaPayload(
                kind="audio",
                telegram_file_id=message.audio.file_id,
                content=caption,
                file_name=getattr(message.audio, "file_name", None),
                file_size=getattr(message.audio, "file_size", None),
                duration=getattr(message.audio, "duration", None),
                media_type=getattr(message.audio, "mime_type", None),
            )

        if message.video:
            return MediaPayload(
                kind="video",
                telegram_file_id=message.video.file_id,
                content=caption,
                file_name=getattr(message.video, "file_name", None),
                file_size=getattr(message.video, "file_size", None),
                width=getattr(message.video, "width", None),
                height=getattr(message.video, "height", None),
                duration=getattr(message.video, "duration", None),
                media_type=getattr(message.video, "mime_type", None),
            )

        if message.animation:
            return MediaPayload(
                kind="animation",
                telegram_file_id=message.animation.file_id,
                content=caption,
                file_name=getattr(message.animation, "file_name", None),
                file_size=getattr(message.animation, "file_size", None),
                width=getattr(message.animation, "width", None),
                height=getattr(message.animation, "height", None),
                duration=getattr(message.animation, "duration", None),
                media_type=getattr(message.animation, "mime_type", None),
            )

        if message.document:
            doc = message.document
            return MediaPayload(
                kind="document",
                telegram_file_id=doc.file_id,
                content=caption or text,
                file_name=getattr(doc, "file_name", None),
                file_size=getattr(doc, "file_size", None),
                media_type=getattr(doc, "mime_type", None),
            )

        if getattr(message, "sticker", None):
            sticker = message.sticker
            return MediaPayload(
                kind="sticker",
                telegram_file_id=sticker.file_id,
                content=caption,
                file_size=getattr(sticker, "file_size", None),
                width=getattr(sticker, "width", None),
                height=getattr(sticker, "height", None),
                media_type=getattr(sticker, "mime_type", None),
            )

        if text:
            return MediaPayload(kind="text", telegram_file_id=None, content=text)

        return None

    @staticmethod
    def build_title(payload: MediaPayload) -> str:
        return {
            "photo": payload.content or "Фото",
            "voice": payload.content or "Голосовое сообщение",
            "audio": payload.content or "Аудио",
            "video_note": payload.content or "Кружок",
            "video": payload.content or "Видео",
            "animation": payload.content or "GIF",
            "document": payload.file_name or payload.content or "Документ",
            "sticker": payload.content or "Стикер",
            "text": payload.content or "Текст",
        }.get(payload.kind, payload.content or payload.kind.title())
