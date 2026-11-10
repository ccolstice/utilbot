from __future__ import annotations

import os
import sqlite3
import logging
from pathlib import Path

from src.utils.folder_emojis import random_folder_emoji

DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "sqlite").strip().lower()
DATABASE_FILE = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
if DATABASE_BACKEND == "sqlite":
    DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
elif DATABASE_BACKEND != "turso":
    raise RuntimeError("DATABASE_BACKEND must be 'sqlite' or 'turso'")


class _NamedRow:
    """sqlite3.Row-like access for DB-API drivers that return tuples."""
    def __init__(self, names, values):
        self._names = list(names)
        self._values = tuple(values)
        self._mapping = dict(zip(self._names, self._values))

    def __getitem__(self, key):
        return self._mapping[key] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names


class _TursoCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        description = getattr(cursor, "description", None) or ()
        self._names = [
            column[0] if isinstance(column, (tuple, list))
            else getattr(column, "name", column)
            for column in description
        ]
        self.rowcount = getattr(cursor, "rowcount", -1)
        self.lastrowid = getattr(cursor, "lastrowid", None)

    def fetchone(self):
        row = self._cursor.fetchone()
        return _NamedRow(self._names, row) if row is not None and self._names else row

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [_NamedRow(self._names, row) for row in rows] if self._names else rows

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                break
            yield row


class _TursoConnection:
    """Small DB-API compatibility shim; queries go only to Turso, never local SQLite."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return _TursoCursor(self._connection.execute(sql, parameters))

    def executescript(self, script):
        # sqlite3.complete_statement correctly handles semicolons inside strings.
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    self.execute(statement)
                statement = ""
        if statement.strip():
            self.execute(statement)

    def commit(self):
        self._connection.commit()

    def close(self):
        self._connection.close()

    def rollback(self):
        self._connection.rollback()


def get_connection():
    if DATABASE_BACKEND == "turso":
        url = os.getenv("TURSO_DATABASE_URL", "").strip()
        token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        if not url or not token:
            raise RuntimeError("DATABASE_BACKEND=turso requires TURSO_DATABASE_URL and TURSO_AUTH_TOKEN; refusing local fallback")
        try:
            import libsql
        except ImportError as exc:
            raise RuntimeError("Turso driver is missing. Rebuild the deployment with requirements.txt.") from exc
        return _TursoConnection(libsql.connect(database=url, auth_token=token))
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
    if DATABASE_BACKEND == "turso":
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
        except Exception:
            # Turso does not implement every SQLite PRAGMA; keep the startup
            # explicit and rely on the database's configured FK behavior.
            logging.getLogger(__name__).warning("Turso did not accept PRAGMA foreign_keys; continuing with server defaults")
    else:
        conn.execute("PRAGMA foreign_keys = ON;")

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
        ai_description TEXT,
        ai_tags TEXT,
        ai_previous_description TEXT,
        ai_previous_tags TEXT,
        ai_previous_ai_tags TEXT,
        ai_previous_content TEXT,
        ai_previous_transcript_text TEXT,
        ai_previous_manual_tag TEXT,
        ai_previous_content_set INTEGER NOT NULL DEFAULT 0,
        ai_previous_transcript_set INTEGER NOT NULL DEFAULT 0,
        ai_previous_manual_tag_set INTEGER NOT NULL DEFAULT 0,
        ai_pending_description TEXT,
        ai_pending_tags TEXT,
        ai_pending_transcript TEXT,
        ai_analysis_active INTEGER NOT NULL DEFAULT 0,
        tag TEXT,
        telegram_file_id TEXT,
        media_group_json TEXT,
        archive_chat_id INTEGER,
        archive_message_id INTEGER,
        archive_message_ids TEXT,
        item_state TEXT NOT NULL DEFAULT 'saved',
        dedupe_key TEXT,
        voice_duration INTEGER,
        transcript_text TEXT,
        transcript_status TEXT DEFAULT NULL,
        ocr_text TEXT,
        ocr_results_json TEXT NOT NULL DEFAULT '{}',
        ocr_pending_text TEXT,
        ocr_pending_image_index INTEGER,
        processing_started_at TIMESTAMP DEFAULT NULL,
        processing_completed_at TIMESTAMP DEFAULT NULL,
        sender_name TEXT,
        sender_username TEXT,
        is_forwarded INTEGER NOT NULL DEFAULT 0,
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

    CREATE TABLE IF NOT EXISTS reminder_notifications (
        reminder_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (reminder_id, chat_id, message_id)
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

    CREATE TABLE IF NOT EXISTS secretary_chats (
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        chat_type TEXT NOT NULL DEFAULT 'private',
        title TEXT,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        last_seen_at REAL NOT NULL,
        PRIMARY KEY (business_connection_id, chat_id)
    );

    CREATE TABLE IF NOT EXISTS secretary_sessions (
        owner_id INTEGER PRIMARY KEY,
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS secretary_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        business_connection_id TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        message_id INTEGER,
        payload TEXT NOT NULL DEFAULT '{}',
        next_at REAL NOT NULL,
        expires_at REAL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS secretary_meta (
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
    _ensure_column(conn, "vault_items", "media_group_json TEXT")
    _ensure_column(conn, "vault_items", "archive_chat_id INTEGER")
    _ensure_column(conn, "vault_items", "archive_message_id INTEGER")
    _ensure_column(conn, "vault_items", "archive_message_ids TEXT")
    _ensure_column(conn, "vault_items", "item_state TEXT NOT NULL DEFAULT 'saved'")
    _ensure_column(conn, "vault_items", "dedupe_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_vault_items_user_dedupe "
        "ON vault_items(user_id, dedupe_key)"
    )
    _ensure_column(conn, "vault_items", "voice_duration INTEGER")
    _ensure_column(conn, "vault_items", "transcript_text TEXT")
    _ensure_column(conn, "vault_items", "transcript_status TEXT DEFAULT NULL")
    _ensure_column(conn, "vault_items", "ocr_text TEXT")
    _ensure_column(conn, "vault_items", "ocr_results_json TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "vault_items", "ocr_pending_text TEXT")
    _ensure_column(conn, "vault_items", "ocr_pending_image_index INTEGER")
    _ensure_column(conn, "vault_items", "processing_started_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "vault_items", "processing_completed_at TIMESTAMP DEFAULT NULL")
    _ensure_column(conn, "rss_entries", "notified INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_items", "sender_name TEXT")
    _ensure_column(conn, "vault_items", "sender_username TEXT")
    _ensure_column(conn, "vault_items", "is_forwarded INTEGER NOT NULL DEFAULT 0")
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
    _ensure_column(conn, "vault_items", "ai_description TEXT")
    _ensure_column(conn, "vault_items", "ai_tags TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_description TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_tags TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_ai_tags TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_content TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_transcript_text TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_manual_tag TEXT")
    _ensure_column(conn, "vault_items", "ai_previous_content_set INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_items", "ai_previous_transcript_set INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_items", "ai_previous_manual_tag_set INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "vault_items", "ai_pending_description TEXT")
    _ensure_column(conn, "vault_items", "ai_pending_tags TEXT")
    _ensure_column(conn, "vault_items", "ai_pending_transcript TEXT")
    _ensure_column(conn, "vault_items", "ai_analysis_active INTEGER NOT NULL DEFAULT 0")
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

    CREATE INDEX IF NOT EXISTS idx_secretary_chats_recent
        ON secretary_chats(last_seen_at DESC);

    CREATE INDEX IF NOT EXISTS idx_secretary_jobs_due
        ON secretary_jobs(status, next_at);
    """)

    conn.commit()
    conn.close()


def create_sqlite_snapshot(destination: Path) -> None:
    """Write a consistent portable SQLite snapshot of the configured database."""
    if DATABASE_BACKEND == "sqlite":
        source = sqlite3.connect(str(DATABASE_FILE))
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return

    conn = get_connection()
    target = sqlite3.connect(str(destination))
    try:
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END"
        ).fetchall()
        table_names = [row["name"] for row in objects if row["type"] == "table"]
        for row in objects:
            if row["type"] == "table":
                target.execute(row["sql"])
        for table in table_names:
            quoted = '"' + table.replace('"', '""') + '"'
            cursor = conn.execute(f"SELECT * FROM {quoted}")
            rows = cursor.fetchall()
            if not rows:
                continue
            columns = rows[0].keys() if hasattr(rows[0], "keys") else cursor._names
            marks = ",".join("?" for _ in columns)
            target.executemany(f"INSERT INTO {quoted} VALUES ({marks})", [tuple(row) for row in rows])
        for row in objects:
            if row["type"] != "table":
                target.execute(row["sql"])
        target.commit()
    finally:
        target.close()
        conn.close()


def restore_from_sqlite_snapshot(source_path: Path) -> None:
    """Restore a validated SQLite snapshot into the active backend."""
    if DATABASE_BACKEND == "sqlite":
        source = sqlite3.connect(str(source_path))
        destination = sqlite3.connect(str(DATABASE_FILE))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return

    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    remote = get_connection()
    try:
        objects = source.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END"
        ).fetchall()
        try:
            remote.execute("PRAGMA foreign_keys = OFF")
        except Exception:
            pass
        remote.execute("BEGIN")
        existing_views = remote.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()
        existing = remote.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for row in existing_views:
            name = row["name"]
            quoted = '"' + name.replace('"', '""') + '"'
            remote.execute(f"DROP VIEW IF EXISTS {quoted}")
        for row in reversed(existing):
            name = row["name"]
            quoted = '"' + name.replace('"', '""') + '"'
            remote.execute(f"DROP TABLE IF EXISTS {quoted}")
        for row in objects:
            if row["type"] == "table":
                remote.execute(row["sql"])
        for row in objects:
            if row["type"] == "table":
                name = row["name"]
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [column[1] for column in source.execute(f"PRAGMA table_info({quoted})").fetchall()]
                marks = ",".join("?" for _ in columns)
                for values in source.execute(f"SELECT * FROM {quoted}"):
                    remote.execute(f"INSERT INTO {quoted} VALUES ({marks})", tuple(values))
        for row in objects:
            if row["type"] != "table":
                remote.execute(row["sql"])
        remote.commit()
    except Exception:
        try:
            remote.rollback()
        except Exception:
            logging.getLogger(__name__).exception("Turso rollback failed after restore error")
        raise
    finally:
        source.close()
        remote.close()
