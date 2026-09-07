from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.profiles import SUPPLY_CHAIN_WORKSPACE
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json
from preciso_mcp import server
from preciso_mcp.tools.ingest_from_file_tool import validate_extraction


def valid_payload() -> dict:
    return {
        "document_id": "validation_doc",
        "chunks": [
            {"chunk_id": "company", "content": "Acme operates Chennai Plant."},
            {"chunk_id": "manufacturing", "content": "Chennai Plant manufactures C-17."},
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
        ],
    }


async def supply_chain_storage(tmp_path: Path):
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


async def write_payload(tmp_path: Path, payload: dict, name: str = "extraction.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_valid_supply_chain_extraction_passes(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    path = await write_payload(tmp_path, valid_payload())

    result = await validate_extraction(str(path), storage, config)

    assert result == {
        "status": "valid",
        "workspace": SUPPLY_CHAIN_WORKSPACE,
        "profile": SUPPLY_CHAIN_WORKSPACE,
        "document_id": "validation_doc",
        "file_path": str(path),
        "counts": {"chunks": 2, "entities": 3, "relationships": 2},
        "errors": [],
        "warnings": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "mutate", "expected_error"),
    [
        (
            "invalid_entity_type",
            lambda payload: payload["entities"][0].update({"entity_type": "UNKNOWN"}),
            "rejects entity",
        ),
        (
            "invalid_relationship_direction",
            lambda payload: payload["relationships"][1].update(
                {"src_id": "company:acme"}
            ),
            "requires `MANUFACTURES` to connect FACILITY -> COMPONENT",
        ),
        (
            "unknown_entity",
            lambda payload: payload["relationships"][0].update(
                {"tgt_id": "facility:ghost"}
            ),
            "unknown entities",
        ),
        (
            "unresolved_source_id",
            lambda payload: payload["relationships"][1].update(
                {"source_id": "missing_chunk"}
            ),
            "unresolvable source_id",
        ),
        (
            "empty_chunk",
            lambda payload: payload["chunks"][1].update({"content": "  "}),
            "empty content",
        ),
    ],
)
async def test_invalid_supply_chain_extraction_fails(tmp_path, name, mutate, expected_error):
    storage, config = await supply_chain_storage(tmp_path)
    payload = valid_payload()
    mutate(payload)
    path = await write_payload(tmp_path, payload, f"{name}.json")

    result = await validate_extraction(str(path), storage, config)

    assert result["status"] == "validation_failed"
    assert any(expected_error in error for error in result["errors"]), result


@pytest.mark.asyncio
async def test_malformed_extraction_fails_without_preflight_reads_or_writes(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    path = await write_payload(
        tmp_path,
        {"document_id": "malformed", "entities": [], "relationships": []},
        "malformed.json",
    )
    filter_keys = AsyncMock(wraps=storage["text_chunks"].filter_keys)
    storage["text_chunks"].filter_keys = filter_keys

    result = await validate_extraction(str(path), storage, config)

    assert result["status"] == "validation_failed"
    assert any("missing required field `chunks`" in error for error in result["errors"])
    filter_keys.assert_not_awaited()
    assert await storage["graph"].get_all_nodes() == []
    assert await storage["text_chunks"].get_all_items() == {}


@pytest.mark.asyncio
async def test_repeated_validation_has_zero_graph_vector_or_chunk_writes(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    path = await write_payload(tmp_path, valid_payload())
    writes = []
    for name, item in storage.items():
        for method_name in (
            "upsert",
            "upsert_node",
            "upsert_edge",
            "upsert_nodes_batch",
            "upsert_edges_batch",
            "index_done_callback",
        ):
            if hasattr(item, method_name):
                original = getattr(item, method_name)

                async def record_write(
                    *args,
                    _name=name,
                    _method=method_name,
                    _original=original,
                    **kwargs,
                ):
                    writes.append((_name, _method))
                    return await _original(*args, **kwargs)

                setattr(item, method_name, record_write)

    first = await validate_extraction(str(path), storage, config)
    second = await validate_extraction(str(path), storage, config)

    assert first == second
    assert first["status"] == "valid"
    assert writes == []
    assert await storage["graph"].get_all_nodes() == []
    assert await storage["graph"].get_all_edges() == []
    assert await storage["text_chunks"].get_all_items() == {}


@pytest.mark.asyncio
async def test_validated_payload_uses_same_contract_as_ingestion(tmp_path):
    storage, config = await supply_chain_storage(tmp_path)
    payload = valid_payload()
    path = await write_payload(tmp_path, payload)

    validation = await validate_extraction(str(path), storage, config)
    ingestion = await ingest_extracted_json(copy.deepcopy(payload), storage, config)

    assert validation["status"] == "valid", validation
    assert ingestion["status"] == "success", ingestion
    assert ingestion["entities_merged"] == validation["counts"]["entities"]
    assert ingestion["relationships_merged"] == validation["counts"]["relationships"]


@pytest.mark.asyncio
async def test_mcp_tool_routes_supply_chain_workspace(tmp_path, monkeypatch):
    storage, config = await supply_chain_storage(tmp_path)
    path = await write_payload(tmp_path, valid_payload())
    monkeypatch.setattr(server, "global_config", config)
    monkeypatch.setattr(server, "workspace_storage_instances", {SUPPLY_CHAIN_WORKSPACE: storage})

    result = await server.validate_extraction(str(path), workspace=SUPPLY_CHAIN_WORKSPACE)

    assert result["status"] == "valid"
    assert result["workspace"] == SUPPLY_CHAIN_WORKSPACE
