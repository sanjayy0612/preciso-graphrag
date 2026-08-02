"""Regression coverage for graph evidence and relationship integrity."""

from __future__ import annotations

import json

from config import GRAPH_FIELD_SEP
from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from ingest.reconciler import reconcile_extractions
from preciso_mcp.tools import ingest_from_file_tool, reconcile_tool
from tests._stubs import StubGraph, StubVDB, make_merge_config


async def test_distinct_relationships_do_not_collide_in_vector_storage():
    graph = StubGraph()
    relationship_vdb = StubVDB()
    config = make_merge_config()
    for source, target in (("AB", "C"), ("A", "BC")):
        await _merge_edges_then_upsert(
            source,
            target,
            [
                {
                    "description": f"{source} relates to {target}.",
                    "keywords": "relation",
                    "source_id": f"chunk-{source}-{target}",
                    "file_path": "document.md",
                    "weight": 1.0,
                    "timestamp": 1,
                }
            ],
            graph,
            relationship_vdb,
            StubVDB(),
            config,
            pipeline_status={},
        )

    assert len(graph.edges) == 2
    assert len(relationship_vdb.records) == 2


async def test_reingesting_identical_relationship_preserves_weight():
    graph = StubGraph()
    relationship_vdb = StubVDB()
    mention = [
        {
            "description": "Tim Cook leads Apple.",
            "keywords": "leadership",
            "source_id": "chunk-1",
            "file_path": "document.md",
            "weight": 1.0,
            "timestamp": 1,
        }
    ]
    for _ in range(2):
        await _merge_edges_then_upsert(
            "TIM_COOK",
            "APPLE",
            mention,
            graph,
            relationship_vdb,
            StubVDB(),
            make_merge_config(),
            pipeline_status={},
        )

    assert graph.edges[("APPLE", "TIM_COOK")]["weight"] == 1.0


async def test_new_relationship_evidence_increases_weight_once():
    graph = StubGraph()
    relationship_vdb = StubVDB()
    for source_id, weight in (("chunk-1", 1.0), ("chunk-2", 2.0)):
        await _merge_edges_then_upsert(
            "TIM_COOK",
            "APPLE",
            [
                {
                    "description": f"Evidence from {source_id}.",
                    "keywords": "leadership",
                    "source_id": source_id,
                    "file_path": "document.md",
                    "weight": weight,
                    "timestamp": 1,
                }
            ],
            graph,
            relationship_vdb,
            StubVDB(),
            make_merge_config(),
            pipeline_status={},
        )

    edge = graph.edges[("APPLE", "TIM_COOK")]
    assert edge["weight"] == 3.0
    assert edge["source_id"].split(GRAPH_FIELD_SEP) == ["chunk-1", "chunk-2"]


async def test_source_id_limits_count_each_cited_chunk():
    graph = StubGraph()
    source_id = f"chunk-1{GRAPH_FIELD_SEP}chunk-2"
    await _merge_nodes_then_upsert(
        "ACME",
        [
            {
                "entity_type": "ORG",
                "description": "ACME evidence.",
                "source_id": source_id,
                "file_path": "document.md",
                "timestamp": 1,
            }
        ],
        graph,
        StubVDB(),
        make_merge_config(max_source_ids=1),
        pipeline_status={},
    )

    assert graph.nodes["ACME"]["source_id"].split(GRAPH_FIELD_SEP) == ["chunk-1"]


async def test_relation_source_id_limits_count_each_cited_chunk():
    graph = StubGraph()
    source_id = f"chunk-1{GRAPH_FIELD_SEP}chunk-2"
    await _merge_edges_then_upsert(
        "ALPHA",
        "BETA",
        [
            {
                "description": "Alpha relates to Beta.",
                "keywords": "relation",
                "source_id": source_id,
                "file_path": "document.md",
                "weight": 1.0,
                "timestamp": 1,
            }
        ],
        graph,
        StubVDB(),
        StubVDB(),
        make_merge_config(max_source_ids=1),
        pipeline_status={},
    )

    assert graph.edges[("ALPHA", "BETA")]["source_id"].split(GRAPH_FIELD_SEP) == ["chunk-1"]


def test_reconciliation_uses_normalized_document_id():
    unified = reconcile_extractions(
        {
            "document_id": "report_part1",
            "entities": [],
            "relationships": [],
            "chunks": [],
        },
        [],
        document_id="report",
    )

    assert unified["document_id"] == "report"


async def test_file_ingest_forwards_pipeline_warnings(tmp_path, monkeypatch):
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(
        json.dumps({"document_id": "doc", "entities": [], "relationships": [], "chunks": []}),
        encoding="utf-8",
    )

    async def fake_ingest(*_args, **_kwargs):
        return {
            "status": "success",
            "entities_merged": 0,
            "relationships_merged": 0,
            "chunks_ingested": 0,
            "warnings": ["entity `GHOST` has unresolvable source_id(s): doc::chunk-99"],
        }

    monkeypatch.setattr(ingest_from_file_tool, "ingest_extracted_json", fake_ingest)
    result = await ingest_from_file_tool.ingest_from_file(str(extraction_path), {}, {})

    assert result["warnings"] == ["entity `GHOST` has unresolvable source_id(s): doc::chunk-99"]


async def test_file_ingest_reports_added_merged_and_duplicate_counts(tmp_path, monkeypatch):
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(
        json.dumps({"document_id": "doc", "entities": [], "relationships": [], "chunks": []}),
        encoding="utf-8",
    )
    counts = {
        "entities": {"added": 1, "merged": 2, "skipped_duplicate": 3},
        "relationships": {"added": 4, "merged": 5, "skipped_duplicate": 6},
        "chunks": {"added": 7, "merged": 8, "skipped_duplicate": 9},
    }

    async def fake_ingest(*_args, **_kwargs):
        return {
            "status": "success",
            "entities_merged": 6,
            "relationships_merged": 15,
            "chunks_ingested": 24,
            "ingestion_counts": counts,
        }

    monkeypatch.setattr(ingest_from_file_tool, "ingest_extracted_json", fake_ingest)
    result = await ingest_from_file_tool.ingest_from_file(str(extraction_path), {}, {})

    assert result["entities_added"] == 1
    assert result["relationships_added"] == 4
    assert result["chunks_stored"] == 7
    assert result["ingestion_counts"] == counts


async def test_reconciliation_forwards_pipeline_warnings(tmp_path, monkeypatch):
    extraction_path = tmp_path / "base.json"
    extraction_path.write_text(
        json.dumps({"document_id": "doc_part1", "entities": [], "relationships": [], "chunks": []}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    captured_payloads = []

    async def fake_ingest(*_args, **_kwargs):
        captured_payloads.append(_kwargs["payload"])
        return {
            "status": "success",
            "entities_merged": 0,
            "relationships_merged": 0,
            "chunks_ingested": 0,
            "errors": [],
            "warnings": ["relationship `A->B` has unresolvable source_id(s): doc::chunk-99"],
        }

    monkeypatch.setattr(reconcile_tool, "ingest_extracted_json", fake_ingest)
    result = await reconcile_tool.ingest_with_reconciliation([str(extraction_path)], {}, {})

    assert result["warnings"] == ["relationship `A->B` has unresolvable source_id(s): doc::chunk-99"]
    assert captured_payloads[0]["document_id"] == "doc"
