from __future__ import annotations

from core.runtime_status import build_runtime_status, update_artifact_manifest


async def get_server_status(storage_instances: dict, global_config: dict) -> dict:
    await update_artifact_manifest(storage_instances, global_config)
    return await build_runtime_status(storage_instances, global_config)
