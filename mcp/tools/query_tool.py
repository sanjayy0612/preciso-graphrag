from __future__ import annotations

from core.query import kg_query
from core.storage.base import QueryParam
from core.utils import logger


async def query_graph(
    query: str, mode: str, storage_instances: dict, global_config: dict
) -> dict:
    try:
        query_param = QueryParam(mode=mode, include_references=True)
        result = await kg_query(
            query=query,
            knowledge_graph_inst=storage_instances["graph"],
            entities_vdb=storage_instances["entities_vdb"],
            relationships_vdb=storage_instances["relationships_vdb"],
            text_chunks_db=storage_instances["text_chunks"],
            query_param=query_param,
            global_config=global_config,
            hashing_kv=storage_instances.get("llm_cache"),
            chunks_vdb=storage_instances["chunks_vdb"],
        )
        if result is None:
            return {"status": "success", "message": "no results", "data": {}}
        return {
            "status": "success",
            "message": "query complete",
            "content": result.content,
            "raw_data": result.raw_data,
            "is_streaming": result.is_streaming,
        }
    except Exception as exc:
        logger.exception("query_graph failed (mode=%s)", mode)
        return {"status": "error", "message": str(exc)}
