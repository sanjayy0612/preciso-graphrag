from __future__ import annotations

from typing import Any

from core.storage.base import BaseVectorStorage
from core.utils import logger


class VectorWriteBatch:
    """Coalesce vector mutations and apply the final state in one write.

    Merge logic can update the same entity several times while processing a
    document's relationships.  Embedding every intermediate value is wasted
    work: only the last value can be present when the ingestion commits.  This
    adapter preserves that final-state behavior while allowing the underlying
    vector store to embed the records as a batch.
    """

    def __init__(self, storage: BaseVectorStorage):
        self._storage = storage
        self._upserts: dict[str, dict[str, Any]] = {}
        self._deletes: set[str] = set()

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        for record_id, record in data.items():
            self._deletes.discard(record_id)
            self._upserts[record_id] = dict(record)

    async def delete(self, ids: list[str]) -> None:
        for record_id in ids:
            self._upserts.pop(record_id, None)
            self._deletes.add(record_id)

    async def flush(self) -> None:
        if self._deletes:
            try:
                await self._storage.delete(sorted(self._deletes))
            except Exception as exc:
                # Relationship merge treats removal of legacy vector IDs as
                # best-effort, so batching must retain the same behavior.
                logger.warning("Failed to delete stale vector IDs: %s", exc)
        if self._upserts:
            await self._storage.upsert(dict(self._upserts))
        self._deletes.clear()
        self._upserts.clear()
