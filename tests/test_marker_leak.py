"""SUMMARY_MARKER exit-surface invariant (pytest port of
test/marker_leak_manual.py, which stays as the runnable manual script):

    storage keeps the marker, every exit strips it.

Drives the REAL merge write path until an entity/relation is flagged pending,
then submits an agent-written summary via submit_summary — the only path that
ever writes a SUMMARY_MARKER segment — and asserts the marker is present in
storage but absent from every exit surface. When a new export/output surface
is added, extend this module."""

from __future__ import annotations

import pytest

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.export_adapters import _edge_properties, _node_properties, _vector_payload
from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from core.utils import convert_to_user_format, make_relation_chunk_key
from preciso_mcp.tools.pending_summaries_tool import list_pending_summaries, submit_summary
from tests._stubs import StubGraph, StubVDB, contains_marker, make_merge_config

RAW_TAIL_SIZE = 2  # small so a handful of merges forces a pending record
N_MERGES = 8


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class StubPendingKV:
    """Minimal pending_summaries stand-in: the subset of JsonKVStorage the
    pending-summary tools and _sync_pending_summary_record actually use."""

    def __init__(self):
        self._data: dict = {}
        self._storage_lock = _NullLock()

    async def get_by_id(self, id):
        return self._data.get(id)

    async def upsert(self, payload):
        self._data.update(payload)

    async def delete(self, ids):
        for i in ids:
            self._data.pop(i, None)

    async def get_all_items(self):
        return {k: dict(v) for k, v in self._data.items()}

    async def index_done_callback(self):
        pass


async def _run_merges(pending):
    """Force one entity and one relation into pending via the real merge write
    path, returning (graph, entity_vdb, relationships_vdb, config, pipeline_status)."""
    config = make_merge_config(raw_tail_size=RAW_TAIL_SIZE)
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
            pending_summaries_storage=pending,
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
            pending_summaries_storage=pending,
        )
    return graph, entity_vdb, relationships_vdb, config, pipeline_status


@pytest.fixture
async def pending_state():
    """Force one entity and one relation into pending via the real merge write
    path, then expose storage_instances for the list_pending_summaries /
    submit_summary MCP tools — these are new exit surfaces per the
    SUMMARY_MARKER contract in config.py and belong under the same guard as
    the other summarized_state assertions below."""
    pending = StubPendingKV()
    graph, entity_vdb, relationships_vdb, config, _pipeline_status = await _run_merges(pending)

    storage_instances = {
        "graph": graph,
        "entities_vdb": entity_vdb,
        "relationships_vdb": relationships_vdb,
        "pending_summaries": pending,
    }
    return storage_instances, config


async def test_list_pending_summaries_output_is_clean(pending_state):
    storage_instances, config = pending_state
    result = await list_pending_summaries(storage_instances, config, limit=50)
    assert result["items"], "precondition: at least one pending item"
    assert not contains_marker(result)


async def test_submit_summary_output_is_clean(pending_state):
    storage_instances, config = pending_state
    pending = storage_instances["pending_summaries"]
    record = await pending.get_by_id("ACME_CORP")
    # Even a hostile agent-submitted summary containing the literal marker text
    # must not leak it back out of the tool's own return payload.
    result = await submit_summary(
        storage_instances,
        config,
        name="ACME_CORP",
        kind="entity",
        summary_text=f"{SUMMARY_MARKER} an agent might echo the marker in submitted text",
        expected_description_count=record["description_count"],
    )
    assert result["status"] == "success"
    assert not contains_marker(result)


@pytest.fixture
async def summarized_state():
    """Run N real merges for one entity and one relationship (forcing them
    pending), then submit an agent-written summary for each so storage
    genuinely contains a SUMMARY_MARKER segment."""
    pending = StubPendingKV()
    graph, entity_vdb, relationships_vdb, config, pipeline_status = await _run_merges(pending)

    storage_instances = {
        "graph": graph,
        "entities_vdb": entity_vdb,
        "relationships_vdb": relationships_vdb,
        "pending_summaries": pending,
    }

    entity_record = await pending.get_by_id("ACME_CORP")
    await submit_summary(
        storage_instances,
        config,
        name="ACME_CORP",
        kind="entity",
        summary_text="Agent-written rolling summary of ACME.",
        expected_description_count=entity_record["description_count"],
    )
    edge_key = make_relation_chunk_key("ACME_CORP", "TIM_APPLE")
    edge_record = await pending.get_by_id(edge_key)
    await submit_summary(
        storage_instances,
        config,
        name="ACME_CORP~TIM_APPLE",
        kind="relation",
        summary_text="Agent-written rolling summary of the relation.",
        expected_description_count=edge_record["description_count"],
        src="ACME_CORP",
        tgt="TIM_APPLE",
    )
    return graph, entity_vdb, relationships_vdb, pipeline_status


async def test_storage_keeps_marker(summarized_state):
    graph, _, _, pipeline_status = summarized_state
    assert SUMMARY_MARKER in graph.nodes["ACME_CORP"]["description"]
    assert SUMMARY_MARKER in graph.edges[("ACME_CORP", "TIM_APPLE")]["description"]
    assert pipeline_status.get("summary_events"), "precondition: merges flagged at least one entity pending"


async def test_vdb_records_are_clean(summarized_state):
    _, entity_vdb, relationships_vdb, _ = summarized_state
    assert not any(contains_marker(r) for r in entity_vdb.records.values())
    assert not any(contains_marker(r) for r in relationships_vdb.records.values())


async def test_user_facing_results_are_clean(summarized_state):
    graph, _, _, _ = summarized_state
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
    graph, _, _, _ = summarized_state
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
