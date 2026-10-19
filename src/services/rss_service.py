from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import aiohttp

from src.db.database import get_connection


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_value(node: ET.Element, names: set[str]) -> str | None:
    for child in node.iter():
        if child is node:
            continue
        if _local_name(child.tag) in names:
            value = child.attrib.get("href", "") if _local_name(child.tag) == "link" else ""
            value = value or "".join(child.itertext()).strip()
            if value:
                return value
    return None


def parse_feed(xml: bytes, fallback_url: str) -> tuple[str, list[dict[str, str]]]:
    root = ET.fromstring(xml)
    feed_title = _child_value(root, {"title"}) or urlparse(fallback_url).netloc
    entries = []
    for node in root.iter():
        if _local_name(node.tag) not in {"item", "entry"}:
            continue
        title = _child_value(node, {"title"}) or "Новая публикация"
        link = _child_value(node, {"link"}) or _child_value(node, {"guid", "id"}) or fallback_url
        if urlparse(link).scheme not in {"http", "https"}:
            link = fallback_url
        guid = _child_value(node, {"guid", "id"}) or link or title
        summary = _child_value(node, {"description", "summary", "content"}) or ""
        published = _child_value(node, {"pubdate", "published", "updated", "date"}) or ""
        entries.append({
            "guid": guid[:1000],
            "title": title[:500],
            "url": link[:2000],
            "summary": " ".join(summary.split())[:2000],
            "published_at": published[:120],
        })
    return feed_title[:300], entries[:20]


class RSSService:
    @staticmethod
    async def fetch(url: str) -> tuple[str, list[dict[str, str]]]:
        timeout = aiohttp.ClientTimeout(total=12, connect=6)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "TelegramUtilityBot/1.0"}) as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                if response.content_length and response.content_length > 2_000_000:
                    raise ValueError("RSS feed is larger than 2 MB")
                return parse_feed(await response.read(), url)

    @staticmethod
    def add(user_id: int, url: str, title: str, latest_guid: str | None):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO rss_feeds (user_id, url, title, latest_guid) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, url) DO UPDATE SET title = excluded.title, latest_guid = excluded.latest_guid",
                (user_id, url, title, latest_guid),
            )
            conn.commit()
            return conn.execute("SELECT * FROM rss_feeds WHERE user_id = ? AND url = ?", (user_id, url)).fetchone()
        finally:
            conn.close()

    @staticmethod
    def list_for_user(user_id: int):
        conn = get_connection()
        try:
            return conn.execute("SELECT * FROM rss_feeds WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()
        finally:
            conn.close()

    @staticmethod
    def get_all():
        conn = get_connection()
        try:
            return conn.execute("SELECT * FROM rss_feeds ORDER BY id").fetchall()
        finally:
            conn.close()

    @staticmethod
    def remove(user_id: int, feed_id: int) -> bool:
        conn = get_connection()
        try:
            cursor = conn.execute("DELETE FROM rss_feeds WHERE user_id = ? AND id = ?", (user_id, feed_id))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def store_new_entries(feed_id: int, user_id: int, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        conn = get_connection()
        try:
            feed = conn.execute("SELECT latest_guid FROM rss_feeds WHERE id = ? AND user_id = ?", (feed_id, user_id)).fetchone()
            if not feed:
                return []
            latest_guid = feed["latest_guid"]
            new_entries = []
            if latest_guid:
                for entry in entries:
                    if entry["guid"] == latest_guid:
                        break
                    new_entries.append(entry)
            # If the feed has rolled over and no longer contains its previous newest entry,
            # skip this poll instead of flooding the user with old posts.
            if latest_guid and not any(entry["guid"] == latest_guid for entry in entries):
                new_entries = []

            stored = []
            # Process oldest unseen items first and advance the watermark gradually,
            # so feeds with several new posts don't silently skip the remainder.
            batch = new_entries[-5:]
            for entry in reversed(batch):
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO rss_entries (feed_id, user_id, guid, title, url, summary, published_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (feed_id, user_id, entry["guid"], entry["title"], entry["url"], entry["summary"], entry["published_at"]),
                )
                if cursor.rowcount:
                    row = conn.execute("SELECT * FROM rss_entries WHERE id = last_insert_rowid()").fetchone()
                    stored.append(dict(row))

            if entries:
                new_watermark = new_entries[-5]["guid"] if len(new_entries) > 5 else entries[0]["guid"]
                conn.execute("UPDATE rss_feeds SET latest_guid = ? WHERE id = ?", (new_watermark, feed_id))
            conn.commit()
            return stored
        finally:
            conn.close()

    @staticmethod
    def set_saved_item(entry_id: int, user_id: int, item_id: int) -> None:
        conn = get_connection()
        try:
            conn.execute("UPDATE rss_entries SET saved_item_id = ? WHERE id = ? AND user_id = ?", (item_id, entry_id, user_id))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def get_entry(user_id: int, entry_id: int):
        conn = get_connection()
        try:
            return conn.execute("SELECT * FROM rss_entries WHERE id = ? AND user_id = ?", (entry_id, user_id)).fetchone()
        finally:
            conn.close()

    @staticmethod
    def pending_notifications(feed_id: int, user_id: int):
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT * FROM rss_entries WHERE feed_id = ? AND user_id = ? AND notified = 0 ORDER BY id",
                (feed_id, user_id),
            ).fetchall()
        finally:
            conn.close()

    @staticmethod
    def mark_notified(entry_id: int) -> None:
        conn = get_connection()
        try:
            conn.execute("UPDATE rss_entries SET notified = 1 WHERE id = ?", (entry_id,))
            conn.commit()
        finally:
            conn.close()
