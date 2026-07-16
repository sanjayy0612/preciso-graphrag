from __future__ import annotations

from core.storage.graph_store import NetworkXStorage
from core.storage.kv_store import JsonKVStorage
from core.storage.vector_store import NanoVectorDBStorage


def build_storage_instances(global_config: dict, workspace: str = "") -> dict:
    embedding_func = global_config["embedding_func"]
    shared_kwargs = {"workspace": workspace, "global_config": global_config}
    return {
        "graph": NetworkXStorage(
            namespace="graph",
            embedding_func=None,
            **shared_kwargs,
        ),
        "text_chunks": JsonKVStorage(
            namespace="text_chunks",
            embedding_func=None,
            **shared_kwargs,
        ),
        "entity_chunks": JsonKVStorage(
            namespace="entity_chunks",
            embedding_func=None,
            **shared_kwargs,
        ),
        "relation_chunks": JsonKVStorage(
            namespace="relation_chunks",
            embedding_func=None,
            **shared_kwargs,
        ),
        "pending_summaries": JsonKVStorage(
            namespace="pending_summaries",
            embedding_func=None,
            **shared_kwargs,
        ),
        "llm_cache": JsonKVStorage(
            namespace="llm_cache",
            embedding_func=None,
            **shared_kwargs,
        ),
        "checkpoints": JsonKVStorage(
            namespace="checkpoints",
            embedding_func=None,
            **shared_kwargs,
        ),
        "entities_vdb": NanoVectorDBStorage(
            namespace="entities",
            embedding_func=embedding_func,
            meta_fields={"entity_name", "source_id", "content", "file_path", "entity_type"},
            **shared_kwargs,
        ),
        "relationships_vdb": NanoVectorDBStorage(
            namespace="relationships",
            embedding_func=embedding_func,
            meta_fields={
                "src_id",
                "tgt_id",
                "source_id",
                "content",
                "file_path",
                "keywords",
                "description",
                "weight",
            },
            **shared_kwargs,
        ),
        "chunks_vdb": NanoVectorDBStorage(
            namespace="chunks",
            embedding_func=embedding_func,
            meta_fields={"full_doc_id", "content", "file_path", "chunk_id"},
            **shared_kwargs,
        ),
    }


async def initialize_storage_instances(storage_instances: dict) -> None:
    for storage in storage_instances.values():
        await storage.initialize()
