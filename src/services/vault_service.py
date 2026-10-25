from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from src.db.database import get_connection
from src.services.tag_service import build_auto_tags, format_tags, merge_user_tags
from src.utils.folder_emojis import random_folder_emoji


class VaultService:
    DEFAULT_FOLDER_NAME = "Inbox"

    @staticmethod
    def _row_to_dict(row):
        return dict(row) if row is not None else None

    @staticmethod
    def create_folder(user_id: int, name: str):
        name = (name or "").strip()
        if not name:
            return None

        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO vault_folders (user_id, name, archived_at, emoji)
            VALUES (?, ?, NULL, ?)
            """,
            (user_id, name, random_folder_emoji()),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_folders
            WHERE user_id = ? AND name = ?
            """,
            (user_id, name),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def get_or_create_default_folder(user_id: int):
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO vault_folders (user_id, name, archived_at, emoji)
            VALUES (?, ?, NULL, '📥')
            """,
            (user_id, VaultService.DEFAULT_FOLDER_NAME),
        )
        conn.execute(
            """
            UPDATE vault_folders
            SET archived_at = NULL
            WHERE user_id = ? AND name = ?
            """,
            (user_id, VaultService.DEFAULT_FOLDER_NAME),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_folders
            WHERE user_id = ? AND name = ?
            """,
            (user_id, VaultService.DEFAULT_FOLDER_NAME),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def get_folder_by_id(user_id: int, folder_id: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT *
            FROM vault_folders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, folder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def list_folders(user_id: int, include_archived: bool = False):
        conn = get_connection()
        if include_archived:
            rows = conn.execute(
                """
                SELECT *
                FROM vault_folders
                WHERE user_id = ?
                ORDER BY archived_at IS NOT NULL, id ASC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM vault_folders
                WHERE user_id = ? AND archived_at IS NULL
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def rename_folder(user_id: int, folder_id: int, new_name: str):
        new_name = (new_name or "").strip()
        if not new_name:
            return None

        conn = get_connection()
        conn.execute(
            """
            UPDATE vault_folders
            SET name = ?
            WHERE user_id = ? AND id = ?
            """,
            (new_name, user_id, folder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_folders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, folder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def set_folder_archived(user_id: int, folder_id: int, archived: bool):
        conn = get_connection()
        value = datetime.utcnow().isoformat(sep=" ", timespec="seconds") if archived else None
        conn.execute(
            """
            UPDATE vault_folders
            SET archived_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (value, user_id, folder_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_folders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, folder_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def archive_folder(user_id: int, folder_id: int):
        return VaultService.set_folder_archived(user_id, folder_id, True)

    @staticmethod
    def restore_folder(user_id: int, folder_id: int):
        return VaultService.set_folder_archived(user_id, folder_id, False)

    @staticmethod
    def delete_folder(user_id: int, folder_id: int):
        conn = get_connection()
        conn.execute(
            """
            DELETE FROM vault_folders
            WHERE user_id = ? AND id = ?
            """,
            (user_id, folder_id),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _media_default_status(kind: str):
        if kind in {"voice", "audio"}:
            return "not_requested"
        return None

    @staticmethod
    def _normalize_tags(tag: str | None, *texts: str | None):
        auto = build_auto_tags(*texts)
        return merge_user_tags(format_tags(tag), auto)

    @staticmethod

    def save_item(
        user_id: int,
        folder_id: int,
        kind: str = "text",
        content: str | None = None,
        tag: str | None = None,
        telegram_file_id: str | None = None,
        archive_chat_id: int | None = None,
        archive_message_id: int | None = None,
        item_state: str = "saved",
        dedupe_key: str | None = None,
        voice_duration: int | None = None,
        transcript_text: str | None = None,
        transcript_status: str | None = None,
        processing_started_at: str | None = None,
        processing_completed_at: str | None = None,
        sender_name: str | None = None,
        sender_username: str | None = None,
        is_forwarded: bool = False,
        source_chat_title: str | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        file_name: str | None = None,
        file_size: int | None = None,
        mime_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
        media_type: str | None = None,
        manual_tag: str | None = None,
        link_url: str | None = None,
        link_domain: str | None = None,
        page_title: str | None = None,
        is_favorite: int = 0,
        favorited_at: str | None = None,
    ):
        if transcript_status is None:
            transcript_status = VaultService._media_default_status(kind)

        manual_tag = format_tags(manual_tag if manual_tag is not None else tag)
        tag = VaultService._normalize_tags(tag, content, transcript_text, file_name)

        conn = get_connection()
        cursor = conn.execute(
            """
            INSERT INTO vault_items (
                user_id,
                folder_id,
                kind,
                content,
                tag,
                telegram_file_id,
                archive_chat_id,
                archive_message_id,
                item_state,
                dedupe_key,
                voice_duration,
                transcript_text,
                transcript_status,
                processing_started_at,
                processing_completed_at,
                sender_name,
                sender_username,
                is_forwarded,
                source_chat_title,
                source_chat_id,
                source_message_id,
                file_name,
                file_size,
                mime_type,
                width,
                height,
                duration,
                media_type,
                manual_tag,
                link_url,
                link_domain,
                page_title,
                is_favorite,
                favorited_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                folder_id,
                kind,
                content,
                tag,
                telegram_file_id,
                archive_chat_id,
                archive_message_id,
                item_state,
                dedupe_key,
                voice_duration,
                transcript_text,
                transcript_status,
                processing_started_at,
                processing_completed_at,
                sender_name,
                sender_username,
                int(is_forwarded),
                source_chat_title,
                source_chat_id,
                source_message_id,
                file_name,
                file_size,
                mime_type,
                width,
                height,
                duration,
                media_type,
                manual_tag,
                link_url,
                link_domain,
                page_title,
                is_favorite,
                favorited_at,
            ),
        )
        conn.commit()
        item_id = None
        if archive_chat_id is not None and archive_message_id is not None:
            inserted = conn.execute(
                "SELECT id FROM vault_items WHERE user_id = ? AND archive_chat_id = ? AND archive_message_id = ? ORDER BY id DESC LIMIT 1",
                (user_id, archive_chat_id, archive_message_id),
            ).fetchone()
            item_id = inserted["id"] if inserted else None
        if not item_id:
            item_id = cursor.lastrowid
        if not item_id:
            inserted = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
            item_id = inserted["id"] if inserted else None
        if item_id is None:
            conn.close()
            raise RuntimeError("Database did not return the ID of the saved archive item")
        item_id = int(item_id)
        conn.close()
        return item_id

    @staticmethod
    def list_items(
        user_id: int,
        folder_id: int | None = None,
        include_archived: bool = False,
    ):
        conn = get_connection()
        if folder_id is None:
            if include_archived:
                rows = conn.execute(
                    """
                    SELECT i.*
                    FROM vault_items i
                    JOIN vault_folders f ON f.id = i.folder_id
                    WHERE i.user_id = ?
                    ORDER BY i.id DESC
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT i.*
                    FROM vault_items i
                    JOIN vault_folders f ON f.id = i.folder_id
                    WHERE i.user_id = ? AND f.archived_at IS NULL
                    ORDER BY i.id DESC
                    """,
                    (user_id,),
                ).fetchall()
        else:
            if include_archived:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM vault_items
                    WHERE user_id = ? AND folder_id = ?
                    ORDER BY id DESC
                    """,
                    (user_id, folder_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT i.*
                    FROM vault_items i
                    JOIN vault_folders f ON f.id = i.folder_id
                    WHERE i.user_id = ? AND i.folder_id = ? AND f.archived_at IS NULL
                    ORDER BY i.id DESC
                    """,
                    (user_id, folder_id),
                ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_item(user_id: int, item_id: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT *
            FROM vault_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, item_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def find_duplicate_item(user_id: int, dedupe_key: str):
        """Return the newest saved item with this user's stable content key."""
        if not dedupe_key:
            return None
        conn = get_connection()
        row = conn.execute(
            """
            SELECT *
            FROM vault_items
            WHERE user_id = ? AND dedupe_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, dedupe_key),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def list_items_by_state(user_id: int, item_state: str, *, limit: int = 200):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT i.*
            FROM vault_items i
            JOIN vault_folders f ON f.id = i.folder_id
            WHERE i.user_id = ? AND i.item_state = ? AND f.archived_at IS NULL
            ORDER BY i.id DESC
            LIMIT ?
            """,
            (user_id, item_state, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def set_item_state(user_id: int, item_id: int, item_state: str):
        if item_state not in {"inbox", "later", "saved", "done"}:
            return None
        conn = get_connection()
        cursor = conn.execute(
            "UPDATE vault_items SET item_state = ? WHERE user_id = ? AND id = ?",
            (item_state, user_id, item_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM vault_items WHERE user_id = ? AND id = ?",
            (user_id, item_id),
        ).fetchone() if cursor.rowcount else None
        conn.close()
        return row

    @staticmethod
    def count_items_by_state(user_id: int, item_state: str) -> int:
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM vault_items i
                JOIN vault_folders f ON f.id = i.folder_id
                WHERE i.user_id = ? AND i.item_state = ? AND f.archived_at IS NULL
                """,
                (user_id, item_state),
            ).fetchone()
            return int(row["total"] or 0)
        finally:
            conn.close()

    @staticmethod
    def set_all_items_by_state(user_id: int, from_state: str, to_state: str) -> int:
        if from_state not in {"inbox", "later", "saved", "done"} or to_state not in {"inbox", "later", "saved", "done"}:
            return 0
        conn = get_connection()
        try:
            cursor = conn.execute(
                """
                UPDATE vault_items
                SET item_state = ?
                WHERE user_id = ? AND item_state = ?
                  AND folder_id IN (SELECT id FROM vault_folders WHERE user_id = ? AND archived_at IS NULL)
                """,
                (to_state, user_id, from_state, user_id),
            )
            conn.commit()
            return int(cursor.rowcount)
        finally:
            conn.close()

    @staticmethod
    def find_duplicate(user_id: int, dedupe_key: str):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT * FROM vault_items
            WHERE user_id = ? AND dedupe_key = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, dedupe_key),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def random_item(user_id: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT i.*
            FROM vault_items i
            JOIN vault_folders f ON f.id = i.folder_id
            WHERE i.user_id = ? AND f.archived_at IS NULL
              AND i.created_at <= datetime('now', '-7 days')
            ORDER BY RANDOM() LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT i.*
                FROM vault_items i
                JOIN vault_folders f ON f.id = i.folder_id
                WHERE i.user_id = ? AND f.archived_at IS NULL
                ORDER BY RANDOM() LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        conn.close()
        return row

    @staticmethod
    def delete_item(user_id: int, item_id: int):
        conn = get_connection()
        conn.execute(
            """
            DELETE FROM vault_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, item_id),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def move_item(user_id: int, item_id: int, folder_id: int):
        conn = get_connection()
        conn.execute(
            """
            UPDATE vault_items
            SET folder_id = ?
            WHERE user_id = ? AND id = ?
            """,
            (folder_id, user_id, item_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, item_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def update_item_tag(user_id: int, item_id: int, tag: str | None):
        tag = tag.strip() if tag else None
        if tag == "":
            tag = None

        conn = get_connection()
        conn.execute(
            """
            UPDATE vault_items
            SET tag = ?,
                manual_tag = ?
            WHERE user_id = ? AND id = ?
            """,
            (tag, tag, user_id, item_id),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT *
            FROM vault_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, item_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def update_item_context(
        user_id: int,
        item_id: int,
        context_text: str,
        context_message_id: int,
    ):
        conn = get_connection()
        cursor = conn.execute(
            """
            UPDATE vault_items
            SET context_text = ?, context_message_id = ?
            WHERE user_id = ? AND id = ?
            """,
            (context_text, context_message_id, user_id, item_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM vault_items WHERE user_id = ? AND id = ?",
            (user_id, item_id),
        ).fetchone() if cursor.rowcount else None
        conn.close()
        return row

    @staticmethod
    def update_media_processing_started(
        user_id: int | None,
        item_id: int,
        *,
        transcript_status: str | None = None,
    ):
        conn = get_connection()
        now = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")
        conn.execute(
            """
            UPDATE vault_items
            SET processing_started_at = ?,
                processing_completed_at = NULL,
                transcript_status = COALESCE(?, transcript_status)
            WHERE id = ?
            """,
            (now, transcript_status, item_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM vault_items WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        return row


    @staticmethod
    def update_media_processing_result(
        item_id: int,
        *,
        transcript_text: str | None = None,
        transcript_status: str | None = None,
        processing_completed_at: str | None = None,
    ):
        conn = get_connection()
        current = conn.execute("SELECT * FROM vault_items WHERE id = ?", (item_id,)).fetchone()
        if not current:
            conn.close()
            return None

        if processing_completed_at is None:
            processing_completed_at = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")

        conn.execute(
            """
            UPDATE vault_items
            SET transcript_text = COALESCE(?, transcript_text),
                transcript_status = COALESCE(?, transcript_status),
                processing_completed_at = ?
            WHERE id = ?
            """,
            (
                transcript_text,
                transcript_status,
                processing_completed_at,
                item_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM vault_items WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        return row

    @staticmethod
    def search_items(
        user_id: int,
        query: str,
        folder_id: int | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        limit: int = 200,
        deep: bool = False,
        tags: list[str] | None = None,
        domain: str | None = None,
        sender: str | None = None,
        before: str | None = None,
        after: str | None = None,
        today: bool = False,
        untagged: bool = False,
    ):
        query = (query or "").strip()
        like = f"%{query.lower()}%"

        clauses = ["i.user_id = ?"]
        params = [user_id]

        if folder_id is not None:
            clauses.append("i.folder_id = ?")
            params.append(folder_id)

        if kind:
            normalized_kind = kind.lower()
            if normalized_kind == "transcript":
                clauses.append("i.kind IN ('voice', 'audio')")
                deep = True
            elif normalized_kind == "link":
                clauses.append("(i.kind = 'link' OR i.media_type = 'link' OR i.link_url IS NOT NULL)")
            elif normalized_kind in {"voice", "audio", "photo", "text", "document", "video", "animation", "sticker", "video_note"}:
                clauses.append("i.kind = ?")
                params.append(normalized_kind)

        if tags:
            for tag in tags:
                clauses.append("LOWER(COALESCE(i.tag, '')) LIKE ?")
                params.append(f"%{tag.lower().lstrip('#')}%")

        if domain:
            clauses.append("LOWER(COALESCE(i.link_domain, '')) LIKE ?")
            params.append(f"%{domain.lower().removeprefix('www.')}%")

        if sender:
            sender_like = f"%{sender.lower().lstrip('@')}%"
            clauses.append(
                "("
                "LOWER(COALESCE(i.sender_username, '')) LIKE ? OR "
                "LOWER(COALESCE(i.sender_name, '')) LIKE ? OR "
                "LOWER(COALESCE(i.source_chat_title, '')) LIKE ?"
                ")"
            )
            params.extend([sender_like, sender_like, sender_like])

        if before:
            clauses.append("DATE(i.created_at) <= DATE(?)")
            params.append(before)

        if after:
            clauses.append("DATE(i.created_at) >= DATE(?)")
            params.append(after)

        if today:
            clauses.append("DATE(i.created_at) = DATE('now')")

        if untagged:
            clauses.append("(i.manual_tag IS NULL OR TRIM(i.manual_tag) = '')")

        if query:
            search_clauses = [
                "LOWER(COALESCE(i.content, '')) LIKE ?",
                "LOWER(COALESCE(i.context_text, '')) LIKE ?",
                "LOWER(COALESCE(i.tag, '')) LIKE ?",
                "LOWER(COALESCE(i.transcript_text, '')) LIKE ?",
                "LOWER(COALESCE(i.file_name, '')) LIKE ?",
                "LOWER(COALESCE(i.page_title, '')) LIKE ?",
                "LOWER(COALESCE(i.link_url, '')) LIKE ?",
                "LOWER(COALESCE(i.link_domain, '')) LIKE ?",
                "LOWER(COALESCE(i.sender_name, '')) LIKE ?",
                "LOWER(COALESCE(i.sender_username, '')) LIKE ?",
                "LOWER(COALESCE(i.source_chat_title, '')) LIKE ?",
                "LOWER(COALESCE(f.name, '')) LIKE ?",
            ]
            search_params = [like] * len(search_clauses)

            if deep:
                search_clauses.extend([
                    "LOWER(COALESCE(i.media_type, '')) LIKE ?",
                ])
                search_params.extend([like])

            clauses.append("(" + " OR ".join(search_clauses) + ")")
            params.extend(search_params)

        if not include_archived:
            clauses.append("f.archived_at IS NULL")

        sql = f"""
            SELECT i.*
            FROM vault_items i
            JOIN vault_folders f ON f.id = i.folder_id
            WHERE {' AND '.join(clauses)}
            ORDER BY i.id DESC
            LIMIT ?
        """
        params.append(limit)

        conn = get_connection()
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    @staticmethod
    def search_count(
        user_id: int,
        query: str,
        folder_id: int | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        deep: bool = False,
    ) -> int:
        rows = VaultService.search_items(
            user_id=user_id,
            query=query,
            folder_id=folder_id,
            kind=kind,
            include_archived=include_archived,
            limit=100000,
            deep=deep,
        )
        return len(rows)

    @staticmethod
    def list_favorites(user_id: int, include_archived: bool = False):
        conn = get_connection()
        if include_archived:
            rows = conn.execute(
                """
                SELECT i.*
                FROM vault_items i
                WHERE i.user_id = ? AND COALESCE(i.is_favorite, 0) = 1
                ORDER BY COALESCE(i.favorited_at, i.created_at) DESC, i.id DESC
                """,
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT i.*
                FROM vault_items i
                JOIN vault_folders f ON f.id = i.folder_id
                WHERE i.user_id = ? AND COALESCE(i.is_favorite, 0) = 1 AND f.archived_at IS NULL
                ORDER BY COALESCE(i.favorited_at, i.created_at) DESC, i.id DESC
                """,
                (user_id,),
            ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def toggle_favorite(user_id: int, item_id: int):
        conn = get_connection()
        row = conn.execute(
            """
            SELECT COALESCE(is_favorite, 0) AS is_favorite
            FROM vault_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, item_id),
        ).fetchone()
        if not row:
            conn.close()
            return None

        new_value = 0 if int(row["is_favorite"] or 0) else 1
        favorited_at = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ") if new_value else None
        conn.execute(
            """
            UPDATE vault_items
            SET is_favorite = ?,
                favorited_at = ?
            WHERE user_id = ? AND id = ?
            """,
            (new_value, favorited_at, user_id, item_id),
        )
        conn.commit()
        out = conn.execute(
            "SELECT * FROM vault_items WHERE user_id = ? AND id = ?",
            (user_id, item_id),
        ).fetchone()
        conn.close()
        return out

    @staticmethod
    def update_item_tags(user_id: int, item_id: int, tags: str | None):
        tags = format_tags(tags)
        conn = get_connection()
        conn.execute(
            """
            UPDATE vault_items
            SET tag = ?,
                manual_tag = ?
            WHERE user_id = ? AND id = ?
            """,
            (tags, tags, user_id, item_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM vault_items WHERE user_id = ? AND id = ?",
            (user_id, item_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def count_items(user_id: int | None = None) -> int:
        conn = get_connection()
        if user_id is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM vault_items").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM vault_items WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return int(row["n"] or 0)

    @staticmethod
    def count_folders(user_id: int | None = None) -> int:
        conn = get_connection()
        if user_id is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM vault_folders").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM vault_folders WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
        return int(row["n"] or 0)

    @staticmethod
    def list_links(user_id: int, *, domain: str | None = None, limit: int = 50):
        return VaultService.search_items(
            user_id=user_id,
            query="",
            kind="link",
            domain=domain,
            include_archived=False,
            limit=limit,
        )

    @staticmethod
    def list_untagged(user_id: int, *, limit: int = 50):
        return VaultService.search_items(
            user_id=user_id,
            query="",
            untagged=True,
            include_archived=False,
            limit=limit,
        )

    @staticmethod
    def list_today(user_id: int, *, limit: int = 50):
        return VaultService.search_items(
            user_id=user_id,
            query="",
            today=True,
            include_archived=False,
            limit=limit,
        )

    @staticmethod
    def review_candidates(user_id: int, *, limit: int = 5):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT i.*
            FROM vault_items i
            JOIN vault_folders f ON f.id = i.folder_id
            WHERE i.user_id = ?
              AND f.archived_at IS NULL
              AND (
                    i.manual_tag IS NULL
                 OR TRIM(i.manual_tag) = ''
                 OR f.name = ?
              )
            ORDER BY
                CASE WHEN i.manual_tag IS NULL OR TRIM(i.manual_tag) = '' THEN 0 ELSE 1 END,
                i.created_at ASC,
                i.id ASC
            LIMIT ?
            """,
            (user_id, VaultService.DEFAULT_FOLDER_NAME, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def source_stats(user_id: int, *, limit: int = 30):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(source_chat_title, ''), NULLIF(sender_username, ''), NULLIF(sender_name, ''), 'direct') AS source,
                COUNT(*) AS total,
                MAX(created_at) AS last_at
            FROM vault_items
            WHERE user_id = ?
            GROUP BY source
            ORDER BY total DESC, last_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def source_items(user_id: int, source_query: str, *, limit: int = 50):
        like = f"%{source_query.lower().lstrip('@')}%"
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT *
            FROM vault_items
            WHERE user_id = ?
              AND (
                    LOWER(COALESCE(source_chat_title, '')) LIKE ?
                 OR LOWER(COALESCE(sender_username, '')) LIKE ?
                 OR LOWER(COALESCE(sender_name, '')) LIKE ?
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, like, like, like, limit),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_or_create_collection(user_id: int, name: str):
        name = (name or "").strip()
        if not name:
            return None
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO collections (user_id, name)
            VALUES (?, ?)
            """,
            (user_id, name),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM collections WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def list_collections(user_id: int):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT c.*, COUNT(ci.item_id) AS item_count
            FROM collections c
            LEFT JOIN collection_items ci ON ci.collection_id = c.id
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.name COLLATE NOCASE ASC
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        return rows

    @staticmethod
    def get_collection(user_id: int, collection_id: int):
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM collections WHERE user_id = ? AND id = ?",
            (user_id, collection_id),
        ).fetchone()
        conn.close()
        return row

    @staticmethod
    def add_item_to_collection(user_id: int, item_id: int, collection_name: str):
        item = VaultService.get_item(user_id, item_id)
        if not item:
            return None
        collection = VaultService.get_or_create_collection(user_id, collection_name)
        if not collection:
            return None
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO collection_items (collection_id, item_id)
            VALUES (?, ?)
            """,
            (collection["id"], item_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (collection["id"],)).fetchone()
        conn.close()
        return row

    @staticmethod
    def collection_items(user_id: int, collection_name: str, *, limit: int = 100):
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT i.*
            FROM vault_items i
            JOIN collection_items ci ON ci.item_id = i.id
            JOIN collections c ON c.id = ci.collection_id
            WHERE c.user_id = ? AND LOWER(c.name) = LOWER(?)
            ORDER BY ci.created_at DESC, i.id DESC
            LIMIT ?
            """,
            (user_id, collection_name.strip(), limit),
        ).fetchall()
        conn.close()
        return rows

    def clear_folder(user_id: int, folder_id: int):
        conn = get_connection()
        conn.execute(
            """
            DELETE FROM vault_items
            WHERE user_id = ? AND folder_id = ?
            """,
            (user_id, folder_id),
        )
        conn.commit()
        conn.close()
