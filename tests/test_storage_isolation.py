"""Storage globals must remain isolated between independent working directories."""

from __future__ import annotations

import asyncio

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json


async def _storage_stack(working_dir: str) -> dict:
    global_config = build_global_config(
        working_dir=working_dir,
        tokenizer=BasicTokenizer(),
        embedding_func=EmbeddingFunc(
            embedding_dim=8,
            max_token_size=8192,
            func=_fallback_embed,
            model_name="fallback",
        ),
    )
    storage_instances = build_storage_instances(global_config)
    await initialize_storage_instances(storage_instances)
    return storage_instances


async def test_json_kv_storage_isolated_by_working_dir(tmp_path):
    left = await _storage_stack(str(tmp_path / "left"))
    await left["text_chunks"].upsert({"same-key": {"content": "left-only"}})

    right = await _storage_stack(str(tmp_path / "right"))

    assert await right["text_chunks"].get_by_id("same-key") is None


async def test_graph_updates_are_seen_by_every_sibling_reader(tmp_path):
    """A refresh notification must fan out, not be consumed by one reader."""
    writer = await _storage_stack(str(tmp_path))
    first_reader = await _storage_stack(str(tmp_path))
    second_reader = await _storage_stack(str(tmp_path))

    await writer["graph"].upsert_node("shared-node", {"kind": "test"})
    await writer["graph"].index_done_callback()

    assert await first_reader["graph"].has_node("shared-node")
    assert await second_reader["graph"].has_node("shared-node")


async def test_vector_updates_are_seen_by_every_sibling_reader(tmp_path):
    writer = await _storage_stack(str(tmp_path))
    first_reader = await _storage_stack(str(tmp_path))
    second_reader = await _storage_stack(str(tmp_path))
    record_id = "entity-shared-node"

    await writer["entities_vdb"].upsert(
        {
            record_id: {
                "content": "shared node",
                "entity_name": "SHARED NODE",
                "source_id": "chunk-1",
                "entity_type": "TEST",
                "file_path": "test.json",
            }
        }
    )
    await writer["entities_vdb"].index_done_callback()

    assert await first_reader["entities_vdb"].get_by_id(record_id) is not None
    assert await second_reader["entities_vdb"].get_by_id(record_id) is not None


async def test_parallel_ingestions_preserve_both_documents(tmp_path):
    """Two sessions must not overwrite each other's graph snapshot on save."""
    left = await _storage_stack(str(tmp_path))
    right = await _storage_stack(str(tmp_path))

    def payload(document_id: str, entity_name: str) -> dict:
        return {
            "document_id": document_id,
            "chunks": [{"chunk_id": "chunk-1", "content": f"Facts about {entity_name}."}],
            "entities": [
                {
                    "entity_name": entity_name,
                    "entity_type": "TEST",
                    "description": f"{entity_name} is a test entity.",
                    "source_id": "chunk-1",
                }
            ],
            "relationships": [],
        }

    results = await asyncio.gather(
        ingest_extracted_json(payload("left-doc", "LEFT ENTITY"), left, left["graph"].global_config),
        ingest_extracted_json(payload("right-doc", "RIGHT ENTITY"), right, right["graph"].global_config),
    )
    assert [result["status"] for result in results] == ["success", "success"]

    verifier = await _storage_stack(str(tmp_path))
    assert await verifier["graph"].has_node("LEFT ENTITY")
    assert await verifier["graph"].has_node("RIGHT ENTITY")
