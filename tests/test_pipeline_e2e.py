"""ingest/pipeline.py::ingest_extracted_json end-to-end against a real storage
stack in a temporary working dir, using the 8-dim fallback embedder (offline)."""

from __future__ import annotations

import copy
import logging

from config import GRAPH_FIELD_SEP
from core.query import _find_related_text_unit_from_entities
from core.storage.base import QueryParam
from core.utils import logger as graphrag_logger
from ingest.pipeline import ingest_extracted_json


def make_payload() -> dict:
    return {
        "document_id": "doc_e2e",
        "file_path": "doc_e2e.md",
        "timestamp": 1_700_000_000,
        "chunks": [
            {"chunk_id": "chunk-1", "content": "Apple reported record revenue in 2023."},
            {"chunk_id": "chunk-2", "content": "Tim Cook has been CEO of Apple since 2011."},
        ],
        "entities": [
            {
                "entity_name": "APPLE",
                "entity_type": "ORG",
                "description": "Apple is a technology company.",
                "source_id": "chunk-1",
            },
            {
                "entity_name": "TIM_COOK",
                "entity_type": "PERSON",
                "description": "Tim Cook is Apple's CEO.",
                "source_id": "chunk-2",
            },
        ],
        "relationships": [
            {
                "src_id": "TIM_COOK",
                "tgt_id": "APPLE",
                "description": "Tim Cook leads Apple.",
                "keywords": "leadership",
                "weight": 1.0,
                "source_id": "chunk-2",
            }
        ],
    }


async def test_ingest_success_and_artifacts_written(storage_stack):
    storage_instances, global_config, working_dir = storage_stack
    result = await ingest_extracted_json(make_payload(), storage_instances, global_config)
    assert result["status"] == "success", result
    assert result["errors"] == []
    assert result["chunks_ingested"] == 2
    assert result["entities_merged"] == 2
    assert result["relationships_merged"] == 1

    for artifact in (
        "graph_graph.graphml",
        "kv_store_text_chunks.json",
        "vdb_entities.json",
        "vdb_relationships.json",
        "vdb_chunks.json",
        "artifact_manifest.json",
    ):
        assert (working_dir / artifact).exists(), f"missing artifact {artifact}"

    node = await storage_instances["graph"].get_node("APPLE")
    assert node is not None
    assert node["description"] == "Apple is a technology company."
    assert "chunk-1" in node["source_id"]
    edge = await storage_instances["graph"].get_edge("TIM_COOK", "APPLE")
    assert edge is not None and "chunk-2" in edge["source_id"]


async def test_reingest_same_payload_is_idempotent(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    first = await ingest_extracted_json(payload, storage_instances, global_config)
    node_before = dict(await storage_instances["graph"].get_node("APPLE"))
    second = await ingest_extracted_json(copy.deepcopy(payload), storage_instances, global_config)
    node_after = dict(await storage_instances["graph"].get_node("APPLE"))
    assert first["status"] == second["status"] == "success"
    assert node_after["description"] == node_before["description"]
    assert node_after["source_id"] == node_before["source_id"]


async def test_invalid_entity_reported_as_partial_success(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    payload["entities"].append({"entity_name": "NO_DESCRIPTION", "entity_type": "ORG", "source_id": "chunk-1"})
    result = await ingest_extracted_json(payload, storage_instances, global_config)
    assert result["status"] == "partial_success"
    assert any("NO_DESCRIPTION" in err for err in result["errors"])
    assert await storage_instances["graph"].get_node("NO_DESCRIPTION") is None
    # valid entities still ingested
    assert await storage_instances["graph"].get_node("APPLE") is not None


async def test_relationship_to_unknown_entity_is_rejected(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    payload["relationships"].append(
        {
            "src_id": "APPLE",
            "tgt_id": "GHOST_CORP",
            "description": "Phantom relation.",
            "source_id": "chunk-1",
        }
    )
    result = await ingest_extracted_json(payload, storage_instances, global_config)
    assert result["status"] == "partial_success"
    assert any("unknown entities" in err for err in result["errors"])
    assert result["relationships_merged"] == 1  # only the valid one
    assert await storage_instances["graph"].get_edge("APPLE", "GHOST_CORP") is None


async def test_non_dict_payload_errors(storage_stack):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(["not", "a", "dict"], storage_instances, global_config)
    assert result["status"] == "error"


async def test_entity_descriptions_accumulate_sep_joined(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    await ingest_extracted_json(payload, storage_instances, global_config)
    second = copy.deepcopy(payload)
    second["entities"][0]["description"] = "Apple designs the iPhone."
    await ingest_extracted_json(second, storage_instances, global_config)
    node = await storage_instances["graph"].get_node("APPLE")
    segments = node["description"].split(GRAPH_FIELD_SEP)
    assert segments == ["Apple is a technology company.", "Apple designs the iPhone."]


async def test_source_ids_expand_split_chunks_and_report_danglers(storage_stack, caplog):
    storage_instances, global_config, _ = storage_stack
    payload = {
        "document_id": "doc_source_ids",
        "chunks": [
            {"chunk_id": "chunk-1", "content": "Apple reported record revenue. " * 60},
            {"chunk_id": "chunk-2", "content": "Apple has an evidence chunk."},
        ],
        "entities": [
            {
                "entity_name": "APPLE",
                "entity_type": "ORG",
                "description": "Apple is a technology company.",
                "source_id": "chunk-1",
            },
            {
                "entity_name": "GHOST",
                "entity_type": "ORG",
                "description": "Ghost has no evidence.",
                "source_id": "chunk-99",
            },
            {
                "entity_name": "TIM_COOK",
                "entity_type": "PERSON",
                "description": "Tim Cook leads Apple.",
                "source_id": "chunk-2",
            },
        ],
        "relationships": [
            {
                "src_id": "TIM_COOK",
                "tgt_id": "APPLE",
                "description": "Tim Cook leads Apple.",
                "source_id": "chunk-1",
            }
        ],
    }

    result = await ingest_extracted_json(payload, storage_instances, global_config)

    assert result["status"] == "success"
    assert any("chunk-99" in warning for warning in result["warnings"])
    apple = await storage_instances["graph"].get_node("APPLE")
    apple_chunk_ids = apple["source_id"].split(GRAPH_FIELD_SEP)
    assert len(apple_chunk_ids) == 3
    assert all(chunk_id.endswith(("-p1", "-p2", "-p3")) for chunk_id in apple_chunk_ids)
    assert all(
        chunk is not None
        for chunk in await storage_instances["text_chunks"].get_by_ids(apple_chunk_ids)
    )
    apple_evidence = await _find_related_text_unit_from_entities(
        [{"entity_name": "APPLE", **apple}],
        QueryParam(),
        storage_instances["text_chunks"],
        storage_instances["graph"],
    )
    assert apple_evidence and all(chunk["content"] for chunk in apple_evidence)
    relation = await storage_instances["graph"].get_edge("TIM_COOK", "APPLE")
    assert relation["source_id"] == apple["source_id"]

    ghost = await storage_instances["graph"].get_node("GHOST")
    graphrag_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            evidence = await _find_related_text_unit_from_entities(
                [{"entity_name": "GHOST", **ghost}],
                QueryParam(),
                storage_instances["text_chunks"],
                storage_instances["graph"],
            )
    finally:
        graphrag_logger.removeHandler(caplog.handler)
    assert evidence == []
    assert "Unresolvable entity evidence chunk" in caplog.text


async def test_strict_source_ids_reject_dangling_citations(storage_stack, monkeypatch):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setenv("GRAPHRAG_STRICT_SOURCE_IDS", "true")
    payload = {
        "document_id": "doc_strict_source_ids",
        "chunks": [{"chunk_id": "chunk-1", "content": "Evidence."}],
        "entities": [
            {
                "entity_name": "GHOST",
                "entity_type": "ORG",
                "description": "Ghost has no evidence.",
                "source_id": "chunk-99",
            }
        ],
        "relationships": [],
    }

    result = await ingest_extracted_json(payload, storage_instances, global_config)

    assert result["status"] == "partial_success"
    assert any("unresolvable source_id" in error for error in result["errors"])
    assert await storage_instances["graph"].get_node("GHOST") is None


async def test_source_id_can_reference_a_chunk_from_an_earlier_ingest(storage_stack, monkeypatch):
    storage_instances, global_config, _ = storage_stack
    await ingest_extracted_json(
        {
            "document_id": "doc_prior_source_ids",
            "chunks": [{"chunk_id": "chunk-1", "content": "Earlier evidence."}],
            "entities": [
                {
                    "entity_name": "FIRST",
                    "entity_type": "ORG",
                    "description": "First entity.",
                    "source_id": "chunk-1",
                }
            ],
            "relationships": [],
        },
        storage_instances,
        global_config,
    )

    monkeypatch.setenv("GRAPHRAG_STRICT_SOURCE_IDS", "true")
    result = await ingest_extracted_json(
        {
            "document_id": "doc_prior_source_ids",
            "chunks": [],
            "entities": [
                {
                    "entity_name": "SECOND",
                    "entity_type": "ORG",
                    "description": "Second entity.",
                    "source_id": "chunk-1",
                }
            ],
            "relationships": [],
        },
        storage_instances,
        global_config,
    )

    assert result["status"] == "success"
    assert result["warnings"] == []
