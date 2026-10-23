from __future__ import annotations

import os

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo


def reminder_due_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5 мин", callback_data="remind_due_5m"),
                InlineKeyboardButton(text="15 мин", callback_data="remind_due_15m"),
                InlineKeyboardButton(text="1 час", callback_data="remind_due_1h"),
            ],
            [
                InlineKeyboardButton(text="Завтра", callback_data="remind_due_tomorrow"),
                InlineKeyboardButton(text="Свой срок", callback_data="remind_due_custom"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="remind_cancel"),
            ],
        ]
    )


def reminder_repeat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Без повтора", callback_data="remind_repeat_none"),
            ],
            [
                InlineKeyboardButton(text="Каждый час", callback_data="remind_repeat_60"),
                InlineKeyboardButton(text="Каждый день", callback_data="remind_repeat_1440"),
            ],
            [
                InlineKeyboardButton(text="Каждую неделю", callback_data="remind_repeat_10080"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="remind_cancel"),
            ],
        ]
    )


def reminders_list_keyboard(rows, *, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = []
    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"⏰ {row['title']} · {str(row['due_at'])[:16]}",
                    callback_data=f"open_reminder_{row['id']}_{page}",
                )
            ]
        )

    nav_row = []
    if total_pages > 1 and page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"reminders_page_{page - 1}"))
    if total_pages > 1 and page < total_pages:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"reminders_page_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton(text="➕ Сделать напоминалку", callback_data="remind_start"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def reminder_detail_keyboard(reminder_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово", callback_data=f"reminder_done_{reminder_id}_{page}"),
                InlineKeyboardButton(text="⏰ Отложить", callback_data=f"reminder_snooze_menu_{reminder_id}_{page}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"reminder_delete_{reminder_id}_{page}"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"reminders_page_{page}"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
            ],
        ]
    )


def reminder_snooze_keyboard(reminder_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="10 мин", callback_data=f"reminder_snooze_{reminder_id}_{page}_10"),
                InlineKeyboardButton(text="1 час", callback_data=f"reminder_snooze_{reminder_id}_{page}_60"),
            ],
            [
                InlineKeyboardButton(text="Завтра", callback_data=f"reminder_snooze_{reminder_id}_{page}_1440"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"open_reminder_{reminder_id}_{page}"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
            ],
        ]
    )


def reminder_notification_keyboard(reminder_id: int, source_item_id: int | None = None) -> InlineKeyboardMarkup:
    keyboard = reminder_detail_keyboard(reminder_id, 1)
    if source_item_id:
        web_app_url = os.getenv("MINI_APP_URL", "").strip()
        if not web_app_url:
            render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
            if render_url.startswith("https://"):
                web_app_url = f"{render_url}/miniapp/"
        if web_app_url.startswith("https://"):
            separator = "&" if "?" in web_app_url else "?"
            keyboard.inline_keyboard.insert(0, [
                InlineKeyboardButton(
                    text="🗂 Открыть материал",
                    web_app=WebAppInfo(url=f"{web_app_url}{separator}item={source_item_id}"),
                )
            ])
    return keyboard
