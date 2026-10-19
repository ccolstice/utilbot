from __future__ import annotations

import logging

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, InlineQuery, Message

from src.config import Settings


class WhitelistFilter(BaseFilter):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def __call__(self, event: Message | CallbackQuery | InlineQuery) -> bool:
        user = event.from_user
        if user is None:
            return False
        allowed = is_allowed_user(user.id, self.settings)
        if not allowed and isinstance(event, Message) and event.text:
            command = event.text.lstrip().split(maxsplit=1)[0].lower()
            if command.split("@", 1)[0] in {"/start", "/help", "/status"}:
                logging.warning(
                    "Ignoring %s because sender is not allowed; check ALLOWED_USER_IDS and OWNER_USER_IDS",
                    command,
                )
                if event.chat.type == "private":
                    await event.answer(
                        "⛔ Этот аккаунт не добавлен в список доступа бота. "
                        "Проверь ALLOWED_USER_IDS и OWNER_USER_IDS в настройках Render."
                    )
        return allowed


def effective_allowed_user_ids(settings: Settings) -> set[int]:
    return set(settings.allowed_user_ids) | set(settings.owner_user_ids)


def effective_owner_user_ids(settings: Settings) -> set[int]:
    if settings.owner_user_ids:
        return set(settings.owner_user_ids)
    return set(settings.allowed_user_ids)


def is_allowed_user(user_id: int, settings: Settings) -> bool:
    allowed = effective_allowed_user_ids(settings)
    if allowed:
        return user_id in allowed
    return not settings.render_free_mode


def is_owner_user(user_id: int, settings: Settings) -> bool:
    owners = effective_owner_user_ids(settings)
    return user_id in owners
