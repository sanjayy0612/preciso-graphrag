from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from core.runtime_status import update_artifact_manifest
from core.storage.shared_storage import get_storage_keyed_lock
from core.utils import compute_mdhash_id, logger, safe_vdb_operation_with_exception
from ingest.transformer import agent_json_to_edges_data, agent_json_to_nodes_data
from ingest.validator import validate_entity, validate_relationship


async def ingest_extracted_json(payload, storage_instances, global_config) -> dict:
    try:
        if not isinstance(payload, dict):
            return {"status": "error", "message": "payload must be an object"}
        document_id = str(
            payload.get("document_id")
            or payload.get("file_path")
            or compute_mdhash_id(str(payload), prefix="doc-")
        )
        file_path = str(payload.get("file_path", "unknown_source"))
        timestamp = int(payload.get("timestamp", time.time()))
        chunks = payload.get("chunks", []) or []
        entities = payload.get("entities", []) or []
        relationships = payload.get("relationships", []) or []
        text_chunks = storage_instances["text_chunks"]
        chunks_vdb = storage_instances["chunks_vdb"]
        graph = storage_instances["graph"]
        entities_vdb = storage_instances["entities_vdb"]
        relationships_vdb = storage_instances["relationships_vdb"]
        llm_cache = storage_instances.get("llm_cache")
        entity_chunks = storage_instances.get("entity_chunks")
        relation_chunks = storage_instances.get("relation_chunks")
        errors: list[str] = []

        max_chunk_tokens = global_config.get("embedding_token_limit")
        if max_chunk_tokens is None:
            max_chunk_tokens = int(os.getenv("GRAPHRAG_CHUNK_TOKEN_LIMIT", "0"))
        max_chunk_chars = int(os.getenv("GRAPHRAG_CHUNK_CHAR_LIMIT", "800"))
        overlap_tokens = int(os.getenv("GRAPHRAG_CHUNK_TOKEN_OVERLAP", "0"))
        overlap_chars = int(os.getenv("GRAPHRAG_CHUNK_CHAR_OVERLAP", "50"))

        def split_chunk_content(content: str) -> list[str]:
            if not content:
                return []
            tokenizer = global_config.get("tokenizer")
            if max_chunk_tokens and getattr(tokenizer, "_encoding", None) is not None:
                tokens = tokenizer.encode(content)
                if len(tokens) <= max_chunk_tokens:
                    return [content]
                step = max(1, max_chunk_tokens - max(0, overlap_tokens))
                parts = []
                start = 0
                while start < len(tokens):
                    end = min(start + max_chunk_tokens, len(tokens))
                    part = tokenizer.decode(tokens[start:end])
                    if part:
                        parts.append(part)
                    start += step
                if parts:
                    return parts
            if max_chunk_chars and len(content) > max_chunk_chars:
                step = max(1, max_chunk_chars - max(0, overlap_chars))
                parts = []
                start = 0
                while start < len(content):
                    end = min(start + max_chunk_chars, len(content))
                    parts.append(content[start:end])
                    start += step
                return parts
            return [content]

        chunk_upserts = {}
        chunk_vdb_upserts = {}
        for idx, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                errors.append(f"chunk at index {idx} must be an object")
                continue
            chunk_id = str(
                chunk.get("chunk_id")
                or compute_mdhash_id(f"{document_id}:{idx}:{chunk.get('content', '')}", prefix="chunk-")
            )
            content = str(chunk.get("content", "")).strip()
            if not content:
                errors.append(f"chunk `{chunk_id}` has empty content")
                continue
            parts = split_chunk_content(content)
            if not parts:
                errors.append(f"chunk `{chunk_id}` has empty content after splitting")
                continue
            base_order_index = int(chunk.get("chunk_order_index", idx))
            for part_index, part_content in enumerate(parts):
                part_id = chunk_id if len(parts) == 1 else f"{chunk_id}-p{part_index + 1}"
                part_order_index = (
                    base_order_index if len(parts) == 1 else base_order_index * 1000 + part_index
                )
                chunk_record = {
                    "tokens": chunk.get("tokens") or len(global_config["tokenizer"].encode(part_content)),
                    "content": part_content,
                    "full_doc_id": document_id,
                    "chunk_order_index": part_order_index,
                    "file_path": str(chunk.get("file_path", file_path)) or file_path,
                }
                chunk_upserts[part_id] = chunk_record
                chunk_vdb_upserts[compute_mdhash_id(part_id, prefix="vchunk-")] = {
                    "content": part_content,
                    "full_doc_id": document_id,
                    "file_path": chunk_record["file_path"],
                    "chunk_id": part_id,
                }
        if chunk_upserts:
            await text_chunks.upsert(chunk_upserts)
            await safe_vdb_operation_with_exception(
                operation=lambda payload=chunk_vdb_upserts: chunks_vdb.upsert(payload),
                operation_name="chunk_upsert",
                entity_name=document_id,
            )

        known_entities: set[str] = set()
        grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entity in entities:
            ok, reason = validate_entity(entity)
            if not ok:
                errors.append(reason)
                continue
            entity_name, node_list = agent_json_to_nodes_data(entity, timestamp)
            grouped_nodes[entity_name].extend(node_list)
            known_entities.add(entity_name)

        grouped_edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for rel in relationships:
            ok, reason = validate_relationship(rel, known_entities)
            if not ok:
                errors.append(reason)
                continue
            src_id, tgt_id, edge_list = agent_json_to_edges_data(rel, timestamp)
            grouped_edges[(src_id, tgt_id)].extend(edge_list)

        pipeline_status = {"summary_events": []}
        merged_nodes = []
        for entity_name, node_list in grouped_nodes.items():
            async with get_storage_keyed_lock(f"node:{entity_name}"):
                node_data = await _merge_nodes_then_upsert(
                    entity_name=entity_name,
                    nodes_data=node_list,
                    knowledge_graph_inst=graph,
                    entity_vdb=entities_vdb,
                    global_config=global_config,
                    pipeline_status=pipeline_status,
                    llm_response_cache=llm_cache,
                    entity_chunks_storage=entity_chunks,
                )
                if node_data is not None:
                    merged_nodes.append(node_data)

        merged_edges = []
        for (src_id, tgt_id), edge_list in grouped_edges.items():
            lock_key = f"edge:{':'.join(sorted((src_id, tgt_id)))}"
            async with get_storage_keyed_lock(lock_key):
                edge_data = await _merge_edges_then_upsert(
                    src_id=src_id,
                    tgt_id=tgt_id,
                    edges_data=edge_list,
                    knowledge_graph_inst=graph,
                    relationships_vdb=relationships_vdb,
                    entity_vdb=entities_vdb,
                    global_config=global_config,
                    pipeline_status=pipeline_status,
                    llm_response_cache=llm_cache,
                    relation_chunks_storage=relation_chunks,
                    entity_chunks_storage=entity_chunks,
                )
                if edge_data is not None:
                    merged_edges.append(edge_data)

        await text_chunks.index_done_callback()
        await chunks_vdb.index_done_callback()
        await graph.index_done_callback()
        await entities_vdb.index_done_callback()
        await relationships_vdb.index_done_callback()
        if entity_chunks is not None:
            await entity_chunks.index_done_callback()
        if relation_chunks is not None:
            await relation_chunks.index_done_callback()
        await update_artifact_manifest(storage_instances, global_config)

        status = "success" if not errors else "partial_success"
        if (
            pipeline_status.get("summary_events")
            and global_config.get("llm_model_func") is None
            and any(
                event.get("reason") == "summary_required"
                for event in pipeline_status.get("summary_events", [])
            )
        ):
            status = "summary_required"
        result = {
            "status": status,
            "message": f"Ingested document `{document_id}`",
            "document_id": document_id,
            "file_path": file_path,
            "chunks_ingested": len(chunk_upserts),
            "entities_merged": len(merged_nodes),
            "relationships_merged": len(merged_edges),
            "errors": errors,
        }
        if status == "summary_required":
            result["message"] = (
                f"Summary required for `{document_id}`. One or more descriptions exceed "
                "token limits and no LLM is configured. Provide summarized descriptions and reingest."
            )
        if pipeline_status.get("summary_events"):
            result["summary_events"] = pipeline_status["summary_events"]
        return result
    except Exception as exc:
        logger.exception("ingest_extracted_json failed (document_id=%s)", payload.get("document_id"))
        return {"status": "error", "message": str(exc)}
