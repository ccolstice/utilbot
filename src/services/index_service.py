from __future__ import annotations

from src.services.search_service import SearchService
from src.services.vault_service import VaultService


class IndexService:
    @staticmethod
    def search(
        user_id: int,
        query: str,
        *,
        folder_id: int | None = None,
        kind: str | None = None,
        deep: bool = False,
        include_archived: bool = False,
        tags: list[str] | None = None,
        domain: str | None = None,
        sender: str | None = None,
        before: str | None = None,
        after: str | None = None,
        today: bool = False,
        untagged: bool = False,
        limit: int = 200,
    ):
        return SearchService.search(
            user_id,
            query,
            folder_id=folder_id,
            kind=kind,
            include_archived=include_archived,
            deep=deep,
            tags=tags,
            domain=domain,
            sender=sender,
            before=before,
            after=after,
            today=today,
            untagged=untagged,
            limit=limit,
        )

    @staticmethod
    def count(
        user_id: int,
        query: str,
        *,
        folder_id: int | None = None,
        kind: str | None = None,
        deep: bool = False,
        include_archived: bool = False,
    ) -> int:
        return VaultService.search_count(
            user_id=user_id,
            query=query,
            folder_id=folder_id,
            kind=kind,
            include_archived=include_archived,
            deep=deep,
        )
