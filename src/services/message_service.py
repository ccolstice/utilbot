from __future__ import annotations

from aiogram.types import Message


def message_excerpt(message: Message) -> str:
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    if message.voice:
        return f"voice:{message.voice.file_id}"
    if message.photo:
        return "photo"
    if message.document:
        return message.document.file_name or "document"
    return "message"
