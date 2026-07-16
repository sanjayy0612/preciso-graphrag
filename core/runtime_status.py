from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import Any

from core.utils import load_json, logger, write_json


def _resolve_working_dir(global_config: dict[str, Any]) -> Path:
    working_dir = global_config.get("working_dir") or "GRAPH_IS_HERE"
    return Path(working_dir).resolve()


def _detect_package(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _get_embedding_provider() -> str:
    return os.getenv("GRAPHRAG_EMBEDDING_PROVIDER", "ollama").lower().strip() or "ollama"


def _build_embedding_status(global_config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    embedding_func = global_config.get("embedding_func")
    provider = _get_embedding_provider()
    model = None
    dimension = None
    mode = "local"
    status = "active"

    if embedding_func is None:
        status = "offline"
        mode = "missing"
        warnings.append(
            "Embeddings are not configured; graph creation still works but vector search is unavailable."
        )
    else:
        model = getattr(embedding_func, "model_name", None) or "unknown"
        dimension = getattr(embedding_func, "embedding_dim", None)
        if provider == "fallback" or model == "fallback":
            mode = "fallback"
            status = "degraded"
            warnings.append(
                "Fallback embeddings are active; graph creation still works, but vector similarity quality is reduced."
            )
        elif provider in {"openai", "cohere"}:
            mode = "cloud"
        else:
            mode = "local"
        if provider == "openai":
            if not _detect_package("openai"):
                status = "degraded"
                warnings.append("OpenAI embeddings configured but the SDK is not installed.")
            if not (os.getenv("GRAPHRAG_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")):
                status = "degraded"
                warnings.append("OpenAI embeddings configured but API key is missing.")
        if provider == "cohere":
            if not _detect_package("cohere"):
                status = "degraded"
                warnings.append("Cohere embeddings configured but the SDK is not installed.")
            if not (os.getenv("GRAPHRAG_COHERE_API_KEY") or os.getenv("COHERE_API_KEY")):
                status = "degraded"
                warnings.append("Cohere embeddings configured but API key is missing.")
        if provider == "ollama":
            if not _detect_package("ollama"):
                status = "degraded"
                warnings.append("Ollama embeddings configured but the client is not installed.")

    return {
        "mode": mode,
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "status": status,
    }, warnings


def _build_llm_status(global_config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    llm_func = global_config.get("llm_model_func")
    configured = llm_func is not None
    status = "active" if configured else "inactive"
    provider = os.getenv("GRAPHRAG_LLM_PROVIDER", "none").strip() or "none"
    model = os.getenv("GRAPHRAG_LLM_MODEL", "").strip() or None

    if configured:
        if provider == "none":
            provider = getattr(llm_func, "__module__", "custom")
        if model is None:
            model = getattr(llm_func, "__name__", "custom")
    else:
        summary_mode = global_config.get("summary_mode", "agent")
        if summary_mode == "agent":
            # No LLM is expected here — summaries are deferred to the MCP-driving
            # agent, not skipped. Not a warning; see the pending_summaries count.
            pass
        else:
            warnings.append(
                "LLM summarization is not configured; extraction and graph creation still work, but summary generation is skipped."
            )

    return {
        "configured": configured,
        "provider": provider,
        "model": model,
        "status": status,
    }, warnings


async def _get_graph_counts(storage_instances: dict[str, Any]) -> dict[str, int]:
    graph_storage = storage_instances.get("graph")
    if graph_storage is None:
        return {"entities": 0, "relationships": 0}
    graph = await graph_storage._get_graph()
    return {
        "entities": int(graph.number_of_nodes()),
        "relationships": int(graph.number_of_edges()),
    }


async def _get_text_chunk_stats(storage_instances: dict[str, Any]) -> dict[str, int]:
    text_chunks = storage_instances.get("text_chunks")
    if text_chunks is None:
        return {"chunks": 0, "documents": 0}
    async with text_chunks._storage_lock:
        values = list(text_chunks._data.values())
    chunk_count = len(values)
    doc_ids = {item.get("full_doc_id") for item in values if isinstance(item, dict)}
    doc_count = len({doc_id for doc_id in doc_ids if doc_id})
    return {"chunks": chunk_count, "documents": doc_count}


async def _get_pending_summary_count(storage_instances: dict[str, Any]) -> int:
    pending_summaries = storage_instances.get("pending_summaries")
    if pending_summaries is None:
        return 0
    async with pending_summaries._storage_lock:
        return len(pending_summaries._data)


async def build_runtime_status(
    storage_instances: dict[str, Any],
    global_config: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    warnings.extend(global_config.get("runtime_warnings", []))
    embedding_info, embedding_warnings = _build_embedding_status(global_config)
    warnings.extend(embedding_warnings)
    llm_info, llm_warnings = _build_llm_status(global_config)
    warnings.extend(llm_warnings)

    counts = await _get_graph_counts(storage_instances)
    chunk_stats = await _get_text_chunk_stats(storage_instances)
    pending_summary_count = await _get_pending_summary_count(storage_instances)
    working_dir = _resolve_working_dir(global_config)

    overall = "ready" if not warnings else "degraded"
    return {
        "overall": overall,
        "warnings": warnings,
        "embedding": embedding_info,
        "graph": {
            "storage": "networkx",
            "location": str(working_dir),
            "graph_file": str(working_dir / "graph_graph.graphml"),
            "entities": counts["entities"],
            "relationships": counts["relationships"],
            "documents_ingested": chunk_stats["documents"],
            "chunks": chunk_stats["chunks"],
        },
        "llm": llm_info,
        # Informational, not a degraded-state signal: count of entities/relations
        # deferred to the agent in summary_mode="agent". See pending_summaries_tool.py.
        "pending_summaries": pending_summary_count,
        "updated_at": int(time.time()),
    }


async def update_artifact_manifest(
    storage_instances: dict[str, Any],
    global_config: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        status = await build_runtime_status(storage_instances, global_config)
        working_dir = _resolve_working_dir(global_config)
        manifest_path = working_dir / "artifact_manifest.json"
        existing = load_json(str(manifest_path)) or {}
        now = int(time.time())
        manifest = {
            "working_dir": str(working_dir),
            "graph_storage_type": status["graph"]["storage"],
            "graph_file": status["graph"]["graph_file"],
            "embedding": {
                "provider": status["embedding"]["provider"],
                "model": status["embedding"]["model"],
                "dimension": status["embedding"]["dimension"],
                "fallback": status["embedding"]["mode"] == "fallback",
            },
            "llm": {
                "configured": status["llm"]["configured"],
                "provider": status["llm"]["provider"],
                "model": status["llm"]["model"],
            },
            "document_count": status["graph"]["documents_ingested"],
            "entity_count": status["graph"]["entities"],
            "relationship_count": status["graph"]["relationships"],
            "chunk_count": status["graph"]["chunks"],
            "overall_status": status["overall"],
            "warnings": status["warnings"],
            "generated_at": existing.get("generated_at", now),
            "updated_at": now,
        }
        write_json(manifest, str(manifest_path))
        return manifest
    except Exception as exc:
        logger.warning("Failed to update artifact manifest: %s", exc)
        return None
