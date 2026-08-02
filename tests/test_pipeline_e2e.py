"""ingest/pipeline.py::ingest_extracted_json end-to-end against a real storage
stack in a temporary working dir, using the 8-dim fallback embedder (offline)."""

from __future__ import annotations

import copy
import logging

from config import GRAPH_FIELD_SEP
from core.query import _find_related_text_unit_from_entities, _find_related_text_unit_from_relations
from core.storage.base import QueryParam
from core.utils import compute_mdhash_id, logger as graphrag_logger
from ingest.pipeline import ingest_extracted_json


class RecordingVectorStore:
    """Minimal vector adapter that records write batch sizes."""

    def __init__(self):
        self.upsert_calls: list[dict[str, dict]] = []
        self.delete_calls: list[list[str]] = []

    async def upsert(self, payload):
        self.upsert_calls.append(copy.deepcopy(payload))

    async def delete(self, ids):
        self.delete_calls.append(list(ids))

    async def index_done_callback(self):
        pass


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


async def test_ingest_batches_entity_and_relationship_vector_writes(storage_stack):
    storage_instances, global_config, _ = storage_stack
    entity_vectors = RecordingVectorStore()
    relationship_vectors = RecordingVectorStore()
    storage_instances["entities_vdb"] = entity_vectors
    storage_instances["relationships_vdb"] = relationship_vectors
    payload = {
        "document_id": "doc_vector_batching",
        "file_path": "doc_vector_batching.md",
        "chunks": [
            {"chunk_id": f"chunk-{index}", "content": f"Evidence {index}."}
            for index in range(1, 4)
        ],
        "entities": [
            {
                "entity_name": name,
                "entity_type": "ORG",
                "description": f"{name} description.",
                "source_id": "chunk-1",
            }
            for name in ("ALPHA", "BETA", "GAMMA", "DELTA")
        ],
        "relationships": [
            {
                "src_id": src,
                "tgt_id": tgt,
                "description": f"{src} relates to {tgt}.",
                "source_id": source_id,
            }
            for src, tgt, source_id in (
                ("ALPHA", "BETA", "chunk-1"),
                ("BETA", "GAMMA", "chunk-2"),
                ("GAMMA", "DELTA", "chunk-3"),
            )
        ],
    }

    result = await ingest_extracted_json(payload, storage_instances, global_config)

    assert result["status"] == "success", result
    assert [len(call) for call in entity_vectors.upsert_calls] == [4]
    assert [len(call) for call in relationship_vectors.upsert_calls] == [3]
    assert [len(call) for call in relationship_vectors.delete_calls] == [6]


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
    chunk_vector_ids = [
        compute_mdhash_id(chunk_id, prefix="vchunk-")
        for chunk_id in ("doc_e2e::chunk-1", "doc_e2e::chunk-2")
    ]
    stored_vectors = await storage_instances["chunks_vdb"].get_vectors_by_ids(chunk_vector_ids)
    assert set(stored_vectors) == set(chunk_vector_ids)


async def test_identical_recovery_replay_is_idempotent(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    first = await ingest_extracted_json(payload, storage_instances, global_config)
    node_before = dict(await storage_instances["graph"].get_node("APPLE"))
    edge_before = dict(await storage_instances["graph"].get_edge("TIM_COOK", "APPLE"))
    second = await ingest_extracted_json(copy.deepcopy(payload), storage_instances, global_config)
    node_after = dict(await storage_instances["graph"].get_node("APPLE"))
    edge_after = dict(await storage_instances["graph"].get_edge("TIM_COOK", "APPLE"))
    assert first["status"] == second["status"] == "success"
    assert node_after["description"] == node_before["description"]
    assert node_after["source_id"] == node_before["source_id"]
    assert edge_after["weight"] == edge_before["weight"]
    assert edge_after["source_id"] == edge_before["source_id"]


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


async def test_new_document_incrementally_adds_entity_evidence(storage_stack):
    storage_instances, global_config, _ = storage_stack
    payload = make_payload()
    await ingest_extracted_json(payload, storage_instances, global_config)
    followup = {
        "document_id": "doc_e2e_followup",
        "file_path": "doc_e2e_followup.md",
        "timestamp": 1_700_000_100,
        "chunks": [{"chunk_id": "chunk-1", "content": "Apple designs the iPhone."}],
        "entities": [
            {
                "entity_name": "APPLE",
                "entity_type": "ORG",
                "description": "Apple designs the iPhone.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
    }
    await ingest_extracted_json(followup, storage_instances, global_config)

    node = await storage_instances["graph"].get_node("APPLE")
    segments = node["description"].split(GRAPH_FIELD_SEP)
    assert segments == ["Apple is a technology company.", "Apple designs the iPhone."]
    assert node["source_id"].split(GRAPH_FIELD_SEP) == [
        "doc_e2e::chunk-1",
        "doc_e2e::chunk-2",
        "doc_e2e_followup::chunk-1",
    ]


async def test_changed_same_document_payload_is_additive_not_replacement(storage_stack):
    storage_instances, global_config, _ = storage_stack
    initial = {
        "document_id": "doc_changed",
        "file_path": "doc_changed.md",
        "chunks": [{"chunk_id": "chunk-1", "content": "Old ACME evidence. " * 100}],
        "entities": [
            {
                "entity_name": "ACME",
                "entity_type": "ORG",
                "description": "Old ACME description.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
    }
    changed = {
        "document_id": "doc_changed",
        "file_path": "doc_changed.md",
        "chunks": [{"chunk_id": "chunk-1", "content": "Corrected ACME evidence."}],
        "entities": [
            {
                "entity_name": "ACME",
                "entity_type": "ORG",
                "description": "Corrected ACME description.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
    }

    await ingest_extracted_json(initial, storage_instances, global_config)
    await ingest_extracted_json(changed, storage_instances, global_config)

    node = await storage_instances["graph"].get_node("ACME")
    source_ids = node["source_id"].split(GRAPH_FIELD_SEP)
    assert "doc_changed::chunk-1" in source_ids
    assert any(chunk_id.startswith("doc_changed::chunk-1-p") for chunk_id in source_ids)
    descriptions = node["description"].split(GRAPH_FIELD_SEP)
    assert descriptions == ["Old ACME description.", "Corrected ACME description."]


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


async def test_source_id_expands_mixed_split_and_unsplit_citations(storage_stack):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        {
            "document_id": "doc_mixed_source_ids",
            "chunks": [
                {"chunk_id": "long", "content": "Long evidence. " * 80},
                {"chunk_id": "short", "content": "Short evidence."},
            ],
            "entities": [
                {
                    "entity_name": "MIXED",
                    "entity_type": "ORG",
                    "description": "An entity supported by both chunks.",
                    "source_id": f"long{GRAPH_FIELD_SEP}short",
                }
            ],
            "relationships": [],
        },
        storage_instances,
        global_config,
    )

    assert result["warnings"] == []
    node = await storage_instances["graph"].get_node("MIXED")
    source_ids = node["source_id"].split(GRAPH_FIELD_SEP)
    assert source_ids[-1] == "doc_mixed_source_ids::short"
    assert len(source_ids) == result["chunks_ingested"]
    assert all(
        chunk is not None
        for chunk in await storage_instances["text_chunks"].get_by_ids(source_ids)
    )


async def test_strict_source_ids_rejects_dangling_relationship_only(storage_stack, monkeypatch):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setenv("GRAPHRAG_STRICT_SOURCE_IDS", " TRUE ")
    result = await ingest_extracted_json(
        {
            "document_id": "doc_strict_relationship",
            "chunks": [{"chunk_id": "chunk-1", "content": "Entity evidence."}],
            "entities": [
                {
                    "entity_name": "ALPHA",
                    "entity_type": "ORG",
                    "description": "Alpha.",
                    "source_id": "chunk-1",
                },
                {
                    "entity_name": "BETA",
                    "entity_type": "ORG",
                    "description": "Beta.",
                    "source_id": "chunk-1",
                },
            ],
            "relationships": [
                {
                    "src_id": "ALPHA",
                    "tgt_id": "BETA",
                    "description": "Unsupported relationship.",
                    "source_id": "chunk-99",
                }
            ],
        },
        storage_instances,
        global_config,
    )

    assert result["status"] == "partial_success"
    assert result["entities_merged"] == 2
    assert result["relationships_merged"] == 0
    assert any("relationship `ALPHA->BETA` has unresolvable source_id" in error for error in result["errors"])
    assert await storage_instances["graph"].get_edge("ALPHA", "BETA") is None


async def test_query_logs_and_excludes_dangling_relationship_evidence(storage_stack, caplog):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        {
            "document_id": "doc_query_dangling_relationship",
            "chunks": [{"chunk_id": "chunk-1", "content": "Entity evidence."}],
            "entities": [
                {
                    "entity_name": "ALPHA",
                    "entity_type": "ORG",
                    "description": "Alpha.",
                    "source_id": "chunk-1",
                },
                {
                    "entity_name": "BETA",
                    "entity_type": "ORG",
                    "description": "Beta.",
                    "source_id": "chunk-1",
                },
            ],
            "relationships": [
                {
                    "src_id": "ALPHA",
                    "tgt_id": "BETA",
                    "description": "Unsupported relationship.",
                    "source_id": "chunk-99",
                }
            ],
        },
        storage_instances,
        global_config,
    )
    assert result["status"] == "success"
    assert result["warnings"]

    edge = await storage_instances["graph"].get_edge("ALPHA", "BETA")
    graphrag_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            evidence = await _find_related_text_unit_from_relations(
                [{"src_tgt": ("ALPHA", "BETA"), **edge}],
                QueryParam(),
                storage_instances["text_chunks"],
            )
    finally:
        graphrag_logger.removeHandler(caplog.handler)

    assert evidence == []
    assert "Unresolvable relationship evidence chunk" in caplog.text
