from __future__ import annotations

from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.keyboards.vault_keyboard import main_menu_keyboard
from src.services.ocr_service import OCRService
from src.services.vault_service import VaultService
from src.states.save_state import SaveStates
from src.utils.text_cleaner import normalize_ocr_text, safe_excerpt

router = Router()
TEMP_DIR = Path("data/media_tmp")
TEMP_DIR.mkdir(parents=True, exist_ok=True)


class OCRStates(StatesGroup):
    waiting_for_mode = State()
    waiting_for_media = State()
    waiting_for_edit_text = State()


async def _send_text(target, text: str, reply_markup=None):
    from src import app as app_module
    await app_module.render_menu_panel(target, text, reply_markup=reply_markup)
    if isinstance(target, CallbackQuery):
        await target.answer()


def _mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="ocr_mode_rus"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="ocr_mode_eng"),
            ],
            [
                InlineKeyboardButton(text="🌍 Mixed", callback_data="ocr_mode_mixed"),
                InlineKeyboardButton(text="⚙️ Auto", callback_data="ocr_mode_auto"),
            ],
            [
                InlineKeyboardButton(text="❌ Отмена", callback_data="ocr_cancel"),
            ],
        ]
    )


def _ocr_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="ocr_edit"),
                InlineKeyboardButton(text="💾 Сохранить", callback_data="ocr_save"),
            ],
            [
                InlineKeyboardButton(text="🔄 Новый OCR", callback_data="ocr_restart"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="ocr_cancel"),
            ],
        ]
    )


async def _show_prompt(target):
    await _send_text(
        target,
        "📄 OCR\n\nСначала выбери режим распознавания, потом пришли фото или картинку-файл.",
        reply_markup=_mode_keyboard(),
    )


async def _show_result(target, text: str):
    preview = text if len(text) <= 3500 else safe_excerpt(text, 3500)
    await _send_text(
        target,
        f"📄 OCR готов\n\n{preview}",
        reply_markup=_ocr_result_keyboard(),
    )


async def _download_image(bot: Bot, message: Message) -> Path | None:
    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        dest = TEMP_DIR / f"ocr_{message.photo[-1].file_id}.jpg"
        await bot.download_file(file.file_path, destination=dest)
        return dest

    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        file = await bot.get_file(doc.file_id)
        suffix = Path(doc.file_name or "ocr_image").suffix or ".jpg"
        dest = TEMP_DIR / f"ocr_{doc.file_id}{suffix}"
        await bot.download_file(file.file_path, destination=dest)
        return dest

    return None


@router.callback_query(F.data.startswith("ocr_mode_"))
async def ocr_mode_callback(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.replace("ocr_mode_", "")
    if mode not in {"rus", "eng", "mixed", "auto"}:
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    await state.update_data(ocr_mode=mode)
    await state.set_state(OCRStates.waiting_for_media)
    await _send_text(
        callback,
        f"✅ Режим OCR: {mode}\n\nТеперь пришли фото или картинку-файл.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(OCRStates.waiting_for_media, F.photo | F.document)
async def ocr_media_handler(message: Message, state: FSMContext, bot: Bot):
    path = await _download_image(bot, message)
    if path is None:
        from src import app as app_module
        await app_module.render_menu_panel(message, "📄 Пришли именно фото или изображение-файл.")
        return

    data = await state.get_data()
    mode = data.get("ocr_mode", "auto")

    from src import app as app_module
    await app_module.render_menu_panel(message, "🧠 Распознаю текст…")
    service = OCRService(mode=mode)
    try:
        text = service.extract_text(path)
        text = normalize_ocr_text(text)
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    if not text:
        await app_module.render_menu_panel(message, "⚠️ OCR ничего не нашёл. Попробуй более чёткое изображение.")
        return

    await state.update_data(
        ocr_text=text,
        ocr_source_message_id=message.message_id,
        ocr_mode=mode,
    )
    await _show_result(message, text)


@router.message(OCRStates.waiting_for_media)
async def ocr_waiting_fallback(message: Message):
    from src import app as app_module
    await app_module.render_menu_panel(message, "📄 Сначала пришли фото или картинку-файл для OCR.")


@router.callback_query(F.data == "ocr_cancel")
async def ocr_cancel_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _send_text(callback, "❌ OCR отменён", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "ocr_restart")
async def ocr_restart_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OCRStates.waiting_for_mode)
    await _send_text(callback, "📄 Выбери режим OCR ещё раз.", reply_markup=_mode_keyboard())


@router.callback_query(F.data == "ocr_edit")
async def ocr_edit_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("ocr_text"):
        await callback.answer("Текст не найден", show_alert=True)
        return
    await state.set_state(OCRStates.waiting_for_edit_text)
    await _send_text(callback, "✏️ Отправь исправленный текст одним сообщением.")


@router.callback_query(F.data == "ocr_save")
async def ocr_save_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = (data.get("ocr_text") or "").strip()
    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return

    await state.update_data(
        payload_type="note",
        note_text=text,
        ocr_text=text,
        ocr_tag=data.get("ocr_tag"),
        ocr_source_message_id=data.get("ocr_source_message_id"),
        ocr_mode=data.get("ocr_mode", "auto"),
    )
    await state.set_state(SaveStates.waiting_for_tag)
    await _send_text(callback, "🏷 OCR готов. Введи тег или напиши 'пропустить'.")


@router.message(OCRStates.waiting_for_edit_text)
async def ocr_edit_text_handler(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        from src import app as app_module
        await app_module.render_menu_panel(message, "✏️ Пришли исправленный текст одним сообщением.")
        return

    text = message.text.strip()
    await state.update_data(ocr_text=text)
    await state.set_state(OCRStates.waiting_for_media)
    await _show_result(message, text)
