from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

from config import GRAPH_FIELD_SEP
from core.merge import _merge_edges_then_upsert, _merge_nodes_then_upsert
from core.runtime_status import update_artifact_manifest
from core.session_lock import ingestion_session_lock
from core.storage.shared_storage import get_storage_keyed_lock
from core.utils import compute_mdhash_id, logger, safe_vdb_operation_with_exception
from ingest.transformer import (
    agent_json_to_edges_data,
    agent_json_to_nodes_data,
    namespace_source_id,
)
from ingest.validator import validate_entity, validate_relationship


async def ingest_extracted_json(payload, storage_instances, global_config) -> dict:
    if not isinstance(payload, dict):
        return {"status": "error", "message": "payload must be an object"}
    workspace = getattr(storage_instances.get("graph"), "workspace", "")
    async with ingestion_session_lock(global_config["working_dir"], workspace):
        return await _ingest_extracted_json(payload, storage_instances, global_config)


async def _ingest_extracted_json(payload, storage_instances, global_config) -> dict:
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
        warnings: list[str] = []
        strict_source_ids = os.getenv("GRAPHRAG_STRICT_SOURCE_IDS", "false").strip().lower() == "true"

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
        chunk_part_ids: dict[str, list[str]] = {}
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
                chunk_part_ids.setdefault(raw_chunk_id, []).append(part_id)
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
            await safe_vdb_operation_with_exception(
                operation=lambda payload=chunk_vdb_upserts: chunks_vdb.upsert(payload),
                operation_name="chunk_upsert",
                entity_name=document_id,
            )
            indexed_vectors = await chunks_vdb.get_vectors_by_ids(list(chunk_vdb_upserts))
            missing_vector_ids = [
                vector_id for vector_id in chunk_vdb_upserts if vector_id not in indexed_vectors
            ]
            if missing_vector_ids:
                raise RuntimeError(
                    f"chunk vector integrity check failed for document `{document_id}`: "
                    f"{len(missing_vector_ids)} vector(s) missing after upsert"
                )
            # Persist chunk text only after its vector is known to exist. This
            # prevents graph evidence from citing text that cannot be ranked.
            await text_chunks.upsert(chunk_upserts)

        def normalize_source_id(record: dict) -> dict:
            normalized = dict(record)
            normalized["source_id"] = namespace_source_id(
                str(record.get("source_id", "")).strip(),
                document_id,
                chunk_part_ids,
            )
            return normalized

        normalized_entities = [normalize_source_id(entity) if isinstance(entity, dict) else entity for entity in entities]
        normalized_relationships = [
            normalize_source_id(relationship) if isinstance(relationship, dict) else relationship
            for relationship in relationships
        ]
        cited_chunk_ids = {
            chunk_id
            for record in [*normalized_entities, *normalized_relationships]
            if isinstance(record, dict)
            for chunk_id in str(record.get("source_id", "")).split(GRAPH_FIELD_SEP)
            if chunk_id
        }
        unresolved_chunk_ids = await text_chunks.filter_keys(cited_chunk_ids)
        resolvable_chunk_ids = cited_chunk_ids - unresolved_chunk_ids

        def unresolved_source_ids(record: dict) -> list[str]:
            return [
                chunk_id
                for chunk_id in str(record.get("source_id", "")).split(GRAPH_FIELD_SEP)
                if chunk_id in unresolved_chunk_ids
            ]

        known_entities: set[str] = set()
        grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entity in normalized_entities:
            ok, reason = validate_entity(
                entity,
                resolvable_source_ids=resolvable_chunk_ids,
                strict_source_ids=strict_source_ids,
            )
            if not ok:
                errors.append(reason)
                continue
            dangling = unresolved_source_ids(entity)
            if dangling:
                warnings.append(
                    f"entity `{entity['entity_name']}` has unresolvable source_id(s): {', '.join(dangling)}"
                )
            entity_name, node_list = agent_json_to_nodes_data(
                entity,
                timestamp,
                document_id,
                source_id_is_namespaced=True,
            )
            grouped_nodes[entity_name].extend(node_list)
            known_entities.add(entity_name)

        grouped_edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for rel in normalized_relationships:
            ok, reason = validate_relationship(
                rel,
                known_entities,
                resolvable_source_ids=resolvable_chunk_ids,
                strict_source_ids=strict_source_ids,
            )
            if not ok:
                errors.append(reason)
                continue
            dangling = unresolved_source_ids(rel)
            if dangling:
                warnings.append(
                    f"relationship `{rel.get('src_id') or rel.get('source_entity')}->{rel.get('tgt_id') or rel.get('target_entity')}` "
                    f"has unresolvable source_id(s): {', '.join(dangling)}"
                )
            src_id, tgt_id, edge_list = agent_json_to_edges_data(
                rel,
                timestamp,
                document_id,
                source_id_is_namespaced=True,
            )
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
            "warnings": warnings,
        }
        if pipeline_status.get("summary_events"):
            result["summary_events"] = pipeline_status["summary_events"]
        return result
    except Exception as exc:
        logger.exception("ingest_extracted_json failed (document_id=%s)", payload.get("document_id"))
        return {"status": "error", "message": str(exc)}
