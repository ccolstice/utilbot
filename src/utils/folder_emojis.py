from __future__ import annotations

import secrets


FOLDER_EMOJIS = (
    "📁", "📂", "🗂️", "📚", "🧰", "🎨", "🎵", "🎬", "🎮", "💡",
    "⭐", "🌿", "🌸", "🔥", "💎", "🧠", "🛠️", "🧪", "🧳", "🪴",
    "🍀", "🌙", "☀️", "🚀", "🏷️", "🧩",
)


def random_folder_emoji() -> str:
    return secrets.choice(FOLDER_EMOJIS)
