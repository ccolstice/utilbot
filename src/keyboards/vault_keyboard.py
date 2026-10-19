from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 Входящие", callback_data="menu:inbox"),
                InlineKeyboardButton(text="🔎 Поиск", callback_data="menu:search"),
            ],
            [
                InlineKeyboardButton(text="⏳ На потом", callback_data="menu:later"),
                InlineKeyboardButton(text="⭐ Избранное", callback_data="menu:favorites"),
            ],
            [
                InlineKeyboardButton(text="📁 Папки", callback_data="menu:vault"),
                InlineKeyboardButton(text="⏰ Напоминания", callback_data="menu:reminders"),
            ],
            [InlineKeyboardButton(text="🎲 Случайная находка", callback_data="menu:random")],
            [InlineKeyboardButton(text="⚙️ Ещё", callback_data="menu:more")],
        ]
    )


def more_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗂 Все файлы", callback_data="menu:all"),
                InlineKeyboardButton(text="📄 OCR", callback_data="menu:ocr"),
            ],
            [
                InlineKeyboardButton(text="📡 RSS-ленты", callback_data="menu:rss"),
                InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
            ],
            [InlineKeyboardButton(text="🏠 Домой", callback_data="go_home")],
        ]
    )


def rss_menu_keyboard(feeds) -> InlineKeyboardMarkup:
    keyboard = []
    for feed in feeds:
        title = " ".join(str(feed["title"] or feed["url"]).split())
        keyboard.append([
            InlineKeyboardButton(text=f"🔗 {title[:28]}", url=feed["url"]),
            InlineKeyboardButton(text=f"✖ #{feed['id']}", callback_data=f"rss_remove_{feed['id']}"),
        ])
    keyboard.append([InlineKeyboardButton(text="➕ Добавить ленту", callback_data="rss_add_start")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Ещё", callback_data="menu:more")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def state_items_keyboard(rows, *, state: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            text=_item_button_label(row),
            callback_data=f"open_state_{state}_{page}_{row['id']}",
        )]
        for row in rows
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"state_page_{state}_{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"state_page_{state}_{page + 1}"))
    if nav:
        keyboard.append(nav)
    if state == "inbox" and rows:
        keyboard.append([InlineKeyboardButton(text="⚡ Разобрать по одному", callback_data="inbox_review_start")])
        keyboard.append([InlineKeyboardButton(text="✅ Разобрать все входящие", callback_data="inbox_bulk_confirm")])
    keyboard.append([
        InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
        InlineKeyboardButton(text="🔎 Поиск", callback_data="menu:search"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def archived_item_keyboard(item_id: int, *, favorite: bool = False, item_state: str = "inbox") -> InlineKeyboardMarkup:
    state_buttons = item_state_buttons(item_id, item_state, prefix="captured_state")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📂 Открыть", callback_data=f"captured_open_{item_id}"),
            InlineKeyboardButton(text="⭐ В избранное" if not favorite else "★ Убрать из избранного", callback_data=f"captured_favorite_{item_id}"),
        ],
        state_buttons,
        [
            InlineKeyboardButton(text="📁 Переместить", callback_data=f"item_move_a_1_{item_id}"),
            InlineKeyboardButton(text="🏷 Добавить тег", callback_data=f"item_tag_a_1_{item_id}"),
        ],
    ])


def item_state_buttons(item_id: int, item_state: str, *, prefix: str = "view_state") -> list[InlineKeyboardButton]:
    options = {
        "inbox": [("⏳ На потом", "later"), ("✅ Разобрано", "saved")],
        "later": [("✅ Готово", "done"), ("↩ В библиотеку", "saved")],
        "done": [("↩ В библиотеку", "saved")],
        "saved": [("⏳ На потом", "later")],
    }.get(item_state, [("⏳ На потом", "later")])
    return [
        InlineKeyboardButton(text=label, callback_data=f"{prefix}_{target}_{item_id}")
        for label, target in options
    ]


def back_keyboard(back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
                InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
            ]
        ]
    )


def folders_keyboard(
    folders,
    *,
    page: int,
    total_pages: int,
    mode: str = "browse",
) -> InlineKeyboardMarkup:
    keyboard = []

    for folder in folders:
        if mode == "browse":
            cb = f"open_folder_{folder['id']}_{page}"
        elif mode == "move":
            cb = f"move_folder_{folder['id']}"
        else:
            cb = f"save_folder_{folder['id']}"
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {folder['name']}",
                    callback_data=cb,
                )
            ]
        )

    nav_row = []
    if total_pages > 1 and page > 1:
        prefix = {
            "browse": "vault_folders_page_",
            "save": "save_folders_page_",
            "move": "move_folders_page_",
        }[mode]
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"{prefix}{page - 1}",
            )
        )

    if total_pages > 1 and page < total_pages:
        prefix = {
            "browse": "vault_folders_page_",
            "save": "save_folders_page_",
            "move": "move_folders_page_",
        }[mode]
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"{prefix}{page + 1}",
            )
        )

    if nav_row:
        keyboard.append(nav_row)

    if mode == "browse":
        keyboard.append(
            [InlineKeyboardButton(text="➕ Создать папку", callback_data="folder_create")]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🗂 Все файлы",
                    callback_data="vault_all_page_1",
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🏠 Меню",
                    callback_data="go_home",
                )
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"{mode}_cancel",
                ),
                InlineKeyboardButton(
                    text="🏠 Меню",
                    callback_data="go_home",
                ),
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _item_button_label(row) -> str:
    kind = row["kind"]
    if kind == "link" or (("media_type" in row.keys()) and row["media_type"] == "link"):
        preview = row["page_title"] if "page_title" in row.keys() and row["page_title"] else row["link_domain"] if "link_domain" in row.keys() and row["link_domain"] else row["content"] or "Ссылка"
        preview = " ".join(str(preview).split())
        return f"🔗 {preview[:32]}{'…' if len(preview) > 32 else ''}"

    if kind == "photo":
        preview = row["tag"] or row["content"] or "Фото"
        preview = " ".join(str(preview).split())
        return f"🖼 {preview[:30]}{'…' if len(preview) > 30 else ''}"

    if kind in {"voice", "audio"}:
        preview = row["tag"] or row["content"] or ("ГС" if kind == "voice" else "Аудио")
        preview = " ".join(str(preview).split())
        prefix = "🎤" if kind == "voice" else "🎵"
        return f"{prefix} {preview[:28]}{'…' if len(preview) > 28 else ''}"

    if kind == "video_note":
        preview = row["tag"] or row["content"] or "Кружок"
        preview = " ".join(str(preview).split())
        return f"🟣 {preview[:28]}{'…' if len(preview) > 28 else ''}"

    if kind == "video":
        preview = row["tag"] or row["content"] or "Видео"
        preview = " ".join(str(preview).split())
        return f"🎬 {preview[:28]}{'…' if len(preview) > 28 else ''}"

    if kind == "animation":
        preview = row["tag"] or row["content"] or "GIF"
        preview = " ".join(str(preview).split())
        return f"✨ {preview[:28]}{'…' if len(preview) > 28 else ''}"

    if kind == "document":
        preview = row["tag"] or row["content"] or "Документ"
        preview = " ".join(str(preview).split())
        return f"📎 {preview[:28]}{'…' if len(preview) > 28 else ''}"

    if kind == "sticker":
        preview = row["tag"] or row["content"] or "Стикер"
        preview = " ".join(str(preview).split())
        return f"🔖 {preview[:28]}{'…' if len(preview) > 28 else ''}"

    preview = row["tag"] or row["content"] or "Текст"
    preview = " ".join(str(preview).split())
    if len(preview) > 34:
        preview = preview[:34].rstrip() + "…"
    return f"📝 {preview}"


def folder_items_keyboard(
    rows,
    *,
    folder_id: int,
    folders_page: int,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    keyboard = []

    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_item_button_label(row),
                    callback_data=f"open_item_f_{folder_id}_{folders_page}_{page}_{row['id']}",
                )
            ]
        )

    nav_row = []
    if total_pages > 1 and page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"vault_items_page_{folder_id}_{folders_page}_{page - 1}",
            )
        )
    if total_pages > 1 and page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"vault_items_page_{folder_id}_{folders_page}_{page + 1}",
            )
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text="⚙️ Папка",
                callback_data=f"folder_manage_{folder_id}_{folders_page}_{page}",
            ),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅️ К папкам",
                callback_data=f"vault_folders_page_{folders_page}",
            ),
            InlineKeyboardButton(
                text="🗂 Все файлы",
                callback_data="vault_all_page_1",
            ),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="go_home",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def all_items_keyboard(
    rows,
    *,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    keyboard = []

    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_item_button_label(row),
                    callback_data=f"open_item_a_{page}_{row['id']}",
                )
            ]
        )

    nav_row = []
    if total_pages > 1 and page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"vault_all_page_{page - 1}",
            )
        )
    if total_pages > 1 and page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"vault_all_page_{page + 1}",
            )
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text="⬅️ К папкам",
                callback_data="vault_folders_page_1",
            ),
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="go_home",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def search_results_keyboard(
    rows,
    *,
    session_id: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    keyboard = []

    for row in rows:
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=_item_button_label(row),
                    callback_data=f"open_item_s_{session_id}_{page}_{row['id']}",
                )
            ]
        )

    nav_row = []
    if total_pages > 1 and page > 1:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"search_page_{session_id}_{page - 1}",
            )
        )
    if total_pages > 1 and page < total_pages:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"search_page_{session_id}_{page + 1}",
            )
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton(
                text="🔎 Новый поиск",
                callback_data="search_prompt",
            ),
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="go_home",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def item_view_keyboard(
    back_callback: str,
    favorite_callback: str | None = None,
    favorite: bool = False,
    actions_callback: str | None = None,
    delete_callback: str | None = None,
    review_next_callback: str | None = None,
    review_move_callback: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    if favorite_callback:
        label = "★ Убрать из избранного" if favorite else "⭐ В избранное"
        favorite_button = InlineKeyboardButton(text=label, callback_data=favorite_callback)
    else:
        favorite_button = None
    if actions_callback:
        actions_button = InlineKeyboardButton(text="⚙️ Ещё действия", callback_data=actions_callback)
        rows.append([button for button in (favorite_button, actions_button) if button])
    elif favorite_button:
        rows.append([favorite_button])

    if review_move_callback or review_next_callback:
        review_row = []
        if review_move_callback:
            review_row.append(InlineKeyboardButton(text="📁 В папку", callback_data=review_move_callback))
        if review_next_callback:
            review_row.append(InlineKeyboardButton(text="⏭ Пропустить", callback_data=review_next_callback))
        rows.append(review_row)

    # Keep deletion on the main item screen, but isolated at the bottom to
    # reduce accidental taps while keeping it easy to find.
    if delete_callback:
        rows.append([InlineKeyboardButton(text="🗑 Удалить материал", callback_data=delete_callback)])

    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
            InlineKeyboardButton(text="🏠 Меню", callback_data="go_home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_actions_keyboard(
    *,
    move_callback: str | None,
    tag_callback: str,
    remind_callback: str,
    card_callback: str,
    ocr_callback: str | None = None,
    context_callback: str | None = None,
    share_query: str | None = None,
    has_context: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if move_callback:
        rows.append([
            InlineKeyboardButton(text="📁 Переместить в папку", callback_data=move_callback),
            InlineKeyboardButton(text="🏷 Изменить тег", callback_data=tag_callback),
        ])
    else:
        rows.append([InlineKeyboardButton(text="🏷 Изменить тег", callback_data=tag_callback)])
    rows.append([InlineKeyboardButton(text="⏰ Напоминание", callback_data=remind_callback)])
    if ocr_callback:
        label = "📄 Распознать текст" if ocr_callback.startswith("item_ocr_run_") else "📄 Показать OCR"
        rows.append([InlineKeyboardButton(text=label, callback_data=ocr_callback)])
    if context_callback:
        label = "✏️ Изменить контекст" if has_context else "🗒 Добавить контекст"
        rows.append([InlineKeyboardButton(text=label, callback_data=context_callback)])
    if share_query is not None:
        rows.append([InlineKeyboardButton(text="📤 Отправить в другой чат", switch_inline_query=share_query)])
    rows.append([InlineKeyboardButton(text="⬅️ К материалу", callback_data=card_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def folder_actions_keyboard(
    folder,
    *,
    folders_page: int,
    page: int,
) -> InlineKeyboardMarkup:
    archived = folder["archived_at"] is not None

    rows = []

    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=f"folder_rename_{folder['id']}_{folders_page}_{page}",
            )
        ]
    )

    if archived:
        rows.append(
            [
                InlineKeyboardButton(
                    text="♻️ Разархивировать",
                    callback_data=f"folder_restore_{folder['id']}_{folders_page}_{page}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗃 Архивировать",
                    callback_data=f"folder_archive_{folder['id']}_{folders_page}_{page}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"folder_delete_{folder['id']}_{folders_page}_{page}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К папке",
                callback_data=f"open_folder_{folder['id']}_{folders_page}",
            ),
            InlineKeyboardButton(
                text="🏠 Меню",
                callback_data="go_home",
            ),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)
