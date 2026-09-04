"""Stage-3 directed storage and deterministic supply-chain query coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.profiles import SUPPLY_CHAIN_WORKSPACE
from core.storage.base import EmbeddingFunc
from core.storage import shared_storage
from core.supply_chain import directed_relationship_id, query_facility_unavailable
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json
from preciso_mcp import server


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "supply_chain"


def fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


async def storage_stack(tmp_path):
    config = build_global_config(
        working_dir=str(tmp_path),
        tokenizer=BasicTokenizer(),
        embedding_func=EmbeddingFunc(
            embedding_dim=8,
            max_token_size=8192,
            func=_fallback_embed,
            model_name="fallback",
        ),
    )
    storage = build_storage_instances(config, workspace=SUPPLY_CHAIN_WORKSPACE)
    await initialize_storage_instances(storage)
    return storage, config


async def ingested_fixture_stack(tmp_path):
    storage, config = await storage_stack(tmp_path)
    result = await ingest_extracted_json(fixture("expected_extraction.json"), storage, config)
    assert result["status"] == "success", result
    return storage, config


@pytest.mark.asyncio
async def test_northbridge_query_matches_ground_truth_and_excludes_unrelated_branch(tmp_path):
    storage, config = await ingested_fixture_stack(tmp_path)
    ground_truth = fixture("ground_truth.json")

    result = await query_facility_unavailable(
        "facility:arkon-components:northbridge", storage, config
    )

    expected = next(
        item
        for item in ground_truth["expected_facility_to_product_paths"]
        if item["facility"] == "facility:arkon-components:northbridge"
    )
    assert result["status"] == "success", result
    assert result["workspace"] == SUPPLY_CHAIN_WORKSPACE
    assert result["snapshot"]["effective_dates"] == ["2026-01-15"]
    assert result["scenario"]["hypothetical"] is True
    assert [item["product_id"] for item in result["potentially_exposed_products"]] == expected["products"]
    assert all(
        excluded not in {item["product_id"] for item in result["potentially_exposed_products"]}
        for excluded in expected["must_not_include"]
    )
    for product in result["potentially_exposed_products"]:
        for path in product["paths"]:
            assert path["nodes"][0] == "facility:arkon-components:northbridge"
            assert [edge["relationship_type"] for edge in path["edges"]] == ["MANUFACTURES", "USED_IN"]
            assert all(edge["evidence"][0]["chunk"]["content"] for edge in path["edges"])


@pytest.mark.asyncio
async def test_query_keeps_separate_paths_to_one_product_and_has_stable_truncation(tmp_path):
    storage, config = await ingested_fixture_stack(tmp_path)

    full = await query_facility_unavailable("facility:arkon-components:northbridge", storage, config)
    limited = await query_facility_unavailable(
        "facility:arkon-components:northbridge", storage, config, max_paths=1
    )

    assert [item["product_id"] for item in full["potentially_exposed_products"]] == [
        "product:aquapump:300",
        "product:aquapump:500",
    ]
    assert limited["completeness"] == {"is_truncated": True, "max_paths": 1}
    assert [item["product_id"] for item in limited["potentially_exposed_products"]] == [
        "product:aquapump:300"
    ]


@pytest.mark.asyncio
async def test_query_keeps_distinct_component_paths_to_the_same_product(tmp_path):
    storage, config = await ingested_fixture_stack(tmp_path)
    additional_path = {
        "document_id": "northbridge_secondary_component",
        "snapshot_effective_date": "2026-01-15",
        "chunks": [{"chunk_id": "secondary", "content": "Northbridge manufactures Sensor S-2, used in AquaPump 500."}],
        "entities": [
            {"entity_name": "facility:arkon-components:northbridge", "entity_type": "FACILITY", "description": "Northbridge Fabrication Facility.", "source_id": "secondary"},
            {"entity_name": "component:arkon-components:sensor-s2", "entity_type": "COMPONENT", "description": "Sensor S-2.", "source_id": "secondary"},
            {"entity_name": "product:aquapump:500", "entity_type": "PRODUCT", "description": "AquaPump 500.", "source_id": "secondary"},
        ],
        "relationships": [
            {"src_id": "facility:arkon-components:northbridge", "tgt_id": "component:arkon-components:sensor-s2", "keywords": "MANUFACTURES", "description": "Northbridge manufactures Sensor S-2.", "source_id": "secondary"},
            {"src_id": "component:arkon-components:sensor-s2", "tgt_id": "product:aquapump:500", "keywords": "USED_IN", "description": "Sensor S-2 is used in AquaPump 500.", "source_id": "secondary"},
        ],
    }
    assert (await ingest_extracted_json(additional_path, storage, config))["status"] == "success"

    result = await query_facility_unavailable("facility:arkon-components:northbridge", storage, config)
    aquapump_500 = next(item for item in result["potentially_exposed_products"] if item["product_id"] == "product:aquapump:500")

    assert len(aquapump_500["paths"]) == 2
    assert [path["nodes"][1] for path in aquapump_500["paths"]] == [
        "component:arkon-components:control-unit-c17",
        "component:arkon-components:sensor-s2",
    ]


@pytest.mark.asyncio
async def test_directed_relationship_evidence_is_edge_specific_idempotent_and_persistent(
    tmp_path, monkeypatch
):
    storage, config = await ingested_fixture_stack(tmp_path)
    supporting_payload = {
        "document_id": "northbridge_supporting_record",
        "snapshot_effective_date": "2026-01-15",
        "chunks": [
            {
                "chunk_id": "support",
                "content": "Northbridge Fabrication Facility manufactures Control Unit C-17.",
            }
        ],
        "entities": [
            {
                "entity_name": "facility:arkon-components:northbridge",
                "entity_type": "FACILITY",
                "description": "Northbridge Fabrication Facility.",
                "source_id": "support",
            },
            {
                "entity_name": "component:arkon-components:control-unit-c17",
                "entity_type": "COMPONENT",
                "description": "Control Unit C-17.",
                "source_id": "support",
            },
        ],
        "relationships": [
            {
                "src_id": "facility:arkon-components:northbridge",
                "tgt_id": "component:arkon-components:control-unit-c17",
                "keywords": "MANUFACTURES",
                "description": "Northbridge Fabrication Facility manufactures Control Unit C-17.",
                "source_id": "support",
            }
        ],
    }

    first = await ingest_extracted_json(supporting_payload, storage, config)
    second = await ingest_extracted_json(supporting_payload, storage, config)
    record_id = directed_relationship_id(
        "facility:arkon-components:northbridge", "MANUFACTURES", "component:arkon-components:control-unit-c17"
    )
    record = await storage["directed_relationships"].get_by_id(record_id)

    assert first["status"] == second["status"] == "success"
    assert len(record["evidence"]) == 2
    assert [item["source_id"] for item in record["evidence"]] == [
        "northbridge_supporting_record::support",
        "synthetic_supply_chain_snapshot_2026_01_15::facility_001",
    ]

    persisted_sidecar = (
        tmp_path / SUPPLY_CHAIN_WORKSPACE / "kv_store_directed_relationships.json"
    )
    assert record_id in json.loads(persisted_sidecar.read_text(encoding="utf-8"))

    # Rebuild the JSON-KV namespace cache so this next initialization must read
    # the sidecar from disk, rather than reusing the first storage instance.
    monkeypatch.setattr(shared_storage, "_namespace_data", {})
    monkeypatch.setattr(shared_storage, "_namespace_locks", {})
    monkeypatch.setattr(shared_storage, "_namespace_update_flags", {})
    monkeypatch.setattr(shared_storage, "_namespace_init_flags", {})
    monkeypatch.setattr(shared_storage, "_keyed_locks", {})
    monkeypatch.setattr(shared_storage, "_data_init_lock", asyncio.Lock())
    reloaded = build_storage_instances(config, workspace=SUPPLY_CHAIN_WORKSPACE)
    await initialize_storage_instances(reloaded)
    reloaded_record = await reloaded["directed_relationships"].get_by_id(record_id)
    assert len(reloaded_record["evidence"]) == 2
    monkeypatch.setattr(server, "global_config", config)
    monkeypatch.setattr(server, "workspace_storage_instances", {SUPPLY_CHAIN_WORKSPACE: reloaded})
    result = await server.query_facility_unavailable_tool("facility:arkon-components:northbridge")
    evidence = result["potentially_exposed_products"][0]["paths"][0]["edges"][0]["evidence"]
    assert [item["source_id"] for item in evidence] == [
        "northbridge_supporting_record::support",
        "synthetic_supply_chain_snapshot_2026_01_15::facility_001",
    ]


@pytest.mark.asyncio
async def test_query_enforces_direction_and_never_resolves_ambiguous_plant_7(tmp_path):
    storage, config = await ingested_fixture_stack(tmp_path)
    reverse_record_id = directed_relationship_id(
        "component:arkon-components:control-unit-c17", "MANUFACTURES", "facility:arkon-components:northbridge"
    )
    await storage["directed_relationships"].upsert(
        {
            reverse_record_id: {
                "src_id": "component:arkon-components:control-unit-c17",
                "relationship_type": "MANUFACTURES",
                "tgt_id": "facility:arkon-components:northbridge",
                "evidence": [
                    {
                        "observation_id": "reverse-test",
                        "source_id": "synthetic_supply_chain_snapshot_2026_01_15::facility_001",
                        "description": "Intentionally reversed record.",
                        "file_path": "test.md",
                        "document_id": "synthetic_supply_chain_snapshot_2026_01_15",
                    }
                ],
            }
        }
    )
    await storage["directed_relationships"].index_done_callback()

    result = await query_facility_unavailable("facility:arkon-components:northbridge", storage, config)
    ambiguous = await query_facility_unavailable("Plant 7", storage, config)

    assert result["status"] == "success"
    assert all(
        path["nodes"][0] == "facility:arkon-components:northbridge"
        for product in result["potentially_exposed_products"]
        for path in product["paths"]
    )
    assert ambiguous["status"] == "unknown_facility"


@pytest.mark.asyncio
async def test_query_fails_closed_for_missing_evidence_or_incomplete_commit(tmp_path):
    storage, config = await ingested_fixture_stack(tmp_path)
    await storage["text_chunks"].delete(["synthetic_supply_chain_snapshot_2026_01_15::bom_001"])
    await storage["text_chunks"].index_done_callback()

    missing_evidence = await query_facility_unavailable(
        "facility:arkon-components:northbridge", storage, config
    )
    assert missing_evidence["status"] == "inconsistent_evidence"
    assert missing_evidence["potentially_exposed_products"] == []

    storage, config = await ingested_fixture_stack(tmp_path / "pending")
    await storage["supply_chain_commits"].upsert(
        {"interrupted_document": {"status": "pending", "document_id": "interrupted_document"}}
    )
    await storage["supply_chain_commits"].index_done_callback()
    incomplete = await query_facility_unavailable("facility:arkon-components:northbridge", storage, config)
    assert incomplete["status"] == "inconsistent_storage"
    assert incomplete["incomplete_documents"] == ["interrupted_document"]


@pytest.mark.asyncio
async def test_failed_directed_sidecar_write_marks_document_failed_and_blocks_query(tmp_path, monkeypatch):
    storage, config = await storage_stack(tmp_path)

    async def fail_write():
        raise OSError("simulated directed-sidecar persistence failure")

    monkeypatch.setattr(storage["directed_relationships"], "index_done_callback", fail_write)
    result = await ingest_extracted_json(fixture("expected_extraction.json"), storage, config)

    commit = await storage["supply_chain_commits"].get_by_id("synthetic_supply_chain_snapshot_2026_01_15")
    query_result = await query_facility_unavailable("facility:arkon-components:northbridge", storage, config)
    assert result["status"] == "error"
    assert commit["status"] == "failed"
    assert query_result["status"] == "inconsistent_storage"


@pytest.mark.asyncio
async def test_mcp_facility_query_routes_only_to_supply_chain_and_finance_stays_generic(tmp_path, monkeypatch):
    finance_config = build_global_config(
        working_dir=str(tmp_path),
        tokenizer=BasicTokenizer(),
        embedding_func=EmbeddingFunc(embedding_dim=8, max_token_size=8192, func=_fallback_embed, model_name="fallback"),
    )
    finance_storage = build_storage_instances(finance_config)
    await initialize_storage_instances(finance_storage)
    supply_storage = build_storage_instances(finance_config, workspace=SUPPLY_CHAIN_WORKSPACE)
    await initialize_storage_instances(supply_storage)
    assert (await ingest_extracted_json(fixture("expected_extraction.json"), supply_storage, finance_config))["status"] == "success"
    monkeypatch.setattr(server, "global_config", finance_config)
    monkeypatch.setattr(server, "storage_instances", finance_storage)
    monkeypatch.setattr(server, "workspace_storage_instances", {SUPPLY_CHAIN_WORKSPACE: supply_storage})

    mcp_result = await server.query_facility_unavailable_tool("facility:arkon-components:northbridge")
    finance_result = await query_facility_unavailable("facility:arkon-components:northbridge", finance_storage, finance_config)

    assert mcp_result["status"] == "success"
    assert finance_result["status"] == "profile_not_supported"
