from __future__ import annotations

import time
from collections import Counter

from config import (
    DEFAULT_FILE_PATH_MORE_PLACEHOLDER,
    DEFAULT_MAX_FILE_PATHS,
    GRAPH_FIELD_SEP,
    SOURCE_IDS_LIMIT_METHOD_FIFO,
    SOURCE_IDS_LIMIT_METHOD_KEEP,
)
from core.storage.base import BaseGraphStorage, BaseKVStorage, BaseVectorStorage
from core.summary import PENDING_SUMMARY_REASON, _handle_entity_relation_summary
from core.utils import (
    _cooperative_yield,
    apply_source_ids_limit,
    compute_mdhash_id,
    make_relation_chunk_key,
    make_relation_vdb_id,
    merge_source_ids,
    performance_timing_log,
    safe_vdb_operation_with_exception,
    split_string_by_multi_markers,
    split_source_ids,
    strip_summary_marker,
)


async def _sync_pending_summary_record(
    pending_summaries_storage: BaseKVStorage | None,
    *,
    key: str,
    kind: str,
    name: str,
    src: str | None,
    tgt: str | None,
    summary_reason: str | None,
    description_count: int,
) -> None:
    """Write or clear the pending-summary work-queue record for `key`.

    Called after every merge. When a later merge brings the field back within
    bounds (summary_reason is no longer PENDING_SUMMARY_REASON), the stale
    record is cleared here too — the pending store is a work queue, not
    authoritative content. `description_count` doubles as an optimistic-
    concurrency token: submit_summary rejects a submission if the live count has
    moved since the agent last read it via list_pending_summaries.
    """
    if pending_summaries_storage is None:
        return
    if summary_reason == PENDING_SUMMARY_REASON:
        await pending_summaries_storage.upsert(
            {
                key: {
                    "kind": kind,
                    "name": name,
                    "src": src,
                    "tgt": tgt,
                    "description_count": description_count,
                    "created_at": int(time.time()),
                }
            }
        )
    else:
        await pending_summaries_storage.delete([key])


async def _merge_nodes_then_upsert(
    entity_name: str,
    nodes_data: list[dict],
    knowledge_graph_inst: BaseGraphStorage,
    entity_vdb: BaseVectorStorage | None,
    global_config: dict,
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    entity_chunks_storage: BaseKVStorage | None = None,
    pending_summaries_storage: BaseKVStorage | None = None,
):
    timing_start = time.perf_counter()
    try:
        already_entity_types = []
        already_source_ids = []
        already_description = []
        already_file_paths = []
        already_node = await knowledge_graph_inst.get_node(entity_name)
        if already_node:
            existing_entity_type = already_node.get("entity_type")
            if not isinstance(existing_entity_type, str) or not existing_entity_type.strip():
                existing_entity_type = "UNKNOWN"
            if "," in existing_entity_type:
                tokens = [token.strip() for token in existing_entity_type.split(",")]
                existing_entity_type = next((token for token in tokens if token), "UNKNOWN")
            already_entity_types.append(existing_entity_type)
            already_source_ids.extend((already_node.get("source_id") or "").split(GRAPH_FIELD_SEP))
            already_file_paths.extend((already_node.get("file_path") or "unknown_source").split(GRAPH_FIELD_SEP))
            existing_desc = (already_node.get("description") or "").strip()
            if existing_desc:
                already_description.extend(existing_desc.split(GRAPH_FIELD_SEP))
        new_source_ids = split_source_ids(
            dp["source_id"] for dp in nodes_data if dp.get("source_id")
        )
        existing_full_source_ids = []
        if entity_chunks_storage is not None:
            stored_chunks = await entity_chunks_storage.get_by_id(entity_name)
            if stored_chunks and isinstance(stored_chunks, dict):
                existing_full_source_ids = split_source_ids(stored_chunks.get("chunk_ids", []))
        if not existing_full_source_ids:
            existing_full_source_ids = [chunk_id for chunk_id in already_source_ids if chunk_id]
        full_source_ids = merge_source_ids(existing_full_source_ids, new_source_ids)
        if entity_chunks_storage is not None and full_source_ids:
            await entity_chunks_storage.upsert(
                {entity_name: {"chunk_ids": full_source_ids, "count": len(full_source_ids)}}
            )
        limit_method = global_config.get("source_ids_limit_method")
        max_source_limit = global_config.get("max_source_ids_per_entity")
        source_ids = apply_source_ids_limit(
            full_source_ids,
            max_source_limit,
            limit_method,
            identifier=f"`{entity_name}`",
        )
        if limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP:
            allowed_source_ids = set(source_ids)
            nodes_data = [
                dp
                for dp in nodes_data
                if not dp.get("source_id")
                or any(
                    chunk_id in allowed_source_ids
                    for chunk_id in split_source_ids([dp["source_id"]])
                )
                or any(
                    chunk_id in existing_full_source_ids
                    for chunk_id in split_source_ids([dp["source_id"]])
                )
            ]
        else:
            nodes_data = list(nodes_data)
        if (
            limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP
            and len(existing_full_source_ids) >= max_source_limit
            and not nodes_data
        ):
            return dict(already_node) if already_node else None
        source_id = GRAPH_FIELD_SEP.join(source_ids)
        entity_type = sorted(
            Counter([dp["entity_type"] for dp in nodes_data] + already_entity_types).items(),
            key=lambda x: x[1],
            reverse=True,
        )[0][0]
        unique_nodes = {}
        for i, dp in enumerate(nodes_data, start=1):
            desc = dp.get("description")
            if desc and desc not in unique_nodes:
                unique_nodes[desc] = dp
            await _cooperative_yield(i, every=32)
        sorted_nodes = sorted(
            unique_nodes.values(),
            key=lambda x: (x.get("timestamp", 0), -len(x.get("description", ""))),
        )
        description_list = already_description + [dp["description"] for dp in sorted_nodes]
        if not description_list:
            description_list = [f"Entity {entity_name}"]
        description, summary_reason = _handle_entity_relation_summary(
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
        )
        if pipeline_status is not None and summary_reason:
            pipeline_status.setdefault("summary_events", []).append(
                {
                    "type": "entity",
                    "name": entity_name,
                    "reason": summary_reason,
                    "description_count": len(description_list),
                }
            )
        await _sync_pending_summary_record(
            pending_summaries_storage,
            key=entity_name,
            kind="entity",
            name=entity_name,
            src=None,
            tgt=None,
            summary_reason=summary_reason,
            description_count=len(description_list),
        )
        file_paths_list = []
        seen_paths = set()
        has_placeholder = False
        max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
        file_path_placeholder = global_config.get(
            "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
        )
        for fp in already_file_paths:
            if fp and fp.startswith(f"...{file_path_placeholder}"):
                has_placeholder = True
                continue
            if fp and fp not in seen_paths:
                file_paths_list.append(fp)
                seen_paths.add(fp)
        for i, dp in enumerate(nodes_data, start=1):
            file_path_item = dp.get("file_path")
            if file_path_item and file_path_item not in seen_paths:
                file_paths_list.append(file_path_item)
                seen_paths.add(file_path_item)
            await _cooperative_yield(i, every=32)
        if len(file_paths_list) > max_file_paths:
            original_count_str = f"{len(file_paths_list)}+" if has_placeholder else str(len(file_paths_list))
            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                file_paths_list = file_paths_list[-max_file_paths:]
                file_paths_list.append(f"...{file_path_placeholder}...(FIFO)")
            else:
                file_paths_list = file_paths_list[:max_file_paths]
                file_paths_list.append(f"...{file_path_placeholder}...(KEEP Old)")
        file_path = GRAPH_FIELD_SEP.join(file_paths_list)
        truncation_info = ""
        if len(source_ids) < len(full_source_ids):
            truncation_info = (
                f"{limit_method} {len(source_ids)}/{len(full_source_ids)}"
                if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO
                else "KEEP Old"
            )
        node_data = {
            "entity_id": entity_name,
            "entity_type": entity_type,
            "description": description,
            "source_id": source_id,
            "file_path": file_path,
            "created_at": int(time.time()),
            "truncate": truncation_info,
        }
        await knowledge_graph_inst.upsert_node(entity_name, node_data=node_data)
        node_data["entity_name"] = entity_name
        if entity_vdb is not None:
            entity_vdb_id = compute_mdhash_id(str(entity_name), prefix="ent-")
            # SUMMARY_MARKER is internal bookkeeping, never embedded content
            entity_content = f"{entity_name}\n{strip_summary_marker(description)}"
            await safe_vdb_operation_with_exception(
                operation=lambda payload={
                    entity_vdb_id: {
                        "entity_name": entity_name,
                        "entity_type": entity_type,
                        "content": entity_content,
                        "source_id": source_id,
                        "file_path": file_path,
                    }
                }: entity_vdb.upsert(payload),
                operation_name="entity_upsert",
                entity_name=entity_name,
                max_retries=3,
                retry_delay=0.1,
            )
        return node_data
    finally:
        performance_timing_log(
            "[_merge_nodes_then_upsert] `%s` completed in %.4fs",
            entity_name,
            time.perf_counter() - timing_start,
        )


async def _merge_edges_then_upsert(
    src_id: str,
    tgt_id: str,
    edges_data: list[dict],
    knowledge_graph_inst: BaseGraphStorage,
    relationships_vdb: BaseVectorStorage | None,
    entity_vdb: BaseVectorStorage | None,
    global_config: dict,
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    added_entities: list = None,
    relation_chunks_storage: BaseKVStorage | None = None,
    entity_chunks_storage: BaseKVStorage | None = None,
    pending_summaries_storage: BaseKVStorage | None = None,
):
    timing_start = time.perf_counter()
    timing_relation = f"`{src_id}`~`{tgt_id}`"
    try:
        if src_id == tgt_id:
            return None
        already_edge = None
        already_weights = []
        already_source_ids = []
        already_description = []
        already_keywords = []
        already_file_paths = []
        if await knowledge_graph_inst.has_edge(src_id, tgt_id):
            already_edge = await knowledge_graph_inst.get_edge(src_id, tgt_id)
            if already_edge:
                already_weights.append(already_edge.get("weight", 1.0))
                if already_edge.get("source_id") is not None:
                    already_source_ids.extend(already_edge["source_id"].split(GRAPH_FIELD_SEP))
                if already_edge.get("file_path") is not None:
                    already_file_paths.extend(already_edge["file_path"].split(GRAPH_FIELD_SEP))
                if already_edge.get("description") is not None:
                    already_description.extend(already_edge["description"].split(GRAPH_FIELD_SEP))
                if already_edge.get("keywords") is not None:
                    already_keywords.extend(
                        split_string_by_multi_markers(already_edge["keywords"], [GRAPH_FIELD_SEP])
                    )
        new_source_ids = split_source_ids(
            dp["source_id"] for dp in edges_data if dp.get("source_id")
        )
        storage_key = make_relation_chunk_key(src_id, tgt_id)
        existing_full_source_ids = []
        if relation_chunks_storage is not None:
            stored_chunks = await relation_chunks_storage.get_by_id(storage_key)
            if stored_chunks and isinstance(stored_chunks, dict):
                existing_full_source_ids = split_source_ids(stored_chunks.get("chunk_ids", []))
        if not existing_full_source_ids:
            existing_full_source_ids = [chunk_id for chunk_id in already_source_ids if chunk_id]
        preexisting_source_ids = set(existing_full_source_ids)
        full_source_ids = merge_source_ids(existing_full_source_ids, new_source_ids)
        if relation_chunks_storage is not None and full_source_ids:
            await relation_chunks_storage.upsert(
                {storage_key: {"chunk_ids": full_source_ids, "count": len(full_source_ids)}}
            )
        limit_method = global_config.get("source_ids_limit_method")
        max_source_limit = global_config.get("max_source_ids_per_relation")
        source_ids = apply_source_ids_limit(
            full_source_ids,
            max_source_limit,
            limit_method,
            identifier=f"`{src_id}`~`{tgt_id}`",
        )
        limit_method = global_config.get("source_ids_limit_method") or SOURCE_IDS_LIMIT_METHOD_KEEP
        if limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP:
            allowed_source_ids = set(source_ids)
            edges_data = [
                dp
                for dp in edges_data
                if not dp.get("source_id")
                or any(
                    chunk_id in allowed_source_ids
                    for chunk_id in split_source_ids([dp["source_id"]])
                )
                or any(
                    chunk_id in existing_full_source_ids
                    for chunk_id in split_source_ids([dp["source_id"]])
                )
            ]
        else:
            edges_data = list(edges_data)
        if (
            limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP
            and len(existing_full_source_ids) >= max_source_limit
            and not edges_data
        ):
            return dict(already_edge) if already_edge else None
        source_id = GRAPH_FIELD_SEP.join(source_ids)
        weight = sum(already_weights) + sum(
            dp["weight"]
            for dp in edges_data
            if not set(split_source_ids([dp.get("source_id", "")])).issubset(preexisting_source_ids)
        )
        all_keywords = set()
        for i, keyword_str in enumerate(already_keywords, start=1):
            if keyword_str:
                all_keywords.update(k.strip() for k in keyword_str.split(",") if k.strip())
            await _cooperative_yield(i, every=32)
        for i, edge in enumerate(edges_data, start=1):
            if edge.get("keywords"):
                all_keywords.update(k.strip() for k in edge["keywords"].split(",") if k.strip())
            await _cooperative_yield(i, every=32)
        keywords = ",".join(sorted(all_keywords))
        unique_edges = {}
        for i, dp in enumerate(edges_data, start=1):
            description_value = dp.get("description")
            if description_value and description_value not in unique_edges:
                unique_edges[description_value] = dp
            await _cooperative_yield(i, every=32)
        sorted_edges = sorted(
            unique_edges.values(),
            key=lambda x: (x.get("timestamp", 0), -len(x.get("description", ""))),
        )
        description_list = already_description + [dp["description"] for dp in sorted_edges]
        if not description_list:
            raise ValueError(f"Relation {src_id}~{tgt_id} has no description")
        description, summary_reason = _handle_entity_relation_summary(
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
        )
        if pipeline_status is not None and summary_reason:
            pipeline_status.setdefault("summary_events", []).append(
                {
                    "type": "relation",
                    "name": timing_relation,
                    "reason": summary_reason,
                    "description_count": len(description_list),
                }
            )
        await _sync_pending_summary_record(
            pending_summaries_storage,
            key=make_relation_chunk_key(src_id, tgt_id),
            kind="relation",
            name=timing_relation,
            src=src_id,
            tgt=tgt_id,
            summary_reason=summary_reason,
            description_count=len(description_list),
        )
        file_paths_list = []
        seen_paths = set()
        has_placeholder = False
        max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
        file_path_placeholder = global_config.get(
            "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
        )
        for fp in already_file_paths:
            if fp and fp.startswith(f"...{file_path_placeholder}"):
                has_placeholder = True
                continue
            if fp and fp not in seen_paths:
                file_paths_list.append(fp)
                seen_paths.add(fp)
        for i, dp in enumerate(edges_data, start=1):
            file_path_item = dp.get("file_path")
            if file_path_item and file_path_item not in seen_paths:
                file_paths_list.append(file_path_item)
                seen_paths.add(file_path_item)
            await _cooperative_yield(i, every=32)
        if len(file_paths_list) > max_file_paths:
            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                file_paths_list = file_paths_list[-max_file_paths:]
                file_paths_list.append(f"...{file_path_placeholder}...(FIFO)")
            else:
                file_paths_list = file_paths_list[:max_file_paths]
                file_paths_list.append(f"...{file_path_placeholder}...(KEEP Old)")
        file_path = GRAPH_FIELD_SEP.join(file_paths_list)
        truncation_info = ""
        if len(source_ids) < len(full_source_ids):
            truncation_info = (
                f"{limit_method} {len(source_ids)}/{len(full_source_ids)}"
                if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO
                else "KEEP Old"
            )
        for need_insert_id in [src_id, tgt_id]:
            existing_node = await knowledge_graph_inst.get_node(need_insert_id)
            if existing_node is None:
                node_created_at = int(time.time())
                node_data = {
                    "entity_id": need_insert_id,
                    "source_id": source_id,
                    "description": description,
                    "entity_type": "UNKNOWN",
                    "file_path": file_path,
                    "created_at": node_created_at,
                    "truncate": "",
                }
                await knowledge_graph_inst.upsert_node(need_insert_id, node_data=node_data)
                if entity_chunks_storage is not None:
                    chunk_ids = [chunk_id for chunk_id in full_source_ids if chunk_id]
                    if chunk_ids:
                        await entity_chunks_storage.upsert(
                            {need_insert_id: {"chunk_ids": chunk_ids, "count": len(chunk_ids)}}
                        )
                if entity_vdb is not None:
                    entity_vdb_id = compute_mdhash_id(need_insert_id, prefix="ent-")
                    entity_content = f"{need_insert_id}\n{strip_summary_marker(description)}"
                    await safe_vdb_operation_with_exception(
                        operation=lambda payload={
                            entity_vdb_id: {
                                "content": entity_content,
                                "entity_name": need_insert_id,
                                "source_id": source_id,
                                "entity_type": "UNKNOWN",
                                "file_path": file_path,
                            }
                        }: entity_vdb.upsert(payload),
                        operation_name="added_entity_upsert",
                        entity_name=need_insert_id,
                        max_retries=3,
                        retry_delay=0.1,
                    )
                if added_entities is not None:
                    added_entities.append(
                        {
                            "entity_name": need_insert_id,
                            "entity_type": "UNKNOWN",
                            "description": description,
                            "source_id": source_id,
                            "file_path": file_path,
                            "created_at": node_created_at,
                        }
                    )
            else:
                updated = False
                existing_full_source_ids = []
                if entity_chunks_storage is not None:
                    stored_chunks = await entity_chunks_storage.get_by_id(need_insert_id)
                    if stored_chunks and isinstance(stored_chunks, dict):
                        existing_full_source_ids = split_source_ids(stored_chunks.get("chunk_ids", []))
                if not existing_full_source_ids and existing_node.get("source_id"):
                    existing_full_source_ids = existing_node["source_id"].split(GRAPH_FIELD_SEP)
                merged_full_source_ids = merge_source_ids(existing_full_source_ids, [chunk_id for chunk_id in source_ids if chunk_id])
                if entity_chunks_storage is not None and merged_full_source_ids != existing_full_source_ids:
                    updated = True
                    await entity_chunks_storage.upsert(
                        {need_insert_id: {"chunk_ids": merged_full_source_ids, "count": len(merged_full_source_ids)}}
                    )
                limited_source_ids = apply_source_ids_limit(
                    merged_full_source_ids,
                    global_config.get("max_source_ids_per_entity"),
                    global_config.get("source_ids_limit_method", SOURCE_IDS_LIMIT_METHOD_KEEP),
                    identifier=f"`{need_insert_id}`",
                )
                limited_source_id_str = GRAPH_FIELD_SEP.join(limited_source_ids)
                if limited_source_id_str != existing_node.get("source_id", ""):
                    updated = True
                    updated_node_data = {**existing_node, "source_id": limited_source_id_str}
                    await knowledge_graph_inst.upsert_node(need_insert_id, node_data=updated_node_data)
                    if entity_vdb is not None:
                        entity_vdb_id = compute_mdhash_id(need_insert_id, prefix="ent-")
                        entity_content = f"{need_insert_id}\n{strip_summary_marker(existing_node.get('description', ''))}"
                        await safe_vdb_operation_with_exception(
                            operation=lambda payload={
                                entity_vdb_id: {
                                    "content": entity_content,
                                    "entity_name": need_insert_id,
                                    "source_id": limited_source_id_str,
                                    "entity_type": existing_node.get("entity_type", "UNKNOWN"),
                                    "file_path": existing_node.get("file_path", "unknown_source"),
                                }
                            }: entity_vdb.upsert(payload),
                            operation_name="existing_entity_update",
                            entity_name=need_insert_id,
                            max_retries=3,
                            retry_delay=0.1,
                        )
        edge_created_at = int(time.time())
        await knowledge_graph_inst.upsert_edge(
            src_id,
            tgt_id,
            {
                "weight": weight,
                "description": description,
                "keywords": keywords,
                "source_id": source_id,
                "file_path": file_path,
                "created_at": edge_created_at,
                "truncate": truncation_info,
            },
        )
        edge_data = {
            "src_id": src_id,
            "tgt_id": tgt_id,
            "description": description,
            "keywords": keywords,
            "source_id": source_id,
            "file_path": file_path,
            "created_at": edge_created_at,
            "truncate": truncation_info,
            "weight": weight,
        }
        sorted_src, sorted_tgt = sorted((src_id, tgt_id))
        if relationships_vdb is not None:
            rel_vdb_id = make_relation_vdb_id(sorted_src, sorted_tgt)
            legacy_rel_vdb_ids = [
                compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-"),
                compute_mdhash_id(sorted_tgt + sorted_src, prefix="rel-"),
            ]
            try:
                await relationships_vdb.delete([rel_vdb_id, *legacy_rel_vdb_ids])
            except Exception:
                pass
            rel_content = f"{keywords}\t{sorted_src}\n{sorted_tgt}\n{strip_summary_marker(description)}"
            await safe_vdb_operation_with_exception(
                operation=lambda payload={
                    rel_vdb_id: {
                        "src_id": sorted_src,
                        "tgt_id": sorted_tgt,
                        "source_id": source_id,
                        "content": rel_content,
                        "keywords": keywords,
                        "description": strip_summary_marker(description),
                        "weight": weight,
                        "file_path": file_path,
                    }
                }: relationships_vdb.upsert(payload),
                operation_name="relationship_upsert",
                entity_name=f"{sorted_src}-{sorted_tgt}",
                max_retries=3,
                retry_delay=0.2,
            )
        return edge_data
    finally:
        performance_timing_log(
            "[_merge_edges_then_upsert] %s completed in %.4fs",
            timing_relation,
            time.perf_counter() - timing_start,
        )
