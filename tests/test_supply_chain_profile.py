"""Stage-1 supply-chain workspace and profile-contract coverage."""

from __future__ import annotations

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.profiles import SUPPLY_CHAIN_WORKSPACE
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json
from preciso_mcp import server


async def _storage_stack(tmp_path, workspace: str = ""):
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
    storage = build_storage_instances(config, workspace=workspace)
    await initialize_storage_instances(storage)
    return storage, config


def supply_chain_payload() -> dict:
    return {
        "document_id": "supply_snapshot_2026_01_01",
        "chunks": [
            {"chunk_id": "company", "content": "Acme operates Chennai Plant."},
            {"chunk_id": "manufacturing", "content": "Chennai Plant manufactures C-17."},
            {"chunk_id": "bom", "content": "C-17 is used in AquaPump 300."},
        ],
        "entities": [
            {
                "entity_name": "company:acme",
                "entity_type": "COMPANY",
                "description": "Acme is the supplier company.",
                "source_id": "company",
            },
            {
                "entity_name": "facility:acme:chennai",
                "entity_type": "FACILITY",
                "description": "Acme's Chennai Plant.",
                "source_id": "company",
            },
            {
                "entity_name": "component:acme:c17",
                "entity_type": "COMPONENT",
                "description": "C-17 controller.",
                "source_id": "manufacturing",
            },
            {
                "entity_name": "product:aquapump:300",
                "entity_type": "PRODUCT",
                "description": "AquaPump 300.",
                "source_id": "bom",
            },
        ],
        "relationships": [
            {
                "src_id": "company:acme",
                "tgt_id": "facility:acme:chennai",
                "keywords": "OPERATES",
                "description": "Acme operates Chennai Plant.",
                "source_id": "company",
            },
            {
                "src_id": "facility:acme:chennai",
                "tgt_id": "component:acme:c17",
                "keywords": "MANUFACTURES",
                "description": "Chennai Plant manufactures C-17.",
                "source_id": "manufacturing",
            },
            {
                "src_id": "component:acme:c17",
                "tgt_id": "product:aquapump:300",
                "keywords": "USED_IN",
                "description": "C-17 is used in AquaPump 300.",
                "source_id": "bom",
            },
        ],
    }


@pytest.mark.asyncio
async def test_supply_chain_profile_accepts_only_valid_typed_dependencies(tmp_path):
    storage, config = await _storage_stack(tmp_path, SUPPLY_CHAIN_WORKSPACE)

    result = await ingest_extracted_json(supply_chain_payload(), storage, config)

    assert result["status"] == "success", result
    assert await storage["graph"].has_node("product:aquapump:300")
    assert (tmp_path / SUPPLY_CHAIN_WORKSPACE / "graph_graph.graphml").exists()
    assert (tmp_path / SUPPLY_CHAIN_WORKSPACE / "artifact_manifest.json").exists()


@pytest.mark.asyncio
async def test_supply_chain_profile_rejects_invalid_relationship_endpoint_types_before_writes(tmp_path):
    storage, config = await _storage_stack(tmp_path, SUPPLY_CHAIN_WORKSPACE)
    payload = supply_chain_payload()
    payload["relationships"][1]["src_id"] = "company:acme"

    result = await ingest_extracted_json(payload, storage, config)

    assert result["status"] == "validation_failed"
    assert any("requires `MANUFACTURES` to connect FACILITY -> COMPONENT" in error for error in result["errors"])
    assert await storage["graph"].get_all_nodes() == []
    assert await storage["text_chunks"].get_by_id("supply_snapshot_2026_01_01::company") is None


@pytest.mark.asyncio
async def test_supply_chain_profile_rejects_missing_or_unresolved_evidence_before_writes(tmp_path):
    storage, config = await _storage_stack(tmp_path, SUPPLY_CHAIN_WORKSPACE)
    payload = supply_chain_payload()
    payload["relationships"][2]["source_id"] = "missing_chunk"

    result = await ingest_extracted_json(payload, storage, config)

    assert result["status"] == "validation_failed"
    assert any("unresolvable source_id" in error for error in result["errors"])
    assert await storage["graph"].get_all_nodes() == []
    assert await storage["text_chunks"].get_by_id("supply_snapshot_2026_01_01::bom") is None


@pytest.mark.asyncio
async def test_supply_chain_workspace_isolated_and_default_finance_contract_is_unchanged(tmp_path):
    finance_storage, finance_config = await _storage_stack(tmp_path)
    supply_storage, supply_config = await _storage_stack(tmp_path, SUPPLY_CHAIN_WORKSPACE)
    finance_payload = {
        "document_id": "finance_doc",
        "chunks": [{"chunk_id": "facts", "content": "Apple reported revenue."}],
        "entities": [
            {
                "entity_name": "APPLE",
                "entity_type": "ORG",
                "description": "Apple is a technology company.",
                "source_id": "facts",
            }
        ],
        "relationships": [],
    }

    finance_result = await ingest_extracted_json(finance_payload, finance_storage, finance_config)
    supply_result = await ingest_extracted_json(supply_chain_payload(), supply_storage, supply_config)

    assert finance_result["status"] == "success", finance_result
    assert supply_result["status"] == "success", supply_result
    assert await finance_storage["graph"].has_node("APPLE")
    assert not await supply_storage["graph"].has_node("APPLE")
    assert await supply_storage["graph"].has_node("product:aquapump:300")
    assert not await finance_storage["graph"].has_node("product:aquapump:300")


@pytest.mark.asyncio
async def test_mcp_routes_supply_chain_ingestion_to_its_isolated_workspace(tmp_path, monkeypatch):
    finance_storage, config = await _storage_stack(tmp_path)
    monkeypatch.setattr(server, "global_config", config)
    monkeypatch.setattr(server, "storage_instances", finance_storage)
    monkeypatch.setattr(server, "workspace_storage_instances", {})

    result = await server.ingest_graph_tool(supply_chain_payload(), workspace=SUPPLY_CHAIN_WORKSPACE)

    assert result["status"] == "success", result
    supply_storage = server.workspace_storage_instances[SUPPLY_CHAIN_WORKSPACE]
    assert await supply_storage["graph"].has_node("product:aquapump:300")
    assert not await finance_storage["graph"].has_node("product:aquapump:300")
