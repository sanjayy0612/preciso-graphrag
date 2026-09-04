"""Validate Stage-2 synthetic fixtures without implementing traversal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.profiles import SUPPLY_CHAIN_WORKSPACE
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "supply_chain"


def load_fixture(relative_path: str) -> dict:
    return json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))


async def supply_chain_storage(tmp_path):
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


@pytest.mark.asyncio
async def test_synthetic_supply_chain_extraction_fixture_passes_strict_profile(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    payload = load_fixture("expected_extraction.json")

    result = await ingest_extracted_json(payload, storage, config)

    assert result["status"] == "success", result
    assert result["entities_merged"] == 11
    assert result["relationships_merged"] == 10
    assert result["warnings"] == []


def test_ground_truth_matches_fixture_facts_and_evidence_references():
    payload = load_fixture("expected_extraction.json")
    ground_truth = load_fixture("ground_truth.json")
    chunk_ids = {chunk["chunk_id"] for chunk in payload["chunks"]}
    entity_ids = {entity["entity_name"] for entity in payload["entities"]}
    relationship_facts = {
        (relationship["src_id"], relationship["keywords"].split(",", 1)[0], relationship["tgt_id"]): relationship
        for relationship in payload["relationships"]
    }

    assert set(ground_truth["entities"]) == entity_ids
    for expected in ground_truth["relationships"]:
        key = (expected["src_id"], expected["type"], expected["tgt_id"])
        assert key in relationship_facts
        assert expected["evidence"] in chunk_ids
        assert relationship_facts[key]["source_id"] == expected["evidence"]

    # These are test expectations for the future path query, not a traversal.
    for expected_path in ground_truth["expected_facility_to_product_paths"]:
        assert expected_path["facility"] in entity_ids
        assert set(expected_path["products"]).isdisjoint(expected_path["must_not_include"])
        assert set(expected_path["products"]).issubset(entity_ids)


def test_registry_keeps_ambiguous_identity_unresolved_and_uses_only_documented_aliases():
    registry = load_fixture("canonical_id_registry.json")
    payload = load_fixture("expected_extraction.json")
    aliases = [
        alias["alias"]
        for entity in registry["entities"]
        for alias in entity["documented_aliases"]
    ]
    ambiguous_alias = registry["ambiguous_aliases"][0]

    assert "Northbridge Site" in aliases
    assert ambiguous_alias == {
        "alias": "Plant 7",
        "status": "unresolved",
        "policy": "do_not_merge",
        "evidence": "sources/identity_notice.md#identity_001",
    }
    assert ambiguous_alias["alias"] not in aliases
    assert all("plant-7" not in entity["entity_name"].lower() for entity in payload["entities"])
    for entity in registry["entities"]:
        for alias in entity["documented_aliases"]:
            relative_path, chunk_id = alias["evidence"].split("#", 1)
            assert (FIXTURE_ROOT / relative_path).is_file()
            assert chunk_id in {chunk["chunk_id"] for chunk in payload["chunks"]}
    relative_path, _ = ambiguous_alias["evidence"].split("#", 1)
    assert (FIXTURE_ROOT / relative_path).is_file()


@pytest.mark.asyncio
async def test_intentionally_invalid_dependency_fixture_is_rejected_by_strict_profile(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    payload = load_fixture("invalid/unsupported_dependency.json")

    result = await ingest_extracted_json(payload, storage, config)

    assert result["status"] == "validation_failed"
    assert any("requires `USED_IN` to connect COMPONENT -> PRODUCT" in error for error in result["errors"])
    assert await storage["graph"].get_all_nodes() == []
