from __future__ import annotations

from math import ceil

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from src.services.vault_service import VaultService
from src.keyboards.vault_keyboard import main_menu_keyboard

router = Router()
PAGE_SIZE = 10


def _paginate(items, page: int, page_size: int = PAGE_SIZE):
    total_pages = max(1, ceil(len(items) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], total_pages, page, start

def _preview_row(row) -> str:
    kind = row["kind"]
    if kind == "photo":
        label = row["tag"] or row["content"] or "Фото"
        return f"🖼 {str(label)[:30]}"
    if kind in {"voice", "audio"}:
        label = row["tag"] or row["content"] or ("ГС" if kind == "voice" else "Аудио")
        return f"{'🎤' if kind == 'voice' else '🎵'} {str(label)[:30]}"
    if kind == "video_note":
        return f"🟣 {(row['tag'] or row['content'] or 'Кружок')[:30]}"
    if kind == "video":
        return f"🎬 {(row['tag'] or row['content'] or 'Видео')[:30]}"
    if kind == "animation":
        return f"✨ {(row['tag'] or row['content'] or 'GIF')[:30]}"
    if kind == "document":
        return f"📎 {(row['tag'] or row['content'] or 'Документ')[:30]}"
    if kind == "sticker":
        return f"🔖 {(row['tag'] or row['content'] or 'Стикер')[:30]}"
    return f"📝 {(row['tag'] or row['content'] or 'Текст')[:30]}"

def _favorites_keyboard(rows, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = []
    for row in rows:
        keyboard.append([
            InlineKeyboardButton(text=_preview_row(row), callback_data=f"fav_open_{page}_{row['id']}")
        ])
    nav = []
    if total_pages > 1 and page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"fav_page_{page - 1}"))
    if total_pages > 1 and page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"fav_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🏠 Меню", callback_data="go_home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def _send_text(target, text: str, reply_markup=None):
    from src import app as app_module
    await app_module.render_menu_panel(target, text, reply_markup=reply_markup)
    if isinstance(target, CallbackQuery):
        await target.answer()

async def _show_favorites_page(target, user_id: int, page: int, edit: bool = False):
    rows = VaultService.list_favorites(user_id, include_archived=False)
    if not rows:
        if edit and isinstance(target, CallbackQuery):
            from src import app as app_module
            await app_module.render_menu_panel(target, "⭐ Избранное пусто", reply_markup=main_menu_keyboard())
            await target.answer()
            return
        await _send_text(target, "⭐ Избранное пусто", reply_markup=main_menu_keyboard())
        return
    page_rows, total_pages, page, _ = _paginate(rows, page)
    text = f"⭐ Избранное\nСтраница {page}/{total_pages}\n\n" + "\n\n".join(
        f"#{row['id']} {_preview_row(row)}" for row in page_rows
    )
    markup = _favorites_keyboard(page_rows, page, total_pages)
    if edit and isinstance(target, CallbackQuery):
        from src import app as app_module
        await app_module.render_menu_panel(target, text, reply_markup=markup)
        await target.answer()
    else:
        await _send_text(target, text, reply_markup=markup)

@router.callback_query(F.data.startswith("fav_page_"))
async def favorites_page(callback: CallbackQuery):
    page = int(callback.data.replace("fav_page_", ""))
    await _show_favorites_page(callback, callback.from_user.id, page, edit=True)

@router.callback_query(F.data.startswith("fav_toggle_"))
async def favorite_toggle(callback: CallbackQuery):
    item_id = int(callback.data.replace("fav_toggle_", ""))
    item = VaultService.toggle_favorite(callback.from_user.id, item_id)
    if not item:
        await callback.answer("Item не найден", show_alert=True)
        return
    state = "добавлено в избранное" if int(item["is_favorite"] or 0) else "убрано из избранного"
    if callback.message and callback.message.reply_markup:
        rows = []
        for row in callback.message.reply_markup.inline_keyboard:
            updated = []
            for button in row:
                if button.callback_data == f"fav_toggle_{item_id}":
                    label = "★ Убрать из избранного" if item["is_favorite"] else "⭐ В избранное"
                    updated.append(InlineKeyboardButton(text=label, callback_data=button.callback_data))
                else:
                    updated.append(button)
            rows.append(updated)
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer(f"⭐ {state}", show_alert=False)

@router.callback_query(F.data.startswith("fav_open_"))
async def favorite_open_item(callback: CallbackQuery):
    parts = callback.data.split("_")
    page = int(parts[2])
    item_id = int(parts[3])
    item = VaultService.get_item(callback.from_user.id, item_id)
    if not item:
        await callback.answer("Item не найден", show_alert=True)
        return
    from src import app as app_module
    await app_module.show_item_view(callback, callback.from_user.id, item_id, origin="all", page=page)


