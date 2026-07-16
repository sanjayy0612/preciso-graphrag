from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, final

from core.storage.base import BaseKVStorage
from core.storage.shared_storage import (
    clear_all_update_flags,
    get_data_init_lock,
    get_namespace_data,
    get_namespace_lock,
    get_update_flag,
    set_all_update_flags,
    try_initialize_namespace,
)
from core.utils import _cooperative_yield, load_json, logger, write_json


@final
@dataclass
class JsonKVStorage(BaseKVStorage):
    def __post_init__(self):
        working_dir = self.global_config["working_dir"]
        workspace_dir = os.path.join(working_dir, self.workspace) if self.workspace else working_dir
        self.workspace = self.workspace or ""
        os.makedirs(workspace_dir, exist_ok=True)
        self._file_name = os.path.join(workspace_dir, f"kv_store_{self.namespace}.json")
        self._data = None
        self._storage_lock = None
        self.storage_updated = None

    async def initialize(self):
        self._storage_lock = get_namespace_lock(self.namespace, workspace=self.workspace)
        self.storage_updated = await get_update_flag(self.namespace, workspace=self.workspace)
        async with get_data_init_lock():
            need_init = await try_initialize_namespace(self.namespace, workspace=self.workspace)
            self._data = await get_namespace_data(self.namespace, workspace=self.workspace)
            if need_init:
                loaded_data = load_json(self._file_name) or {}
                async with self._storage_lock:
                    self._data.update(loaded_data)

    async def index_done_callback(self) -> None:
        async with self._storage_lock:
            if self.storage_updated.value:
                write_json(dict(self._data), self._file_name)
                await clear_all_update_flags(self.namespace, workspace=self.workspace)

    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        async with self._storage_lock:
            result = self._data.get(id)
            if result:
                result = dict(result)
                result.setdefault("create_time", 0)
                result.setdefault("update_time", 0)
                result["_id"] = id
            return result

    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        async with self._storage_lock:
            results = []
            for id in ids:
                item = self._data.get(id)
                if item is None:
                    results.append(None)
                    continue
                copied = dict(item)
                copied.setdefault("create_time", 0)
                copied.setdefault("update_time", 0)
                copied["_id"] = id
                results.append(copied)
            return results

    async def filter_keys(self, keys: set[str]) -> set[str]:
        async with self._storage_lock:
            return set(keys) - set(self._data.keys())

    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        if not data:
            return
        current_time = int(time.time())
        async with self._storage_lock:
            for i, (k, v) in enumerate(data.items(), start=1):
                if k in self._data:
                    v["update_time"] = current_time
                else:
                    v["create_time"] = current_time
                    v["update_time"] = current_time
                v["_id"] = k
                await _cooperative_yield(i)
            self._data.update(data)
            await set_all_update_flags(self.namespace, workspace=self.workspace)

    async def delete(self, ids: list[str]) -> None:
        async with self._storage_lock:
            any_deleted = False
            for key in ids:
                if self._data.pop(key, None) is not None:
                    any_deleted = True
            if any_deleted:
                await set_all_update_flags(self.namespace, workspace=self.workspace)

    async def is_empty(self) -> bool:
        async with self._storage_lock:
            return len(self._data) == 0

    async def get_all_items(self) -> dict[str, dict[str, Any]]:
        async with self._storage_lock:
            return {k: dict(v) for k, v in self._data.items()}

    async def drop(self) -> dict[str, str]:
        try:
            async with self._storage_lock:
                self._data.clear()
                await set_all_update_flags(self.namespace, workspace=self.workspace)
            await self.index_done_callback()
            return {"status": "success", "message": "data dropped"}
        except Exception as exc:
            logger.error("[%s] Error dropping %s: %s", self.workspace, self.namespace, exc)
            return {"status": "error", "message": str(exc)}
