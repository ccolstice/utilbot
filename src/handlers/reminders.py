from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.keyboards.reminder_keyboard import (
    reminder_detail_keyboard,
    reminder_due_keyboard,
    reminder_repeat_keyboard,
    reminder_snooze_keyboard,
    reminders_list_keyboard,
)
from src.keyboards.vault_keyboard import main_menu_keyboard
from src.services.vault_service import VaultService
from src.services.reminder_service import ReminderService
from src.utils.time_parser import parse_datetime_input
from src.utils.text_cleaner import safe_excerpt

router = Router()
PAGE_SIZE = 10

# Fallback draft storage so reminder creation doesn't depend only on FSM memory.
pending_reminder_drafts: dict[int, dict] = {}


class ReminderStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_custom_due = State()
    waiting_for_repeat = State()


class ReminderEditStates(StatesGroup):
    waiting_for_new_title = State()
    waiting_for_new_due = State()


def _paginate(items, page: int, page_size: int = PAGE_SIZE):
    total_pages = max(1, ceil(len(items) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total_pages, page, start


def _format_dt(value) -> str:
    if not value:
        return "—"
    try:
        return str(value)[:16]
    except Exception:
        return str(value)


def _reminder_text(row) -> str:
    repeat = row["repeat_minutes"]
    repeat_txt = "нет"
    if repeat:
        if repeat % 10080 == 0:
            repeat_txt = f"каждые {repeat // 10080} нед."
        elif repeat % 1440 == 0:
            repeat_txt = f"каждые {repeat // 1440} дн."
        else:
            repeat_txt = f"каждые {repeat} мин."

    lines = [
        f"⏰ Напоминание #{row['id']}",
        "",
        row["title"],
        "",
        f"⏳ Когда: {_format_dt(row['due_at'])}",
        f"🔁 Повтор: {repeat_txt}",
        f"📌 Статус: {row['status']}",
    ]

    source_item_id = row["source_item_id"] if "source_item_id" in row.keys() else None
    note = row["note"] if "note" in row.keys() else None

    if source_item_id:
        lines.append(f"📎 Источник: item #{source_item_id}")
    if note:
        lines.append(f"🗒 {note}")

    return "\n".join(lines)


def _item_reminder_title(row) -> str:
    if row["tag"]:
        return row["tag"]
    if row["content"]:
        return safe_excerpt(row["content"], 80)
    if row["kind"] == "photo":
        return "Фото"
    if row["kind"] == "voice":
        return "Голосовое сообщение"
    if row["kind"] == "audio":
        return "Аудио"
    return "Файл"


async def _send_text(target, text: str, reply_markup=None):
    from src import app as app_module
    await app_module.render_menu_panel(target, text, reply_markup=reply_markup)
    if isinstance(target, CallbackQuery):
        await target.answer()


async def _edit_text_or_send(target, text: str, reply_markup=None):
    if isinstance(target, CallbackQuery):
        await _send_text(target, text, reply_markup=reply_markup)
    else:
        from src import app as app_module
        await app_module.render_menu_panel(target, text, reply_markup=reply_markup)


async def show_reminders_page(target, user_id: int, page: int, *, edit: bool = False):
    rows = ReminderService.list_reminders(user_id, include_done=False, limit=100)
    if not rows:
        if edit and isinstance(target, CallbackQuery):
            await _send_text(
                target,
                "⏰ Напоминаний пока нет\n\nМожешь сразу создать новое.",
                reply_markup=reminders_list_keyboard([], page=1, total_pages=1),
            )
            return
        await _send_text(
            target,
            "⏰ Напоминаний пока нет\n\nМожешь сразу создать новое.",
            reply_markup=reminders_list_keyboard([], page=1, total_pages=1),
        )
        return

    page_rows, total_pages, page, start = _paginate(rows, page)
    text = f"⏰ Напоминания\nСтраница {page}/{total_pages}\n\n"
    visible_index = start + 1
    for row in page_rows:
        text += f"#{visible_index} {row['title']}\n⏳ {_format_dt(row['due_at'])}\n\n"
        visible_index += 1

    markup = reminders_list_keyboard(page_rows, page=page, total_pages=total_pages)
    if edit:
        await _edit_text_or_send(target, text, reply_markup=markup)
    else:
        await _send_text(target, text, reply_markup=markup)


@router.message(ReminderStates.waiting_for_title)
async def reminder_title_message_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await _send_text(message, "📝 Введи текст напоминания.")
        return

    title = message.text.strip()
    await state.update_data(title=title)
    pending_reminder_drafts[message.from_user.id] = {"title": title}
    await state.set_state(ReminderStates.waiting_for_repeat)
    await _send_text(message, "⏰ Выбери время напоминания:", reply_markup=reminder_due_keyboard())


@router.callback_query(F.data == "remind_start")
async def remind_start_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReminderStates.waiting_for_title)
    await _edit_text_or_send(callback, "📝 Введи текст напоминания.")


@router.callback_query(F.data == "remind_cancel")
async def remind_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    pending_reminder_drafts.pop(callback.from_user.id, None)
    await _edit_text_or_send(callback, "❌ Напоминание отменено", reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("item_remind_"))
async def item_remind_callback(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.replace("item_remind_", ""))
    item = VaultService.get_item(callback.from_user.id, item_id)
    if not item:
        await callback.answer("Item не найден", show_alert=True)
        return

    title = _item_reminder_title(item)
    await state.update_data(
        title=title,
        source_item_id=item_id,
        note=f"Напоминание из item #{item_id}",
    )
    pending_reminder_drafts[callback.from_user.id] = {
        "title": title,
        "source_item_id": item_id,
        "note": f"Напоминание из item #{item_id}",
    }
    await state.set_state(ReminderStates.waiting_for_repeat)
    await _send_text(
        callback,
        f"⏰ Напоминание для item #{item_id}\n\n{title}\n\nВыбери время:",
        reply_markup=reminder_due_keyboard(),
    )


@router.callback_query(F.data.startswith("remind_due_"))
async def remind_due_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get("title")
    if not title:
        await callback.answer("Сначала введи текст", show_alert=True)
        return

    choice = callback.data.replace("remind_due_", "")
    now = datetime.utcnow().replace(second=0, microsecond=0)

    if choice == "5m":
        due = now + timedelta(minutes=5)
    elif choice == "15m":
        due = now + timedelta(minutes=15)
    elif choice == "1h":
        due = now + timedelta(hours=1)
    elif choice == "tomorrow":
        due = now + timedelta(days=1)
    elif choice == "custom":
        await state.set_state(ReminderStates.waiting_for_custom_due)
        await _send_text(callback, "⏳ Введи дату/время. Примеры: 2026-06-02 18:00, tomorrow 18:00, in 10m")
        return
    else:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return

    due_iso = due.isoformat(sep=" ")
    await state.update_data(due_at=due_iso)
    pending_reminder_drafts.setdefault(callback.from_user.id, {})
    pending_reminder_drafts[callback.from_user.id]["due_at"] = due_iso
    await state.set_state(ReminderStates.waiting_for_repeat)
    await _send_text(callback, "🔁 Повтор?", reply_markup=reminder_repeat_keyboard())


@router.message(ReminderStates.waiting_for_custom_due)
async def reminder_custom_due_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await _send_text(message, "⏳ Введи дату/время.")
        return

    due = parse_datetime_input(message.text)
    if not due:
        await _send_text(message, "❌ Не смог распознать дату/время. Пример: 2026-06-02 18:00")
        return

    await state.update_data(due_at=due.replace(second=0, microsecond=0).isoformat(sep=" "))
    await state.set_state(ReminderStates.waiting_for_repeat)
    await _send_text(message, "🔁 Повтор?", reply_markup=reminder_repeat_keyboard())


@router.callback_query(F.data.startswith("remind_repeat_"))
async def remind_repeat_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    draft = pending_reminder_drafts.get(callback.from_user.id, {})
    title = draft.get("title") or data.get("title")
    due_at = draft.get("due_at") or data.get("due_at")
    if not title or not due_at:
        await state.clear()
        pending_reminder_drafts.pop(callback.from_user.id, None)
        await callback.answer("Сессия напоминания устарела. Создай напоминание заново.", show_alert=True)
        return

    repeat_raw = callback.data.replace("remind_repeat_", "")
    repeat_minutes = None if repeat_raw == "none" else int(repeat_raw)

    reminder = ReminderService.create_reminder(
        user_id=callback.from_user.id,
        title=title,
        due_at=datetime.fromisoformat(due_at),
        repeat_minutes=repeat_minutes,
        note=draft.get("note") or data.get("note"),
        source_item_id=draft.get("source_item_id") or data.get("source_item_id"),
    )

    await state.clear()
    pending_reminder_drafts.pop(callback.from_user.id, None)
    await _send_text(
        callback,
        f"✅ Напоминание создано\n\n{_reminder_text(reminder)}",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("reminders_page_"))
async def reminders_page_callback(callback: CallbackQuery):
    page = int(callback.data.replace("reminders_page_", ""))
    await show_reminders_page(callback, callback.from_user.id, page, edit=True)


@router.callback_query(F.data.startswith("open_reminder_"))
async def open_reminder_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    reminder_id = int(parts[2])
    page = int(parts[3])
    row = ReminderService.get_reminder(callback.from_user.id, reminder_id)
    if not row:
        await callback.answer("Напоминание не найдено", show_alert=True)
        return
    await _edit_text_or_send(callback, _reminder_text(row), reply_markup=reminder_detail_keyboard(reminder_id, page))


@router.callback_query(F.data.startswith("reminder_done_"))
async def reminder_done_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    reminder_id = int(parts[2])
    page = int(parts[3])

    row = ReminderService.get_reminder(callback.from_user.id, reminder_id)
    if not row:
        await callback.answer("Напоминание не найдено", show_alert=True)
        return

    repeat_minutes = row["repeat_minutes"]
    if repeat_minutes:
        ReminderService.reschedule_recurring(reminder_id, int(repeat_minutes))
    else:
        ReminderService.mark_done(callback.from_user.id, reminder_id)
    await ReminderService.clear_notification_messages(
        callback.bot,
        callback.from_user.id,
        reminder_id,
        exclude_message_id=callback.message.message_id if callback.message else None,
    )

    await show_reminders_page(callback, callback.from_user.id, page, edit=True)


@router.callback_query(F.data.startswith("reminder_delete_"))
async def reminder_delete_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    reminder_id = int(parts[2])
    page = int(parts[3])
    ReminderService.delete_reminder(callback.from_user.id, reminder_id)
    await ReminderService.clear_notification_messages(
        callback.bot,
        callback.from_user.id,
        reminder_id,
        exclude_message_id=callback.message.message_id if callback.message else None,
    )
    await show_reminders_page(callback, callback.from_user.id, page, edit=True)


@router.callback_query(F.data.startswith("reminder_snooze_menu_"))
async def reminder_snooze_menu_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    reminder_id = int(parts[3])
    page = int(parts[4])
    await _edit_text_or_send(callback, "⏰ На сколько отложить?", reply_markup=reminder_snooze_keyboard(reminder_id, page))


@router.callback_query(F.data.startswith("reminder_snooze_"))
async def reminder_snooze_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    reminder_id = int(parts[2])
    page = int(parts[3])
    minutes = int(parts[4])
    ReminderService.snooze_reminder(callback.from_user.id, reminder_id, minutes)
    await ReminderService.clear_notification_messages(
        callback.bot,
        callback.from_user.id,
        reminder_id,
        exclude_message_id=callback.message.message_id if callback.message else None,
    )
    await show_reminders_page(callback, callback.from_user.id, page, edit=True)
