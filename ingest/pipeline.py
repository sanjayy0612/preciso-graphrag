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
from core.storage.vector_write_batch import VectorWriteBatch
from core.utils import (
    compute_mdhash_id,
    logger,
    make_relation_chunk_key,
    safe_vdb_operation_with_exception,
    split_source_ids,
)
from ingest.transformer import (
    agent_json_to_edges_data,
    agent_json_to_nodes_data,
    namespace_source_id,
)
from ingest.validator import (
    validate_entity,
    validate_extraction_structure,
    validate_relationship,
)
from core.profiles import (
    GENERIC_PROFILE,
    SUPPLY_CHAIN_PROFILE,
    resolve_dataset_profile,
    validate_profile_records,
)
from core.supply_chain import (
    begin_supply_chain_commit,
    fail_supply_chain_commit,
    finish_supply_chain_commit,
    persist_directed_relationships,
    persist_snapshot_metadata,
    validate_snapshot_metadata,
)


def _new_source_ids(records: list[dict[str, Any]]) -> set[str]:
    return set(split_source_ids(record.get("source_id", "") for record in records))


async def _classify_graph_inputs(
    graph,
    entity_chunks,
    relation_chunks,
    grouped_nodes: dict[str, list[dict[str, Any]]],
    grouped_edges: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Classify graph inputs without changing the legacy processed totals."""
    entity_counts = {"added": 0, "merged": 0, "skipped_duplicate": 0}
    relationship_counts = {"added": 0, "merged": 0, "skipped_duplicate": 0}

    existing_nodes = await graph.get_nodes_batch(list(grouped_nodes))
    entity_chunk_rows = (
        await entity_chunks.get_by_ids(list(grouped_nodes)) if entity_chunks is not None else []
    )
    for index, (entity_name, records) in enumerate(grouped_nodes.items()):
        existing_node = existing_nodes.get(entity_name)
        if existing_node is None:
            entity_counts["added"] += 1
            continue
        stored = entity_chunk_rows[index] if entity_chunk_rows else None
        prior_sources = set(
            split_source_ids(
                (stored or {}).get("chunk_ids", [])
                or (existing_node.get("source_id", ""),)
            )
        )
        if _new_source_ids(records).issubset(prior_sources):
            entity_counts["skipped_duplicate"] += 1
        else:
            entity_counts["merged"] += 1

    edge_pairs = [{"src": src, "tgt": tgt} for src, tgt in grouped_edges]
    existing_edges = await graph.get_edges_batch(edge_pairs)
    relation_keys = [make_relation_chunk_key(src, tgt) for src, tgt in grouped_edges]
    relation_chunk_rows = (
        await relation_chunks.get_by_ids(relation_keys) if relation_chunks is not None else []
    )
    for index, (edge, records) in enumerate(grouped_edges.items()):
        existing_edge = existing_edges.get(edge)
        if existing_edge is None:
            relationship_counts["added"] += 1
            continue
        stored = relation_chunk_rows[index] if relation_chunk_rows else None
        prior_sources = set(
            split_source_ids(
                (stored or {}).get("chunk_ids", [])
                or (existing_edge.get("source_id", ""),)
            )
        )
        if _new_source_ids(records).issubset(prior_sources):
            relationship_counts["skipped_duplicate"] += 1
        else:
            relationship_counts["merged"] += 1

    return entity_counts, relationship_counts


def _classify_chunk_inputs(
    chunk_upserts: dict[str, dict[str, Any]],
    existing_chunks: list[dict[str, Any] | None],
) -> dict[str, int]:
    counts = {"added": 0, "merged": 0, "skipped_duplicate": 0}
    stable_fields = ("content", "full_doc_id", "chunk_order_index", "file_path")
    for record, existing in zip(chunk_upserts.values(), existing_chunks):
        if existing is None:
            counts["added"] += 1
        elif all(existing.get(field) == record.get(field) for field in stable_fields):
            counts["skipped_duplicate"] += 1
        else:
            counts["merged"] += 1
    return counts


async def preflight_extraction(payload, storage_instances, global_config) -> dict[str, Any]:
    """Build the exact non-mutating validation view used by ingestion.

    This function deliberately performs only validation and reads.  It prepares
    the namespaced chunk/source IDs and the records that ingestion may safely
    transform, but it never calls an upsert, callback, or manifest writer.

    Generic workspaces retain their historical partial-success behavior for
    invalid individual records.  Strict profiles, such as ``supply_chain``,
    reject the complete payload before any artifact is written.
    """
    structure_errors = validate_extraction_structure(payload)
    document_id = str(
        payload.get("document_id")
        or payload.get("file_path")
        or compute_mdhash_id(str(payload), prefix="doc-")
    ) if isinstance(payload, dict) else None
    file_path = str(payload.get("file_path", "unknown_source")) if isinstance(payload, dict) else "unknown_source"
    counts = {
        "chunks": len(payload.get("chunks", [])) if isinstance(payload, dict) and isinstance(payload.get("chunks", []), list) else 0,
        "entities": len(payload.get("entities", [])) if isinstance(payload, dict) and isinstance(payload.get("entities", []), list) else 0,
        "relationships": len(payload.get("relationships", [])) if isinstance(payload, dict) and isinstance(payload.get("relationships", []), list) else 0,
    }

    graph = storage_instances.get("graph")
    workspace = getattr(graph, "workspace", "")
    profile = resolve_dataset_profile(global_config, workspace)
    result: dict[str, Any] = {
        "document_id": document_id,
        "file_path": file_path,
        "workspace": workspace or None,
        "profile": profile,
        "counts": counts,
        "errors": list(structure_errors),
        "warnings": [],
        "fatal_errors": list(structure_errors),
        "record_errors": [],
        "normalized_entities": [],
        "normalized_relationships": [],
        "valid_entities": [],
        "valid_relationships": [],
        "chunk_upserts": {},
        "chunk_vdb_upserts": {},
        "unresolved_chunk_ids": set(),
        "unresolvable_source_ids": set(),
        "resolvable_source_ids": set(),
    }
    if structure_errors:
        return result

    chunks = payload["chunks"]
    entities = payload["entities"]
    relationships = payload["relationships"]
    tokenizer = global_config.get("tokenizer")
    max_chunk_tokens = global_config.get("embedding_token_limit")
    if max_chunk_tokens is None:
        max_chunk_tokens = int(os.getenv("GRAPHRAG_CHUNK_TOKEN_LIMIT", "0"))
    max_chunk_chars = int(os.getenv("GRAPHRAG_CHUNK_CHAR_LIMIT", "800"))
    overlap_tokens = int(os.getenv("GRAPHRAG_CHUNK_TOKEN_OVERLAP", "0"))
    overlap_chars = int(os.getenv("GRAPHRAG_CHUNK_CHAR_OVERLAP", "50"))

    def split_chunk_content(content: str) -> list[str]:
        if not content:
            return []
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

    chunk_upserts: dict[str, dict[str, Any]] = {}
    chunk_vdb_upserts: dict[str, dict[str, Any]] = {}
    chunk_part_ids: dict[str, list[str]] = {}
    chunk_errors: list[str] = []
    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            chunk_errors.append(f"chunk at index {idx} must be an object")
            continue
        raw_chunk_id = str(
            chunk.get("chunk_id")
            or compute_mdhash_id(f"{document_id}:{idx}:{chunk.get('content', '')}", prefix="chunk-")
        )
        chunk_id = f"{document_id}::{raw_chunk_id}"
        content = str(chunk.get("content", "")).strip()
        if not content:
            chunk_errors.append(f"chunk `{chunk_id}` has empty content")
            continue
        parts = split_chunk_content(content)
        if not parts:
            chunk_errors.append(f"chunk `{chunk_id}` has empty content after splitting")
            continue
        try:
            base_order_index = int(chunk.get("chunk_order_index", idx))
        except (TypeError, ValueError):
            chunk_errors.append(f"chunk `{chunk_id}` has invalid chunk_order_index")
            continue
        for part_index, part_content in enumerate(parts):
            part_id = chunk_id if len(parts) == 1 else f"{chunk_id}-p{part_index + 1}"
            chunk_part_ids.setdefault(raw_chunk_id, []).append(part_id)
            part_order_index = (
                base_order_index if len(parts) == 1 else base_order_index * 1000 + part_index
            )
            token_count = len(tokenizer.encode(part_content)) if tokenizer is not None else 0
            chunk_record = {
                "tokens": chunk.get("tokens") or token_count,
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

    def normalize_source_id(record: dict) -> dict:
        normalized = dict(record)
        normalized["source_id"] = namespace_source_id(
            str(record.get("source_id", "")).strip(),
            document_id,
            chunk_part_ids,
        )
        return normalized

    normalized_entities = [
        normalize_source_id(entity) if isinstance(entity, dict) else entity
        for entity in entities
    ]
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
    text_chunks = storage_instances.get("text_chunks")
    unresolved_chunk_ids = set()
    empty_evidence_ids: set[str] = set()
    if cited_chunk_ids and text_chunks is not None:
        unresolved_chunk_ids = await text_chunks.filter_keys(cited_chunk_ids - set(chunk_upserts))
        historical_ids = sorted(cited_chunk_ids - set(chunk_upserts) - unresolved_chunk_ids)
        historical_chunks = await text_chunks.get_by_ids(historical_ids)
        empty_evidence_ids = {
            source_id
            for source_id, chunk in zip(historical_ids, historical_chunks)
            if not isinstance(chunk, dict) or not str(chunk.get("content", "")).strip()
        }
    elif cited_chunk_ids:
        unresolved_chunk_ids = set(cited_chunk_ids - set(chunk_upserts))

    unresolvable_source_ids = unresolved_chunk_ids | empty_evidence_ids
    resolvable_source_ids = cited_chunk_ids - unresolvable_source_ids
    profile_errors = validate_profile_records(
        profile,
        normalized_entities,
        normalized_relationships,
        resolvable_source_ids=resolvable_source_ids,
    )
    if profile.name == SUPPLY_CHAIN_PROFILE:
        directed_relationships = storage_instances.get("directed_relationships")
        supply_chain_metadata = storage_instances.get("supply_chain_metadata")
        supply_chain_commits = storage_instances.get("supply_chain_commits")
        if not all((directed_relationships, supply_chain_metadata, supply_chain_commits)):
            profile_errors.append("supply-chain workspace is missing directed sidecar storage")
        else:
            profile_errors.extend(await validate_snapshot_metadata(payload, supply_chain_metadata))

    strict_source_ids = (
        profile.strict_source_ids
        or os.getenv("GRAPHRAG_STRICT_SOURCE_IDS", "false").strip().lower() == "true"
    )
    record_errors: list[str] = []
    valid_entities: list[dict[str, Any]] = []
    known_entities: set[str] = set()
    for entity in normalized_entities:
        ok, reason = validate_entity(
            entity,
            resolvable_source_ids=resolvable_source_ids,
            strict_source_ids=strict_source_ids,
        )
        if not ok:
            record_errors.append(reason)
            continue
        valid_entities.append(entity)
        known_entities.add(entity["entity_name"])

    valid_relationships: list[dict[str, Any]] = []
    for relationship in normalized_relationships:
        ok, reason = validate_relationship(
            relationship,
            known_entities,
            resolvable_source_ids=resolvable_source_ids,
            strict_source_ids=strict_source_ids,
        )
        if not ok:
            record_errors.append(reason)
            continue
        valid_relationships.append(relationship)

    warnings: list[str] = []
    for record_type, records in (("entity", valid_entities), ("relationship", valid_relationships)):
        for record in records:
            dangling = [
                source_id
                for source_id in str(record.get("source_id", "")).split(GRAPH_FIELD_SEP)
                if source_id in unresolvable_source_ids
            ]
            if dangling:
                if record_type == "entity":
                    name = record.get("entity_name", "")
                    warnings.append(
                        f"entity `{name}` has unresolvable source_id(s): {', '.join(dangling)}"
                    )
                else:
                    src_id = record.get("src_id") or record.get("source_entity")
                    tgt_id = record.get("tgt_id") or record.get("target_entity")
                    warnings.append(
                        f"relationship `{src_id}->{tgt_id}` has unresolvable source_id(s): "
                        f"{', '.join(dangling)}"
                    )

    all_errors = [*structure_errors, *chunk_errors, *profile_errors, *record_errors]
    fatal_errors = [*structure_errors, *chunk_errors, *profile_errors]
    if profile.name != GENERIC_PROFILE:
        fatal_errors.extend(record_errors)
    result.update(
        {
            "counts": counts,
            "errors": all_errors,
            "warnings": warnings,
            "fatal_errors": fatal_errors,
            "record_errors": record_errors,
            "normalized_entities": normalized_entities,
            "normalized_relationships": normalized_relationships,
            "valid_entities": valid_entities,
            "valid_relationships": valid_relationships,
            "chunk_upserts": chunk_upserts,
            "chunk_vdb_upserts": chunk_vdb_upserts,
            "unresolved_chunk_ids": unresolved_chunk_ids,
            "unresolvable_source_ids": unresolvable_source_ids,
            "resolvable_source_ids": resolvable_source_ids,
        }
    )
    return result


async def ingest_extracted_json(payload, storage_instances, global_config) -> dict:
    if not isinstance(payload, dict):
        return {"status": "error", "message": "payload must be an object"}
    workspace = getattr(storage_instances.get("graph"), "workspace", "")
    async with ingestion_session_lock(global_config["working_dir"], workspace):
        return await _ingest_extracted_json(payload, storage_instances, global_config)


async def _ingest_extracted_json(payload, storage_instances, global_config) -> dict:
    document_id: str | None = None
    supply_chain_commits = None
    supply_chain_commit_started = False
    try:
        if not isinstance(payload, dict):
            return {"status": "error", "message": "payload must be an object"}
        preflight = await preflight_extraction(payload, storage_instances, global_config)
        document_id = preflight["document_id"]
        file_path = preflight["file_path"]
        profile = preflight["profile"]
        errors = list(preflight["record_errors"])
        warnings = list(preflight["warnings"])
        if preflight["fatal_errors"]:
            return {
                "status": "validation_failed",
                "message": f"Validation failed for document `{document_id}`",
                "document_id": document_id,
                "file_path": file_path,
                "chunks_ingested": 0,
                "entities_merged": 0,
                "relationships_merged": 0,
                "ingestion_counts": {
                    "entities": {"added": 0, "merged": 0, "skipped_duplicate": 0},
                    "relationships": {"added": 0, "merged": 0, "skipped_duplicate": 0},
                    "chunks": {"added": 0, "merged": 0, "skipped_duplicate": 0},
                },
                "errors": preflight["errors"],
                "warnings": warnings,
            }

        timestamp = int(payload.get("timestamp", time.time()))
        text_chunks = storage_instances["text_chunks"]
        chunks_vdb = storage_instances["chunks_vdb"]
        graph = storage_instances["graph"]
        entities_vdb = storage_instances["entities_vdb"]
        relationships_vdb = storage_instances["relationships_vdb"]
        entity_chunks = storage_instances.get("entity_chunks")
        relation_chunks = storage_instances.get("relation_chunks")
        pending_summaries = storage_instances.get("pending_summaries")
        entity_vector_batch = VectorWriteBatch(entities_vdb)
        relationship_vector_batch = VectorWriteBatch(relationships_vdb)
        chunk_upserts = preflight["chunk_upserts"]
        chunk_vdb_upserts = preflight["chunk_vdb_upserts"]
        valid_entities = preflight["valid_entities"]
        valid_relationships = preflight["valid_relationships"]
        directed_relationships = storage_instances.get("directed_relationships")
        supply_chain_metadata = storage_instances.get("supply_chain_metadata")
        supply_chain_commits = storage_instances.get("supply_chain_commits")
        if profile.name == SUPPLY_CHAIN_PROFILE:
            await begin_supply_chain_commit(supply_chain_commits, document_id)
            supply_chain_commit_started = True

        existing_chunks = await text_chunks.get_by_ids(list(chunk_upserts))
        chunk_counts = _classify_chunk_inputs(chunk_upserts, existing_chunks)
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

        known_entities: set[str] = set()
        grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entity in valid_entities:
            entity_name, node_list = agent_json_to_nodes_data(
                entity,
                timestamp,
                document_id,
                source_id_is_namespaced=True,
            )
            grouped_nodes[entity_name].extend(node_list)
            known_entities.add(entity_name)

        grouped_edges: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for rel in valid_relationships:
            src_id, tgt_id, edge_list = agent_json_to_edges_data(
                rel,
                timestamp,
                document_id,
                source_id_is_namespaced=True,
            )
            grouped_edges[(src_id, tgt_id)].extend(edge_list)

        entity_counts, relationship_counts = await _classify_graph_inputs(
            graph,
            entity_chunks,
            relation_chunks,
            grouped_nodes,
            grouped_edges,
        )

        pipeline_status: dict[str, Any] = {"summary_events": []}
        merged_nodes = []
        for entity_name, node_list in grouped_nodes.items():
            async with get_storage_keyed_lock(f"node:{entity_name}"):
                node_data = await _merge_nodes_then_upsert(
                    entity_name=entity_name,
                    nodes_data=node_list,
                    knowledge_graph_inst=graph,
                    entity_vdb=entity_vector_batch,
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
                    relationships_vdb=relationship_vector_batch,
                    entity_vdb=entity_vector_batch,
                    global_config=global_config,
                    pipeline_status=pipeline_status,
                    relation_chunks_storage=relation_chunks,
                    entity_chunks_storage=entity_chunks,
                    pending_summaries_storage=pending_summaries,
                )
                if edge_data is not None:
                    merged_edges.append(edge_data)

        await safe_vdb_operation_with_exception(
            operation=entity_vector_batch.flush,
            operation_name="entity_batch_upsert",
            entity_name=document_id,
            max_retries=3,
            retry_delay=0.1,
        )
        await safe_vdb_operation_with_exception(
            operation=relationship_vector_batch.flush,
            operation_name="relationship_batch_upsert",
            entity_name=document_id,
            max_retries=3,
            retry_delay=0.2,
        )

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
        if profile.name == SUPPLY_CHAIN_PROFILE:
            await persist_directed_relationships(
                valid_relationships,
                document_id=document_id,
                directed_relationships_storage=directed_relationships,
            )
            await persist_snapshot_metadata(payload, supply_chain_metadata)
            await finish_supply_chain_commit(supply_chain_commits, document_id)
            supply_chain_commit_started = False
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
            "ingestion_counts": {
                "entities": entity_counts,
                "relationships": relationship_counts,
                "chunks": chunk_counts,
            },
            "errors": errors,
            "warnings": warnings,
        }
        if pipeline_status.get("summary_events"):
            result["summary_events"] = pipeline_status["summary_events"]
        return result
    except Exception as exc:
        if supply_chain_commit_started and supply_chain_commits is not None and document_id is not None:
            try:
                await fail_supply_chain_commit(supply_chain_commits, document_id, str(exc))
            except Exception:
                logger.exception("Failed to mark supply-chain ingestion as failed (document_id=%s)", document_id)
        logger.exception("ingest_extracted_json failed (document_id=%s)", payload.get("document_id"))
        return {"status": "error", "message": str(exc)}
