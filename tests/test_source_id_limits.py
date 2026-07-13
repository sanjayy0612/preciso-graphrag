"""Source-id limiting: the pure helpers in core/utils.py and the KEEP vs FIFO
policy observed through the real merge path in core/merge.py."""

from __future__ import annotations

from config import (
    GRAPH_FIELD_SEP,
    SOURCE_IDS_LIMIT_METHOD_FIFO,
    SOURCE_IDS_LIMIT_METHOD_KEEP,
)
from core.merge import _merge_nodes_then_upsert
from core.utils import apply_source_ids_limit, merge_source_ids
from tests._stubs import StubGraph, StubVDB, make_merge_config


def test_merge_source_ids_dedupes_preserving_order():
    assert merge_source_ids(["a", "b"], ["b", "c", "a", "d"]) == ["a", "b", "c", "d"]
    assert merge_source_ids(None, ["x", "", "x"]) == ["x"]
    assert merge_source_ids([], None) == []


def test_apply_limit_under_limit_is_unchanged():
    ids = ["a", "b", "c"]
    assert apply_source_ids_limit(ids, 5, SOURCE_IDS_LIMIT_METHOD_KEEP) == ids
    assert apply_source_ids_limit(ids, 3, SOURCE_IDS_LIMIT_METHOD_FIFO) == ids


def test_apply_limit_keep_retains_oldest():
    ids = ["a", "b", "c", "d", "e"]
    assert apply_source_ids_limit(ids, 3, SOURCE_IDS_LIMIT_METHOD_KEEP) == ["a", "b", "c"]


def test_apply_limit_fifo_retains_newest():
    ids = ["a", "b", "c", "d", "e"]
    assert apply_source_ids_limit(ids, 3, SOURCE_IDS_LIMIT_METHOD_FIFO) == ["c", "d", "e"]


def test_apply_limit_zero_or_negative_empties():
    assert apply_source_ids_limit(["a", "b"], 0, SOURCE_IDS_LIMIT_METHOD_KEEP) == []
    assert apply_source_ids_limit(["a", "b"], -1, SOURCE_IDS_LIMIT_METHOD_FIFO) == []


async def _merge_n_mentions(method: str, limit: int, n: int) -> dict:
    """Run n single-mention merges of one entity through the real merge path."""
    config = make_merge_config(
        None,  # no LLM needed: descriptions stay tiny
        source_ids_limit_method=method,
        max_source_ids=limit,
    )
    graph = StubGraph()
    entity_vdb = StubVDB()
    for i in range(n):
        await _merge_nodes_then_upsert(
            "ACME_CORP",
            [
                {
                    "entity_type": "ORG",
                    "description": f"Fact {i}.",
                    "source_id": f"chunk-{i}",
                    "file_path": f"doc_{i}.md",
                    "timestamp": i,
                }
            ],
            graph,
            entity_vdb,
            config,
            pipeline_status={},
        )
    return graph.nodes["ACME_CORP"]


async def test_merge_keep_policy_keeps_oldest_source_ids():
    node = await _merge_n_mentions(SOURCE_IDS_LIMIT_METHOD_KEEP, limit=3, n=5)
    assert node["source_id"].split(GRAPH_FIELD_SEP) == ["chunk-0", "chunk-1", "chunk-2"]


async def test_merge_fifo_policy_keeps_newest_source_ids():
    node = await _merge_n_mentions(SOURCE_IDS_LIMIT_METHOD_FIFO, limit=3, n=5)
    assert node["source_id"].split(GRAPH_FIELD_SEP) == ["chunk-2", "chunk-3", "chunk-4"]
