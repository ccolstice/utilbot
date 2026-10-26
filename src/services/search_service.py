from __future__ import annotations

import re

from src.services.vault_service import VaultService
from src.utils.search_parser import parse_search_request


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = {
    "а", "без", "бы", "был", "была", "были", "быть", "в", "вам", "вас", "во",
    "вот", "все", "всё", "для", "до", "его", "ее", "её", "если", "есть", "же",
    "за", "и", "из", "или", "к", "как", "ко", "мне", "мы", "на", "над", "не",
    "него", "нее", "неё", "нет", "ни", "но", "о", "об", "от", "по", "под",
    "при", "про", "с", "со", "то", "у", "уже", "что", "это", "этот", "эта",
    "эти", "я",
}


def _searchable_blob(row) -> str:
    parts = [
        row["title"] if "title" in row.keys() else None,
        row["content"] if "content" in row.keys() else None,
        row["context_text"] if "context_text" in row.keys() else None,
        row["tag"] if "tag" in row.keys() else None,
        row["manual_tag"] if "manual_tag" in row.keys() else None,
        row["transcript_text"] if "transcript_text" in row.keys() else None,
        row["file_name"] if "file_name" in row.keys() else None,
        row["page_title"] if "page_title" in row.keys() else None,
        row["link_url"] if "link_url" in row.keys() else None,
        row["link_domain"] if "link_domain" in row.keys() else None,
        row["sender_name"] if "sender_name" in row.keys() else None,
        row["sender_username"] if "sender_username" in row.keys() else None,
        row["source_chat_title"] if "source_chat_title" in row.keys() else None,
        row["mime_type"] if "mime_type" in row.keys() else None,
    ]
    return " ".join(str(part) for part in parts if part)


def _search_score(row, query_terms: list[str], normalized_query: str) -> int:
    blob = " ".join(_searchable_blob(row).casefold().split())
    words = set(_WORD_RE.findall(blob))
    score = 0
    for term in query_terms:
        if term in words:
            score += 3
        elif any(len(term) >= 4 and word.startswith(term) or len(word) >= 4 and term.startswith(word) for word in words):
            # Russian inflections often change a word ending (e.g. "файл" / "файлы").
            score += 2
        elif term in blob:
            score += 1
    if normalized_query and normalized_query in blob:
        score += 5
    return score


class SearchService:
    @staticmethod
    def search(
        user_id: int,
        raw_query: str,
        *,
        folder_id: int | None = None,
        kind: str | None = None,
        include_archived: bool = False,
        deep: bool = False,
        limit: int = 200,
        tags: list[str] | None = None,
        domain: str | None = None,
        sender: str | None = None,
        before: str | None = None,
        after: str | None = None,
        today: bool = False,
        untagged: bool = False,
    ):
        parsed = parse_search_request(raw_query, deep_default=deep)
        query = parsed.query
        kind = kind or parsed.kind
        folder_name = parsed.folder_name
        parsed_tags = [tag.lower().lstrip("#") for tag in (parsed.tags or []) if tag]
        tags = tags or parsed_tags
        domain = domain or parsed.domain
        sender = sender or parsed.sender
        before = before or parsed.before
        after = after or parsed.after
        today = today or parsed.today
        untagged = untagged or parsed.untagged

        if folder_id is None and folder_name:
            for folder in VaultService.list_folders(user_id, include_archived=True):
                if folder["name"].lower() == folder_name.lower():
                    folder_id = int(folder["id"])
                    break

        if not query and kind is None and folder_id is None and not tags and not domain and not sender and not before and not after and not today and not untagged:
            rows = VaultService.list_items(user_id, include_archived=include_archived)
        else:
            rows = VaultService.search_items(
                user_id=user_id,
                # SQLite LOWER()/LIKE are not Unicode case-insensitive. Fetch the
                # filtered candidate set and match text with Python casefold below
                # so Russian and other non-ASCII text searches work consistently.
                query="",
                folder_id=folder_id,
                kind=kind,
                include_archived=include_archived,
                deep=parsed.deep or deep or kind == "transcript" or bool(tags),
                tags=tags or None,
                domain=domain,
                sender=sender,
                before=before,
                after=after,
                today=today,
                untagged=untagged,
                limit=max(10000, limit),
            )

        if kind == "transcript":
            rows = [row for row in rows if row["kind"] in {"voice", "audio"}]

        if tags:
            def _tag_match(row) -> bool:
                row_tags = {part.strip().lower().lstrip("#") for part in (row["tag"] or "").split(",") if part.strip()}
                return all(tag in row_tags for tag in tags)
            rows = [row for row in rows if _tag_match(row)]

        if query:
            normalized_query = " ".join(query.casefold().split())
            query_terms = [
                term for term in dict.fromkeys(_WORD_RE.findall(normalized_query))
                if term not in _STOP_WORDS and len(term) > 1
            ]
            # Natural-language archive questions need recall: the model may
            # paraphrase the stored text, and Russian word endings can differ.
            # Rank partial matches above unrelated recent items instead of
            # requiring every query word to appear verbatim.
            scored = [
                (self_score, row)
                for row in rows
                if (self_score := _search_score(row, query_terms, normalized_query)) > 0
            ]
            scored.sort(key=lambda item: (item[0], int(item[1]["id"])), reverse=True)
            rows = [row for _, row in scored]
        else:
            rows = list(rows)

        return rows[:limit]
