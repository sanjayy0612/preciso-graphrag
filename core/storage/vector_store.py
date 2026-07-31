from __future__ import annotations

import asyncio
import base64
import os
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, final

import numpy as np
from nano_vectordb import NanoVectorDB

from core.storage.base import BaseVectorStorage
from core.storage.shared_storage import (
    get_namespace_lock,
    get_update_flag,
    set_all_update_flags,
)
from core.utils import compute_mdhash_id, logger


@final
@dataclass
class NanoVectorDBStorage(BaseVectorStorage):
    def __post_init__(self):
        self._validate_embedding_func()
        self._client = None
        self._storage_lock = None
        self.storage_updated = None
        kwargs = self.global_config.get("vector_db_storage_cls_kwargs", {})
        cosine_threshold = kwargs.get("cosine_better_than_threshold")
        if cosine_threshold is None:
            raise ValueError("cosine_better_than_threshold must be specified")
        self.cosine_better_than_threshold = cosine_threshold
        working_dir = self.global_config["working_dir"]
        self._working_dir = working_dir
        workspace_dir = os.path.join(working_dir, self.workspace) if self.workspace else working_dir
        self.workspace = self.workspace or ""
        os.makedirs(workspace_dir, exist_ok=True)
        self._client_file_name = os.path.join(workspace_dir, f"vdb_{self.namespace}.json")
        self._max_batch_size = self.global_config["embedding_batch_num"]
        try:
            self._client = NanoVectorDB(
                self.embedding_func.embedding_dim,
                storage_file=self._client_file_name,
            )
        except AssertionError as exc:
            backup_path = (
                f"{self._client_file_name}.dim-mismatch-{int(time.time())}.bak"
            )
            try:
                if os.path.exists(self._client_file_name):
                    Path(self._client_file_name).rename(backup_path)
            except OSError:
                backup_path = self._client_file_name
            warning = (
                f"Vector index for `{self.namespace}` had an embedding dimension mismatch and was reset. "
                f"Previous file saved to `{backup_path}`. Reingest data to rebuild vector search."
            )
            self.global_config.setdefault("runtime_warnings", []).append(warning)
            logger.warning("%s Original error: %s", warning, exc)
            self._client = NanoVectorDB(
                self.embedding_func.embedding_dim,
                storage_file=self._client_file_name,
            )

    async def initialize(self):
        self.storage_updated = await get_update_flag(
            self.namespace, workspace=self.workspace, working_dir=self._working_dir
        )
        self._storage_lock = get_namespace_lock(
            self.namespace, workspace=self.workspace, working_dir=self._working_dir
        )

    async def _get_client(self):
        async with self._storage_lock:
            if self.storage_updated.value:
                self._client = NanoVectorDB(
                    self.embedding_func.embedding_dim,
                    storage_file=self._client_file_name,
                )
                self.storage_updated.value = False
            return self._client

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            return
        current_time = int(time.time())
        list_data = [
            {
                "__id__": key,
                "__created_at__": current_time,
                **{meta_key: meta_val for meta_key, meta_val in value.items() if meta_key in self.meta_fields},
            }
            for key, value in data.items()
        ]
        contents = [value["content"] for value in data.values()]
        batches = [
            contents[i : i + self._max_batch_size]
            for i in range(0, len(contents), self._max_batch_size)
        ]
        embeddings_list = await asyncio.gather(
            *(self.embedding_func(batch, context="document") for batch in batches)
        )
        embeddings = np.concatenate(embeddings_list) if embeddings_list else np.array([])
        if len(embeddings) != len(list_data):
            raise ValueError("embedding count mismatch during upsert")
        for i, record in enumerate(list_data):
            vector_f16 = embeddings[i].astype(np.float16)
            compressed = zlib.compress(vector_f16.tobytes())
            encoded = base64.b64encode(compressed).decode("utf-8")
            record["vector"] = encoded
            record["__vector__"] = embeddings[i]
        client = await self._get_client()
        client.upsert(datas=list_data)

    async def query(
        self, query: str, top_k: int, query_embedding: list[float] | None = None
    ) -> list[dict[str, Any]]:
        if query_embedding is None:
            query_embedding = (await self.embedding_func([query], context="query", _priority=5))[0]
        client = await self._get_client()
        results = client.query(
            query=query_embedding,
            top_k=top_k,
            better_than_threshold=self.cosine_better_than_threshold,
        )
        return [
            {
                **{k: v for k, v in item.items() if k != "vector"},
                "id": item["__id__"],
                "distance": item["__metrics__"],
                "created_at": item.get("__created_at__"),
            }
            for item in results
        ]

    async def delete(self, ids: list[str]):
        client = await self._get_client()
        client.delete(ids)

    async def delete_entity(self, entity_name: str) -> None:
        entity_id = compute_mdhash_id(entity_name, prefix="ent-")
        client = await self._get_client()
        if client.get([entity_id]):
            client.delete([entity_id])

    async def delete_entity_relation(self, entity_name: str) -> None:
        client = await self._get_client()
        storage = getattr(client, "_NanoVectorDB__storage")
        relations = [
            item
            for item in storage["data"]
            if item.get("src_id") == entity_name or item.get("tgt_id") == entity_name
        ]
        if relations:
            client.delete([relation["__id__"] for relation in relations])

    async def index_done_callback(self) -> bool:
        async with self._storage_lock:
            try:
                self._client.save()
                await set_all_update_flags(
                    self.namespace, workspace=self.workspace, working_dir=self._working_dir
                )
                self.storage_updated.value = False
                return True
            except Exception as exc:
                logger.error("[%s] Error saving data for %s: %s", self.workspace, self.namespace, exc)
                return False

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        client = await self._get_client()
        result = client.get([id])
        if not result:
            return None
        item = result[0]
        return {
            **{k: v for k, v in item.items() if k != "vector"},
            "id": item.get("__id__"),
            "created_at": item.get("__created_at__"),
        }

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        if not ids:
            return []
        client = await self._get_client()
        results = client.get(ids)
        result_map = {}
        for item in results:
            if not item:
                continue
            result_map[str(item.get("__id__"))] = {
                **{k: v for k, v in item.items() if k != "vector"},
                "id": item.get("__id__"),
                "created_at": item.get("__created_at__"),
            }
        return [result_map.get(str(requested_id)) for requested_id in ids]

    async def get_vectors_by_ids(self, ids: list[str]) -> dict[str, list[float]]:
        if not ids:
            return {}
        client = await self._get_client()
        results = client.get(ids)
        vectors: dict[str, list[float]] = {}
        for item in results:
            if not item:
                continue
            vector = item.get("__vector__")
            if vector is None and "vector" in item:
                compressed = base64.b64decode(item["vector"])
                vector = np.frombuffer(zlib.decompress(compressed), dtype=np.float16).astype(np.float32)
            if vector is not None:
                vectors[str(item["__id__"])] = vector.tolist()
        return vectors

    async def drop(self) -> dict[str, str]:
        try:
            client = await self._get_client()
            storage = getattr(client, "_NanoVectorDB__storage")
            storage["data"] = []
            await self.index_done_callback()
            return {"status": "success", "message": "data dropped"}
        except Exception as exc:
            logger.exception("NanoVectorDBStorage.drop failed (namespace=%s)", self.namespace)
            return {"status": "error", "message": str(exc)}
