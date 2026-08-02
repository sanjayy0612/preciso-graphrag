from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

_namespace_data: dict[str, dict[str, Any]] = {}
_namespace_locks: dict[str, asyncio.Lock] = {}
_namespace_update_flags: dict[str, "UpdateFlag"] = {}
_namespace_init_flags: dict[str, bool] = {}
_keyed_locks: dict[str, asyncio.Lock] = {}
_data_init_lock = asyncio.Lock()


@dataclass
class UpdateFlag:
    value: bool = False
    # ``value`` remains for JsonKVStorage's write-dirty semantics.  A revision
    # is needed by storage instances that each keep their own in-memory copy:
    # a boolean notification can be consumed by only one sibling reader.
    revision: int = 0


def initialize_share_data() -> None:
    return None


def get_final_namespace(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> str:
    working_dir = working_dir or ""
    workspace = workspace or ""
    return f"{working_dir}:{workspace}:{namespace}"


def get_namespace_lock(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> asyncio.Lock:
    final_namespace = get_final_namespace(namespace, workspace, working_dir)
    if final_namespace not in _namespace_locks:
        _namespace_locks[final_namespace] = asyncio.Lock()
    return _namespace_locks[final_namespace]


async def get_update_flag(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> UpdateFlag:
    final_namespace = get_final_namespace(namespace, workspace, working_dir)
    if final_namespace not in _namespace_update_flags:
        _namespace_update_flags[final_namespace] = UpdateFlag()
    return _namespace_update_flags[final_namespace]


async def set_all_update_flags(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> None:
    flag = await get_update_flag(namespace, workspace, working_dir)
    flag.value = True
    flag.revision += 1


async def clear_all_update_flags(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> None:
    flag = await get_update_flag(namespace, workspace, working_dir)
    flag.value = False


async def try_initialize_namespace(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> bool:
    final_namespace = get_final_namespace(namespace, workspace, working_dir)
    if final_namespace not in _namespace_init_flags:
        _namespace_init_flags[final_namespace] = True
        return True
    return False


async def get_namespace_data(
    namespace: str,
    workspace: str | None = None,
    working_dir: str | None = None,
) -> dict[str, Any]:
    final_namespace = get_final_namespace(namespace, workspace, working_dir)
    if final_namespace not in _namespace_data:
        _namespace_data[final_namespace] = {}
    return _namespace_data[final_namespace]


def get_data_init_lock() -> asyncio.Lock:
    return _data_init_lock


def get_storage_keyed_lock(key: str) -> asyncio.Lock:
    if key not in _keyed_locks:
        _keyed_locks[key] = asyncio.Lock()
    return _keyed_locks[key]
