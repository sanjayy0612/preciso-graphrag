"""source_id -> chunk_id evidence links survive ingest.

Two invariants are covered here:

1. When the pipeline splits an oversized chunk into `-pN` parts, citations to the
   original chunk id must be remapped to those parts. Before the remap the cited id
   no longer existed in storage, so `core/query.py` silently dropped the evidence
   (`chunk_data is None` is skipped) and the entity looked unsupported.

2. A source_id matching no chunk at all is reported instead of being accepted in
   silence. Every SKILL.md and docs/{architecture,faq,getting-started}.md states that
   every source_id must map to a real chunk_id; nothing enforced it.
"""

from __future__ import annotations

import pytest

from config import GRAPH_FIELD_SEP
from ingest.pipeline import ingest_extracted_json
from ingest.transformer import resolve_source_id


def cited_chunks(record: dict) -> list[str]:
    return [part for part in record["source_id"].split(GRAPH_FIELD_SEP) if part]


async def resolvable(storage_instances: dict, record: dict) -> tuple[int, int]:
    """Return (retrievable, cited) for a node/edge's source_id."""
    ids = cited_chunks(record)
    found = await storage_instances["text_chunks"].get_by_ids(ids)
    return sum(1 for item in found if item is not None), len(ids)


def payload(**overrides) -> dict:
    base = {
        "document_id": "doc_src",
        "file_path": "doc_src.md",
        "timestamp": 1_700_000_000,
        "chunks": [{"chunk_id": "chunk-1", "content": "Apple reported record revenue."}],
        "entities": [
            {
                "entity_name": "APPLE",
                "entity_type": "ORG",
                "description": "Apple is a technology company.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
    }
    base.update(overrides)
    return base


# --- resolve_source_id unit behaviour -------------------------------------------------


def test_resolve_namespaces_unknown_ids_and_reports_them():
    resolved, unresolved = resolve_source_id("chunk-1", "doc_a", {})
    assert resolved == "doc_a::chunk-1"
    assert unresolved == ["chunk-1"]


def test_resolve_expands_split_chunk_to_every_part():
    chunk_map = {"chunk-1": ["doc_a::chunk-1-p1", "doc_a::chunk-1-p2"]}
    resolved, unresolved = resolve_source_id("chunk-1", "doc_a", chunk_map)
    assert resolved == "doc_a::chunk-1-p1<SEP>doc_a::chunk-1-p2"
    assert unresolved == []


def test_resolve_handles_mixed_known_and_unknown_ids():
    chunk_map = {"chunk-1": ["doc_a::chunk-1"]}
    resolved, unresolved = resolve_source_id(
        f"chunk-1{GRAPH_FIELD_SEP}chunk-9", "doc_a", chunk_map
    )
    assert resolved == "doc_a::chunk-1<SEP>doc_a::chunk-9"
    assert unresolved == ["chunk-9"]


def test_resolve_deduplicates_overlapping_expansions():
    chunk_map = {"chunk-1": ["doc_a::chunk-1-p1"], "chunk-2": ["doc_a::chunk-1-p1"]}
    resolved, _ = resolve_source_id(f"chunk-1{GRAPH_FIELD_SEP}chunk-2", "doc_a", chunk_map)
    assert resolved == "doc_a::chunk-1-p1"


def test_resolve_passes_empty_source_id_through():
    assert resolve_source_id("", "doc_a", {}) == ("", [])


# --- split-chunk evidence links -------------------------------------------------------


async def test_split_chunk_citation_stays_retrievable(storage_stack, monkeypatch):
    """The regression: a chunk over the char limit is stored as `-pN` parts, and the
    entity citing it must still reach every part."""
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_LIMIT", "100")
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_OVERLAP", "0")

    long_content = "Apple reported record revenue this quarter. " * 10  # ~430 chars
    result = await ingest_extracted_json(
        payload(chunks=[{"chunk_id": "chunk-1", "content": long_content}]),
        storage_instances,
        global_config,
    )

    assert result["status"] == "success", result
    assert result["chunks_ingested"] > 1, "content should have been split"
    assert "warnings" not in result, result.get("warnings")

    node = await storage_instances["graph"].get_node("APPLE")
    retrievable, cited = await resolvable(storage_instances, node)
    assert cited == result["chunks_ingested"]
    assert retrievable == cited, "every cited chunk part must be retrievable"


async def test_relationship_citation_survives_split(storage_stack, monkeypatch):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_LIMIT", "100")
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_OVERLAP", "0")

    long_content = "Tim Cook has led Apple since 2011 and drove its growth. " * 10
    result = await ingest_extracted_json(
        payload(
            chunks=[{"chunk_id": "chunk-1", "content": long_content}],
            entities=[
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
                    "source_id": "chunk-1",
                },
            ],
            relationships=[
                {
                    "src_id": "TIM_COOK",
                    "tgt_id": "APPLE",
                    "description": "Tim Cook leads Apple.",
                    "source_id": "chunk-1",
                }
            ],
        ),
        storage_instances,
        global_config,
    )

    assert result["status"] == "success", result
    edge = await storage_instances["graph"].get_edge("TIM_COOK", "APPLE")
    retrievable, cited = await resolvable(storage_instances, edge)
    assert cited > 1 and retrievable == cited


async def test_unsplit_chunk_citation_is_unchanged(storage_stack):
    """Chunks under the limit keep the plain `{document_id}::{chunk_id}` form."""
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(payload(), storage_instances, global_config)

    assert result["status"] == "success"
    node = await storage_instances["graph"].get_node("APPLE")
    assert node["source_id"] == "doc_src::chunk-1"
    retrievable, cited = await resolvable(storage_instances, node)
    assert (retrievable, cited) == (1, 1)


# --- dangling citations ---------------------------------------------------------------


async def test_dangling_source_id_is_reported_as_warning(storage_stack):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        payload(
            entities=[
                {
                    "entity_name": "GHOST",
                    "entity_type": "ORG",
                    "description": "Cited to a chunk that was never provided.",
                    "source_id": "chunk-99",
                }
            ]
        ),
        storage_instances,
        global_config,
    )

    # Default mode stays non-blocking so existing extractions keep ingesting.
    assert result["status"] == "success"
    assert result["errors"] == []
    assert any("chunk-99" in warning for warning in result["warnings"])
    assert any("GHOST" in warning for warning in result["warnings"])
    assert await storage_instances["graph"].get_node("GHOST") is not None


async def test_partially_dangling_source_id_keeps_the_valid_half(storage_stack):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        payload(
            entities=[
                {
                    "entity_name": "APPLE",
                    "entity_type": "ORG",
                    "description": "Apple is a technology company.",
                    "source_id": f"chunk-1{GRAPH_FIELD_SEP}chunk-99",
                }
            ]
        ),
        storage_instances,
        global_config,
    )

    assert result["status"] == "success"
    assert any("chunk-99" in warning for warning in result["warnings"])
    node = await storage_instances["graph"].get_node("APPLE")
    retrievable, cited = await resolvable(storage_instances, node)
    assert (retrievable, cited) == (1, 2), "the real chunk is still reachable"


async def test_valid_citation_produces_no_warning(storage_stack):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(payload(), storage_instances, global_config)
    assert "warnings" not in result


async def test_citation_to_earlier_ingest_is_not_flagged(storage_stack):
    """A follow-up payload may cite chunks written by a previous ingest of the same
    document. Those resolve in storage and must not be reported as dangling."""
    storage_instances, global_config, _ = storage_stack
    await ingest_extracted_json(payload(), storage_instances, global_config)

    result = await ingest_extracted_json(
        payload(
            chunks=[],
            entities=[
                {
                    "entity_name": "TIM_COOK",
                    "entity_type": "PERSON",
                    "description": "Tim Cook is Apple's CEO.",
                    "source_id": "chunk-1",
                }
            ],
        ),
        storage_instances,
        global_config,
    )

    assert result["status"] == "success"
    assert "warnings" not in result, result.get("warnings")
    node = await storage_instances["graph"].get_node("TIM_COOK")
    retrievable, cited = await resolvable(storage_instances, node)
    assert (retrievable, cited) == (1, 1)


# --- strict mode ----------------------------------------------------------------------


@pytest.fixture
def strict_mode(monkeypatch):
    monkeypatch.setenv("GRAPHRAG_STRICT_SOURCE_IDS", "true")


async def test_strict_mode_rejects_entity_with_dangling_citation(storage_stack, strict_mode):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        payload(
            entities=[
                {
                    "entity_name": "APPLE",
                    "entity_type": "ORG",
                    "description": "Apple is a technology company.",
                    "source_id": "chunk-1",
                },
                {
                    "entity_name": "GHOST",
                    "entity_type": "ORG",
                    "description": "Cited to a chunk that was never provided.",
                    "source_id": "chunk-99",
                },
            ]
        ),
        storage_instances,
        global_config,
    )

    assert result["status"] == "partial_success"
    assert any("chunk-99" in error for error in result["errors"])
    assert await storage_instances["graph"].get_node("GHOST") is None
    assert await storage_instances["graph"].get_node("APPLE") is not None


async def test_strict_mode_drops_edges_orphaned_by_a_rejected_entity(storage_stack, strict_mode):
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        payload(
            entities=[
                {
                    "entity_name": "APPLE",
                    "entity_type": "ORG",
                    "description": "Apple is a technology company.",
                    "source_id": "chunk-1",
                },
                {
                    "entity_name": "GHOST",
                    "entity_type": "ORG",
                    "description": "Cited to a chunk that was never provided.",
                    "source_id": "chunk-99",
                },
            ],
            relationships=[
                {
                    "src_id": "GHOST",
                    "tgt_id": "APPLE",
                    "description": "Edge hanging off a rejected entity.",
                    "source_id": "chunk-1",
                }
            ],
        ),
        storage_instances,
        global_config,
    )

    assert result["status"] == "partial_success"
    assert result["relationships_merged"] == 0
    assert await storage_instances["graph"].get_edge("GHOST", "APPLE") is None


async def test_strict_mode_keeps_entity_with_one_good_and_one_bad_mention(
    storage_stack, strict_mode
):
    """The same entity may be extracted twice. If either mention cites a real chunk the
    entity survives, and its edges must survive with it."""
    storage_instances, global_config, _ = storage_stack
    result = await ingest_extracted_json(
        payload(
            chunks=[
                {"chunk_id": "chunk-1", "content": "Apple reported record revenue."},
                {"chunk_id": "chunk-2", "content": "Tim Cook has been CEO since 2011."},
            ],
            entities=[
                {
                    "entity_name": "APPLE",
                    "entity_type": "ORG",
                    "description": "Apple is a technology company.",
                    "source_id": "chunk-1",
                },
                {
                    "entity_name": "APPLE",
                    "entity_type": "ORG",
                    "description": "Second mention citing a missing chunk.",
                    "source_id": "chunk-99",
                },
                {
                    "entity_name": "TIM_COOK",
                    "entity_type": "PERSON",
                    "description": "Tim Cook is Apple's CEO.",
                    "source_id": "chunk-2",
                },
            ],
            relationships=[
                {
                    "src_id": "TIM_COOK",
                    "tgt_id": "APPLE",
                    "description": "Tim Cook leads Apple.",
                    "source_id": "chunk-2",
                }
            ],
        ),
        storage_instances,
        global_config,
    )

    assert await storage_instances["graph"].get_node("APPLE") is not None
    assert result["relationships_merged"] == 1, "the valid edge must not be dropped"
    assert await storage_instances["graph"].get_edge("TIM_COOK", "APPLE") is not None


async def test_strict_mode_accepts_valid_split_citations(storage_stack, strict_mode, monkeypatch):
    """Strict mode must not reject citations that the remap resolved correctly."""
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_LIMIT", "100")
    monkeypatch.setenv("GRAPHRAG_CHUNK_CHAR_OVERLAP", "0")

    long_content = "Apple reported record revenue this quarter. " * 10
    result = await ingest_extracted_json(
        payload(chunks=[{"chunk_id": "chunk-1", "content": long_content}]),
        storage_instances,
        global_config,
    )

    assert result["status"] == "success", result
    assert result["errors"] == []
    assert await storage_instances["graph"].get_node("APPLE") is not None
