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
        entity_chunks = storage_instances.get("entity_chunks")
        relation_chunks = storage_instances.get("relation_chunks")
        pending_summaries = storage_instances.get("pending_summaries")
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
        # Raw chunk id (as cited by the agent's source_id) -> the keys actually written.
        # An oversized chunk is stored as several `-pN` parts, so a citation to it has to
        # expand to every part or the evidence link dangles.
        chunk_id_map: dict[str, list[str]] = {}
        for idx, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                errors.append(f"chunk at index {idx} must be an object")
                continue
            raw_chunk_id = str(
                chunk.get("chunk_id")
                or compute_mdhash_id(f"{document_id}:{idx}:{chunk.get('content', '')}", prefix="chunk-")
            )
            # Namespace by document_id so chunk IDs stay globally unique even when
            # multiple extraction files reuse the same internal numbering (chunk_001, ...).
            chunk_id = f"{document_id}::{raw_chunk_id}"
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
                chunk_id_map.setdefault(raw_chunk_id, []).append(part_id)
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

        # Chunk ids the pipeline could not account for are not necessarily wrong: a
        # follow-up payload may cite chunks written by an earlier ingest of the same
        # document. Collect them first, then confirm against storage in one batch.
        warnings: list[str] = []
        strict_source_ids = os.getenv("GRAPHRAG_STRICT_SOURCE_IDS", "false").lower() == "true"
        candidate_unresolved: dict[str, list[str]] = defaultdict(list)

        known_entities: set[str] = set()
        pending_nodes: list[tuple[str, list[dict[str, Any]], list[str]]] = []
        for entity in entities:
            ok, reason = validate_entity(entity)
            if not ok:
                errors.append(reason)
                continue
            entity_name, node_list, unresolved = agent_json_to_nodes_data(
                entity, timestamp, document_id, chunk_id_map
            )
            pending_nodes.append((entity_name, node_list, unresolved))
            known_entities.add(entity_name)
            for raw_id in unresolved:
                candidate_unresolved[raw_id].append(f"entity `{entity_name}`")

        pending_edges: list[tuple[str, str, list[dict[str, Any]], list[str]]] = []
        for rel in relationships:
            ok, reason = validate_relationship(rel, known_entities)
            if not ok:
                errors.append(reason)
                continue
            src_id, tgt_id, edge_list, unresolved = agent_json_to_edges_data(
                rel, timestamp, document_id, chunk_id_map
            )
            pending_edges.append((src_id, tgt_id, edge_list, unresolved))
            for raw_id in unresolved:
                candidate_unresolved[raw_id].append(f"relationship `{src_id}->{tgt_id}`")

        dangling_ids: set[str] = set()
        if candidate_unresolved:
            missing = await text_chunks.filter_keys(
                {f"{document_id}::{raw_id}" for raw_id in candidate_unresolved}
            )
            dangling_ids = {
                raw_id for raw_id in candidate_unresolved if f"{document_id}::{raw_id}" in missing
            }

        for raw_id in sorted(dangling_ids):
            citers = candidate_unresolved[raw_id]
            message = (
                f"source_id `{raw_id}` does not match any chunk in this document "
                f"(cited by {', '.join(sorted(set(citers)))})"
            )
            if strict_source_ids:
                errors.append(message)
            else:
                warnings.append(message)
                logger.warning("ingest %s: %s", document_id, message)

        def _has_dangling(unresolved: list[str]) -> bool:
            return any(raw_id in dangling_ids for raw_id in unresolved)

        grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entity_name, node_list, unresolved in pending_nodes:
            if strict_source_ids and _has_dangling(unresolved):
                continue
            grouped_nodes[entity_name].extend(node_list)
        if strict_source_ids:
            # An entity survives if any of its occurrences cited a resolvable chunk, so
            # derive the surviving names from what actually made it through.
            known_entities = set(grouped_nodes)

        grouped_edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for src_id, tgt_id, edge_list, unresolved in pending_edges:
            if strict_source_ids and (
                _has_dangling(unresolved)
                or src_id not in known_entities
                or tgt_id not in known_entities
            ):
                continue
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
                    entity_chunks_storage=entity_chunks,
                    pending_summaries_storage=pending_summaries,
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
                    relation_chunks_storage=relation_chunks,
                    entity_chunks_storage=entity_chunks,
                    pending_summaries_storage=pending_summaries,
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
        if pending_summaries is not None:
            await pending_summaries.index_done_callback()
        await update_artifact_manifest(storage_instances, global_config)

        status = "success" if not errors else "partial_success"
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
        if warnings:
            result["warnings"] = warnings
        if pipeline_status.get("summary_events"):
            result["summary_events"] = pipeline_status["summary_events"]
        return result
    except Exception as exc:
        logger.exception("ingest_extracted_json failed (document_id=%s)", payload.get("document_id"))
        return {"status": "error", "message": str(exc)}
