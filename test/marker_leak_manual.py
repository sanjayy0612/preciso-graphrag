#!/usr/bin/env python
"""
Manual guard test for the SUMMARY_MARKER exit-surface invariant (see the
CONTRACT comment above SUMMARY_MARKER in config.py):

    storage keeps the marker, every exit strips it.

Drives the REAL merge write path (_merge_nodes_then_upsert /
_merge_edges_then_upsert) with in-memory stubs until an entity/relation is
flagged pending, then submits an agent-written summary via submit_summary —
the only path that ever writes a SUMMARY_MARKER segment — and asserts the
marker:
  - IS present in the stored graph node/edge description (storage keeps it),
  - is ABSENT from every VDB upsert payload value (embedding + Qdrant source),
  - is ABSENT from every convert_to_user_format field (user-facing results),
  - is ABSENT from the Neo4j adapter property dicts,
  - is ABSENT from the Qdrant _vector_payload, even for a hostile record.
  - is ABSENT from list_pending_summaries / submit_summary tool outputs.

Uses a deterministic tokenizer — no Ollama/network required.

Usage:
    python test/marker_leak_manual.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import GRAPH_FIELD_SEP, SOURCE_IDS_LIMIT_METHOD_KEEP, SUMMARY_MARKER
from core.export_adapters import _edge_properties, _node_properties, _vector_payload
from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from core.utils import convert_to_user_format, make_relation_chunk_key
from preciso_mcp.tools.pending_summaries_tool import list_pending_summaries, submit_summary

RAW_TAIL_SIZE = 2  # small so a handful of merges forces a pending record
N_MERGES = 8

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def contains_marker(value) -> bool:
    return SUMMARY_MARKER in json.dumps(value, ensure_ascii=False, default=str)


class StubTokenizer:
    def encode(self, text):
        return (text or "").split()

    def decode(self, tokens):
        return " ".join(tokens)


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


class StubGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(src, tgt):
        return tuple(sorted((src, tgt)))

    async def get_node(self, name):
        return self.nodes.get(name)

    async def upsert_node(self, name, node_data):
        self.nodes[name] = dict(node_data)

    async def has_edge(self, src, tgt):
        return self._key(src, tgt) in self.edges

    async def get_edge(self, src, tgt):
        return self.edges.get(self._key(src, tgt))

    async def upsert_edge(self, src, tgt, edge_data):
        self.edges[self._key(src, tgt)] = dict(edge_data)

    async def index_done_callback(self):
        pass


class StubVDB:
    """Captures every payload the merge path would embed/export."""

    def __init__(self):
        self.records: dict[str, dict] = {}

    async def upsert(self, payload):
        self.records.update({k: dict(v) for k, v in payload.items()})

    async def index_done_callback(self):
        pass

    async def delete(self, ids):
        for record_id in ids:
            self.records.pop(record_id, None)


def make_config():
    return {
        "llm_model_func": None,  # summary compression never uses an LLM
        "tokenizer": StubTokenizer(),
        "summary_context_size": 200,
        "summary_max_tokens": 50,
        "raw_tail_size": RAW_TAIL_SIZE,
        "source_ids_limit_method": SOURCE_IDS_LIMIT_METHOD_KEEP,
        "max_source_ids_per_entity": 100,
        "max_source_ids_per_relation": 100,
        "max_file_paths": 10,
    }


async def build_summarized_state():
    """Run N real merges for one entity and one relationship (forcing them
    pending), then submit an agent-written summary for each via submit_summary
    so storage genuinely contains a SUMMARY_MARKER segment."""
    pending = StubPendingKV()
    config = make_config()
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

    storage_instances = {
        "graph": graph,
        "entities_vdb": entity_vdb,
        "relationships_vdb": relationships_vdb,
        "pending_summaries": pending,
    }

    list_result = await list_pending_summaries(storage_instances, config, limit=50)

    entity_record = await pending.get_by_id("ACME_CORP")
    entity_submit_result = await submit_summary(
        storage_instances,
        config,
        name="ACME_CORP",
        kind="entity",
        summary_text="Agent-written rolling summary of ACME.",
        expected_description_count=entity_record["description_count"],
    )
    edge_key = make_relation_chunk_key("ACME_CORP", "TIM_APPLE")
    edge_record = await pending.get_by_id(edge_key)
    relation_submit_result = await submit_summary(
        storage_instances,
        config,
        name="ACME_CORP~TIM_APPLE",
        kind="relation",
        summary_text="Agent-written rolling summary of the relation.",
        expected_description_count=edge_record["description_count"],
        src="ACME_CORP",
        tgt="TIM_APPLE",
    )
    return graph, entity_vdb, relationships_vdb, pipeline_status, list_result, entity_submit_result, relation_submit_result


async def main() -> None:
    (
        graph,
        entity_vdb,
        relationships_vdb,
        pipeline_status,
        list_result,
        entity_submit_result,
        relation_submit_result,
    ) = await build_summarized_state()
    node = graph.nodes["ACME_CORP"]
    edge = graph.edges[("ACME_CORP", "TIM_APPLE")]

    print("\n[1] Storage keeps the marker (precondition: an agent summary was actually submitted)")
    check(entity_submit_result["status"] == "success", "entity submit_summary succeeded")
    check(relation_submit_result["status"] == "success", "relation submit_summary succeeded")
    check(SUMMARY_MARKER in node.get("description", ""), "graph node description keeps the marker")
    check(SUMMARY_MARKER in edge.get("description", ""), "graph edge description keeps the marker")
    check(bool(pipeline_status.get("summary_events")), "summary_events still reported")

    print("\n[2] VDB records (embedding content + Qdrant payload source) are clean")
    check(
        not any(contains_marker(record) for record in entity_vdb.records.values()),
        "no entity VDB payload value carries the marker",
    )
    check(
        not any(contains_marker(record) for record in relationships_vdb.records.values()),
        "no relationship VDB payload value carries the marker",
    )

    print("\n[3] User-facing query results are clean")
    entities_context = [
        {
            "entity": "ACME_CORP",
            "type": node.get("entity_type", "UNKNOWN"),
            "description": node.get("description", ""),
        }
    ]
    relations_context = [
        {
            "entity1": "ACME_CORP",
            "entity2": "TIM_APPLE",
            "description": edge.get("description", ""),
        }
    ]
    original_entity = {**node, "entity_name": "ACME_CORP"}
    original_relation = {**edge, "src_id": "ACME_CORP", "tgt_id": "TIM_APPLE"}
    user_format = convert_to_user_format(
        entities_context,
        relations_context,
        [],
        [],
        "mix",
        {"ACME_CORP": original_entity},
        {("ACME_CORP", "TIM_APPLE"): original_relation},
    )
    check(not contains_marker(user_format), "no convert_to_user_format field carries the marker")

    print("\n[4] Neo4j export properties are clean")
    node_props = _node_properties({**node, "id": "ACME_CORP"}, "ws")
    edge_props = _edge_properties({**edge, "source": "ACME_CORP", "target": "TIM_APPLE"}, "ws")
    check(not contains_marker(node_props), "Neo4j node properties clean")
    check(not contains_marker(edge_props), "Neo4j edge properties clean")

    print("\n[5] Qdrant payload builder strips even a hostile record")
    hostile = {
        "__id__": "rel-x",
        "description": f"{SUMMARY_MARKER} smuggled summary{GRAPH_FIELD_SEP}raw fact",
        "nested_list": [f"{SUMMARY_MARKER} in a list"],
    }
    payload = _vector_payload(hostile, "relationships", "ws")
    check(not contains_marker(payload), "Qdrant payload clean even for marker-bearing record")

    print("\n[6] list_pending_summaries / submit_summary tool outputs are clean")
    check(not contains_marker(list_result), "list_pending_summaries output carries no marker")
    check(not contains_marker(entity_submit_result), "submit_summary (entity) output carries no marker")
    check(not contains_marker(relation_submit_result), "submit_summary (relation) output carries no marker")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
