from __future__ import annotations

import pytest

import config
from core.runtime_status import build_runtime_status
from core.storage.base import EmbeddingFunc
from preciso_mcp import server


@pytest.mark.asyncio
async def test_status_is_degraded_after_embedding_probe_failure(monkeypatch, tmp_path):
    async def unavailable_embedder(texts, **kwargs):
        raise ConnectionError("embedding provider unavailable")

    monkeypatch.setattr(config, "DEFAULT_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(config, "_ollama_embed", unavailable_embedder)

    embedding_func = config.build_default_embedding_func()
    status = await build_runtime_status(
        {},
        {"working_dir": str(tmp_path), "embedding_func": embedding_func},
    )

    assert status["overall"] == "degraded"
    assert status["embedding"]["mode"] == "unavailable"
    assert status["embedding"]["status"] == "degraded"
    assert any("embedding probe failed" in warning for warning in status["warnings"])


@pytest.mark.asyncio
async def test_status_tracks_runtime_embedding_failure_and_recovery(monkeypatch, tmp_path):
    provider_available = False

    async def runtime_embedder(texts, **kwargs):
        if not provider_available:
            raise ConnectionError("provider stopped")
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setenv("GRAPHRAG_EMBEDDING_PROVIDER", "ollama")
    embedding_func = EmbeddingFunc(
        embedding_dim=2,
        max_token_size=128,
        func=runtime_embedder,
        model_name="runtime-test",
    )

    with pytest.raises(ConnectionError, match="provider stopped"):
        await embedding_func(["test"])
    failed_status = await build_runtime_status(
        {}, {"working_dir": str(tmp_path), "embedding_func": embedding_func}
    )
    assert failed_status["overall"] == "degraded"
    assert failed_status["embedding"]["mode"] == "unavailable"

    provider_available = True
    await embedding_func(["test"])
    recovered_status = await build_runtime_status(
        {}, {"working_dir": str(tmp_path), "embedding_func": embedding_func}
    )
    assert recovered_status["overall"] == "ready"
    assert recovered_status["embedding"]["status"] == "active"


@pytest.mark.asyncio
async def test_inline_ingestion_rejects_structurally_invalid_payload(
    monkeypatch, storage_stack
):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setattr(server, "storage_instances", storage_instances)
    monkeypatch.setattr(server, "global_config", global_config)

    result = await server.ingest_graph_tool({"document_id": "invalid", "entities": []})

    assert result["status"] == "validation_failed"
    assert "missing required field `relationships`" in result["errors"]
    assert "missing required field `chunks`" in result["errors"]


@pytest.mark.asyncio
async def test_inline_ingestion_rejects_semantically_invalid_payload(
    monkeypatch, storage_stack
):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setattr(server, "storage_instances", storage_instances)
    monkeypatch.setattr(server, "global_config", global_config)

    payload = {
        "document_id": "invalid-entity",
        "entities": [
            {
                "entity_name": "Acme",
                "entity_type": "COMPANY",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
        "chunks": [{"chunk_id": "chunk-1", "content": "Acme."}],
    }

    result = await server.ingest_graph_tool(payload)

    assert result["status"] == "validation_failed"
    assert any("requires description" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_inline_ingestion_preserves_valid_payload_behavior(monkeypatch, storage_stack):
    storage_instances, global_config, _ = storage_stack
    monkeypatch.setattr(server, "storage_instances", storage_instances)
    monkeypatch.setattr(server, "global_config", global_config)

    payload = {
        "document_id": "valid-inline",
        "entities": [
            {
                "entity_name": "Acme",
                "entity_type": "COMPANY",
                "description": "Acme makes widgets.",
                "source_id": "chunk-1",
            }
        ],
        "relationships": [],
        "chunks": [{"chunk_id": "chunk-1", "content": "Acme makes widgets."}],
    }

    result = await server.ingest_graph_tool(payload)

    assert result["status"] == "success"
    assert result["entities_merged"] == 1
    assert result["chunks_ingested"] == 1
