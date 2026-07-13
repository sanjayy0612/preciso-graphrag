"""SUMMARY_MARKER exit-surface invariant (pytest port of
test/marker_leak_manual.py, which stays as the runnable manual script):

    storage keeps the marker, every exit strips it.

Drives the REAL merge write path until a rolling summary is forced, then
asserts the marker is present in storage but absent from every exit surface.
When a new export/output surface is added, extend this module."""

from __future__ import annotations

import pytest

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.export_adapters import _edge_properties, _node_properties, _vector_payload
from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from core.utils import convert_to_user_format
from tests._stubs import StubGraph, StubLLM, StubVDB, contains_marker, make_merge_config

RAW_TAIL_SIZE = 2  # small so a handful of merges forces the rolling summary
N_MERGES = 8


@pytest.fixture
async def summarized_state():
    """Run N real merges for one entity and one relationship, forcing a summary."""
    llm = StubLLM()
    config = make_merge_config(llm, raw_tail_size=RAW_TAIL_SIZE)
    graph = StubGraph()
    entity_vdb = StubVDB()
    relationships_vdb = StubVDB()
    pipeline_status: dict = {}

    for i in range(N_MERGES):
        await _merge_nodes_then_upsert(
            "ACME_CORP",
            [
                {
                    "entity_type": "ORG",
                    "description": f"Verbatim fact number {i} about ACME from a source document.",
                    "source_id": f"chunk-{i}",
                    "file_path": f"doc_{i}.md",
                    "timestamp": i,
                }
            ],
            graph,
            entity_vdb,
            config,
            pipeline_status=pipeline_status,
        )
        await _merge_edges_then_upsert(
            "ACME_CORP",
            "TIM_APPLE",
            [
                {
                    "weight": 1.0,
                    "description": f"Verbatim relation fact number {i} between the two parties.",
                    "keywords": "employment",
                    "source_id": f"chunk-{i}",
                    "file_path": f"doc_{i}.md",
                    "timestamp": i,
                }
            ],
            graph,
            relationships_vdb,
            entity_vdb,
            config,
            pipeline_status=pipeline_status,
        )
    return graph, entity_vdb, relationships_vdb, pipeline_status, llm


async def test_storage_keeps_marker(summarized_state):
    graph, _, _, pipeline_status, llm = summarized_state
    assert llm.calls > 0, "precondition: merges forced at least one LLM summarization"
    assert SUMMARY_MARKER in graph.nodes["ACME_CORP"]["description"]
    assert SUMMARY_MARKER in graph.edges[("ACME_CORP", "TIM_APPLE")]["description"]
    assert pipeline_status.get("summary_events")


async def test_vdb_records_are_clean(summarized_state):
    _, entity_vdb, relationships_vdb, _, _ = summarized_state
    assert not any(contains_marker(r) for r in entity_vdb.records.values())
    assert not any(contains_marker(r) for r in relationships_vdb.records.values())


async def test_user_facing_results_are_clean(summarized_state):
    graph, _, _, _, _ = summarized_state
    node = graph.nodes["ACME_CORP"]
    edge = graph.edges[("ACME_CORP", "TIM_APPLE")]
    user_format = convert_to_user_format(
        [{"entity": "ACME_CORP", "type": node.get("entity_type", "UNKNOWN"), "description": node["description"]}],
        [{"entity1": "ACME_CORP", "entity2": "TIM_APPLE", "description": edge["description"]}],
        [],
        [],
        "mix",
        {"ACME_CORP": {**node, "entity_name": "ACME_CORP"}},
        {("ACME_CORP", "TIM_APPLE"): {**edge, "src_id": "ACME_CORP", "tgt_id": "TIM_APPLE"}},
    )
    assert not contains_marker(user_format)


async def test_neo4j_export_properties_are_clean(summarized_state):
    graph, _, _, _, _ = summarized_state
    node_props = _node_properties({**graph.nodes["ACME_CORP"], "id": "ACME_CORP"}, "ws")
    edge_props = _edge_properties(
        {**graph.edges[("ACME_CORP", "TIM_APPLE")], "source": "ACME_CORP", "target": "TIM_APPLE"}, "ws"
    )
    assert not contains_marker(node_props)
    assert not contains_marker(edge_props)


def test_qdrant_payload_strips_hostile_record():
    hostile = {
        "__id__": "rel-x",
        "description": f"{SUMMARY_MARKER} smuggled summary{GRAPH_FIELD_SEP}raw fact",
        "nested_list": [f"{SUMMARY_MARKER} in a list"],
    }
    assert not contains_marker(_vector_payload(hostile, "relationships", "ws"))
