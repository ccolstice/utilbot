from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from src.utils.folder_emojis import random_folder_emoji

DATABASE_FILE = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)


def get_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column_sql: str):
    column_name = column_sql.split()[0]
    if not _column_exists(conn, table, column_name):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")


def init_db():
    conn = get_connection()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS vault_folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        archived_at TIMESTAMP DEFAULT NULL,
        UNIQUE(user_id, name)
    );

    CREATE TABLE IF NOT EXISTS vault_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        folder_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'text',
        content TEXT,
        tag TEXT,
        telegram_file_id TEXT,
        archive_chat_id INTEGER,
        archive_message_id INTEGER,
        item_state TEXT NOT NULL DEFAULT 'saved',
        dedupe_key TEXT,
        voice_duration INTEGER,
        extracted_text TEXT,
        transcript_text TEXT,
        ocr_status TEXT DEFAULT NULL,
        transcript_status TEXT DEFAULT NULL,
        processing_started_at TIMESTAMP DEFAULT NULL,
        processing_completed_at TIMESTAMP DEFAULT NULL,
        sender_name TEXT,
        sender_username TEXT,
        source_chat_title TEXT,
        source_chat_id INTEGER,
        source_message_id INTEGER,
        file_name TEXT,
        file_size INTEGER,
        mime_type TEXT,
        width INTEGER,
        height INTEGER,
        duration INTEGER,
        media_type TEXT,
        manual_tag TEXT,
        link_url TEXT,
        link_domain TEXT,
        page_title TEXT,
        is_favorite INTEGER DEFAULT 0,
        favorited_at TIMESTAMP DEFAULT NULL,
        context_text TEXT,
        context_message_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(folder_id) REFERENCES vault_folders(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        due_at TIMESTAMP NOT NULL,
        repeat_minutes INTEGER DEFAULT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP DEFAULT NULL,
        last_notified_at TIMESTAMP DEFAULT NULL,
        next_retry_at TIMESTAMP DEFAULT NULL,
        retry_count INTEGER DEFAULT 0,
        source_item_id INTEGER DEFAULT NULL,
        note TEXT DEFAULT NULL
    );

    CREATE TABLE IF NOT EXISTS collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name)
    );

    CREATE TABLE IF NOT EXISTS collection_items (
        collection_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(collection_id, item_id),
        FOREIGN KEY(collection_id) REFERENCES collections(id) ON DELETE CASCADE,
        FOREIGN KEY(item_id) REFERENCES vault_items(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS bot_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS rss_feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        latest_guid TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, url)
    );

    CREATE TABLE IF NOT EXISTS rss_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        guid TEXT NOT NULL,
        title TEXT,
        url TEXT,
        summary TEXT,
        published_at TEXT,
        saved_item_id INTEGER,
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(feed_id, guid),
        FOREIGN KEY(feed_id) REFERENCES rss_feeds(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS smart_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        query TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, name)
    );
    """)

    _ensure_column(conn, "vault_folders", "archived_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "vault_folders", "emoji TEXT")
    for row in conn.execute("SELECT id FROM vault_folders WHERE emoji IS NULL OR TRIM(emoji) = ''").fetchall():
        conn.execute("UPDATE vault_folders SET emoji = ? WHERE id = ?", (random_folder_emoji(), row["id"]))
    _ensure_column(conn, "vault_items", "tag TEXT")
    _ensure_column(conn, "vault_items", "telegram_file_id TEXT")
    _ensure_column(conn, "vault_items", "archive_chat_id INTEGER")
    _ensure_column(conn, "vault_items", "archive_message_id INTEGER")
    _ensure_column(conn, "vault_items", "item_state TEXT NOT NULL DEFAULT 'saved'")
    _ensure_column(conn, "vault_items", "dedupe_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vault_items_user_dedupe "
        "ON vault_items(user_id, dedupe_key)"
    )
    _ensure_column(conn, "vault_items", "voice_duration INTEGER")
    _ensure_column(conn, "vault_items", "extracted_text TEXT")
    _ensure_column(conn, "vault_items", "transcript_text TEXT")
    _ensure_column(conn, "vault_items", "ocr_status TEXT DEFAULT NULL")
    _ensure_column(conn, "vault_items", "transcript_status TEXT DEFAULT NULL")
    _ensure_column(conn, "vault_items", "processing_started_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "vault_items", "processing_completed_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "rss_entries", "notified INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_items", "sender_name TEXT")
    _ensure_column(conn, "vault_items", "sender_username TEXT")
    _ensure_column(conn, "vault_items", "source_chat_title TEXT")
    _ensure_column(conn, "vault_items", "source_chat_id INTEGER")
    _ensure_column(conn, "vault_items", "source_message_id INTEGER")
    _ensure_column(conn, "vault_items", "file_name TEXT")
    _ensure_column(conn, "vault_items", "file_size INTEGER")
    _ensure_column(conn, "vault_items", "mime_type TEXT")
    _ensure_column(conn, "vault_items", "width INTEGER")
    _ensure_column(conn, "vault_items", "height INTEGER")
    _ensure_column(conn, "vault_items", "duration INTEGER")
    _ensure_column(conn, "vault_items", "media_type TEXT")
    _ensure_column(conn, "vault_items", "manual_tag TEXT")
    _ensure_column(conn, "vault_items", "link_url TEXT")
    _ensure_column(conn, "vault_items", "link_domain TEXT")
    _ensure_column(conn, "vault_items", "page_title TEXT")
    _ensure_column(conn, "vault_items", "is_favorite INTEGER DEFAULT 0")
    _ensure_column(conn, "vault_items", "favorited_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "vault_items", "context_text TEXT")
    _ensure_column(conn, "vault_items", "context_message_id INTEGER")
    _ensure_column(conn, "vault_items", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    _ensure_column(conn, "reminders", "repeat_minutes INTEGER DEFAULT NULL")
    _ensure_column(conn, "reminders", "status TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(conn, "reminders", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    _ensure_column(conn, "reminders", "completed_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "reminders", "last_notified_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "reminders", "next_retry_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "reminders", "retry_count INTEGER DEFAULT 0")
    _ensure_column(conn, "reminders", "source_item_id INTEGER DEFAULT NULL")
    _ensure_column(conn, "reminders", "note TEXT DEFAULT NULL")

    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_vault_items_user_folder
        ON vault_items(user_id, folder_id);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_kind
        ON vault_items(user_id, kind);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_tag
        ON vault_items(user_id, tag);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_content
        ON vault_items(user_id, content);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_transcript
        ON vault_items(user_id, transcript_text);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_created
        ON vault_items(user_id, created_at);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_state
        ON vault_items(user_id, item_state, id DESC);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_favorite
        ON vault_items(user_id, is_favorite, favorited_at);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_manual_tag
        ON vault_items(user_id, manual_tag);

    CREATE INDEX IF NOT EXISTS idx_vault_items_user_link_domain
        ON vault_items(user_id, link_domain);

    CREATE INDEX IF NOT EXISTS idx_vault_folders_user_archived
        ON vault_folders(user_id, archived_at);

    CREATE INDEX IF NOT EXISTS idx_reminders_user_status_due_retry
        ON reminders(user_id, status, due_at, next_retry_at);

    CREATE INDEX IF NOT EXISTS idx_collections_user_name
        ON collections(user_id, name);

    CREATE INDEX IF NOT EXISTS idx_collection_items_item
        ON collection_items(item_id);
    """)

    conn.commit()
    conn.close()
