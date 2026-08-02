from __future__ import annotations

import pytest

from core.query import EvidenceVectorIntegrityError, kg_query, select_evidence_chunks_by_vector
from core.storage.base import QueryParam
from core.utils import compute_mdhash_id
from ingest.pipeline import ingest_extracted_json


class StubChunkVectors:
    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors

    async def get_vectors_by_ids(self, ids: list[str]) -> dict[str, list[float]]:
        return {vector_id: self.vectors[vector_id] for vector_id in ids if vector_id in self.vectors}


def vector_id(chunk_id: str) -> str:
    return compute_mdhash_id(chunk_id, prefix="vchunk-")


@pytest.mark.asyncio
async def test_evidence_selection_applies_one_global_top_k_across_sources():
    candidates = [
        {"chunk_id": "entity-best", "content": "entity evidence", "source_type": "entity"},
        {"chunk_id": "relation-low", "content": "relation evidence", "source_type": "relationship"},
        {"chunk_id": "vector-second", "content": "direct evidence", "source_type": "vector"},
    ]
    chunks_vdb = StubChunkVectors(
        {
            vector_id("entity-best"): [1.0, 0.0],
            vector_id("relation-low"): [0.0, 1.0],
            vector_id("vector-second"): [0.8, 0.2],
        }
    )

    selected = await select_evidence_chunks_by_vector(
        query="products",
        candidates=candidates,
        chunks_vdb=chunks_vdb,
        top_k=2,
        min_similarity=0.1,
        query_embedding=[1.0, 0.0],
    )

    assert [chunk["chunk_id"] for chunk in selected] == ["entity-best", "vector-second"]
    assert [chunk["source_type"] for chunk in selected] == ["entity", "vector"]


@pytest.mark.asyncio
async def test_evidence_selection_does_not_fill_top_k_with_weak_matches():
    candidates = [
        {"chunk_id": "strong", "content": "strong evidence"},
        {"chunk_id": "weak", "content": "weak evidence"},
    ]
    chunks_vdb = StubChunkVectors(
        {
            vector_id("strong"): [1.0, 0.0],
            vector_id("weak"): [0.0, 1.0],
        }
    )

    selected = await select_evidence_chunks_by_vector(
        query="products",
        candidates=candidates,
        chunks_vdb=chunks_vdb,
        top_k=2,
        min_similarity=0.5,
        query_embedding=[1.0, 0.0],
    )

    assert [chunk["chunk_id"] for chunk in selected] == ["strong"]


@pytest.mark.asyncio
async def test_evidence_selection_fails_closed_when_any_vector_is_missing():
    candidates = [
        {"chunk_id": "present", "content": "stored evidence"},
        {"chunk_id": "missing", "content": "unindexed evidence"},
    ]
    chunks_vdb = StubChunkVectors({vector_id("present"): [1.0, 0.0]})

    with pytest.raises(EvidenceVectorIntegrityError, match="missing"):
        await select_evidence_chunks_by_vector(
            query="products",
            candidates=candidates,
            chunks_vdb=chunks_vdb,
            top_k=2,
            min_similarity=0.0,
            query_embedding=[1.0, 0.0],
        )


@pytest.mark.asyncio
async def test_query_path_vector_ranks_ingested_evidence_with_one_global_cap(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["kg_evidence_top_k"] = 2
    global_config["kg_evidence_min_similarity"] = -1.0
    payload = {
        "document_id": "doc_evidence",
        "chunks": [
            {"chunk_id": "chunk-1", "content": "ACME manufactures industrial pumps."},
            {"chunk_id": "chunk-2", "content": "ACME manufactures turbine components."},
            {"chunk_id": "chunk-3", "content": "ACME was founded in 1985."},
        ],
        "entities": [
            {
                "entity_name": "ACME",
                "entity_type": "ORG",
                "description": f"ACME fact {index}.",
                "source_id": f"chunk-{index}",
            }
            for index in range(1, 4)
        ],
        "relationships": [],
    }
    ingest_result = await ingest_extracted_json(payload, storage_instances, global_config)
    assert ingest_result["status"] == "success"

    result = await kg_query(
        query="What products does ACME manufacture?",
        knowledge_graph_inst=storage_instances["graph"],
        entities_vdb=storage_instances["entities_vdb"],
        relationships_vdb=storage_instances["relationships_vdb"],
        text_chunks_db=storage_instances["text_chunks"],
        query_param=QueryParam(mode="local", only_need_context=True, ll_keywords=["ACME"]),
        global_config=global_config,
        chunks_vdb=storage_instances["chunks_vdb"],
    )

    chunks = result.raw_data["data"]["chunks"]
    assert len(chunks) == 2
    assert all(chunk["chunk_id"].startswith("doc_evidence::chunk-") for chunk in chunks)


@pytest.mark.asyncio
async def test_query_path_fails_closed_instead_of_falling_back_when_vector_is_missing(
    storage_stack,
):
    storage_instances, global_config, _ = storage_stack
    payload = {
        "document_id": "doc_missing_vector",
        "chunks": [{"chunk_id": "chunk-1", "content": "ACME makes pumps."}],
        "entities": [
            {
                "entity_name": "ACME",
                "entity_type": "ORG",
                "description": "ACME is a manufacturer.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
    }
    ingest_result = await ingest_extracted_json(payload, storage_instances, global_config)
    assert ingest_result["status"] == "success"

    chunk_id = "doc_missing_vector::chunk-1"
    await storage_instances["chunks_vdb"].delete(
        [compute_mdhash_id(chunk_id, prefix="vchunk-")]
    )

    with pytest.raises(EvidenceVectorIntegrityError, match=chunk_id):
        await kg_query(
            query="What does ACME make?",
            knowledge_graph_inst=storage_instances["graph"],
            entities_vdb=storage_instances["entities_vdb"],
            relationships_vdb=storage_instances["relationships_vdb"],
            text_chunks_db=storage_instances["text_chunks"],
            query_param=QueryParam(mode="local", ll_keywords=["ACME"]),
            global_config=global_config,
            chunks_vdb=storage_instances["chunks_vdb"],
        )
