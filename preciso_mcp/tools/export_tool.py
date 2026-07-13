from __future__ import annotations

from core.export_adapters import export_graph_to_neo4j, export_vectors_to_qdrant


async def export_to_neo4j(
    storage_instances: dict,
    *,
    uri: str | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict:
    return await export_graph_to_neo4j(
        storage_instances,
        uri=uri,
        username=username,
        password=password,
        database=database,
        workspace=workspace,
        clear_existing=clear_existing,
    )


async def export_to_qdrant(
    storage_instances: dict,
    global_config: dict,
    *,
    url: str | None = None,
    api_key: str | None = None,
    collection_prefix: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict:
    return await export_vectors_to_qdrant(
        storage_instances,
        global_config,
        url=url,
        api_key=api_key,
        collection_prefix=collection_prefix,
        workspace=workspace,
        clear_existing=clear_existing,
    )
