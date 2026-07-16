from __future__ import annotations

import asyncio
from typing import Any

from config import GRAPH_FIELD_SEP
from core.merge import _sync_pending_summary_record
from core.storage.shared_storage import get_storage_keyed_lock
from core.summary import (
    _assemble_description,
    _enforce_summary_budget,
    _handle_entity_relation_summary,
    _split_summary_zones,
    resolve_raw_tail_size,
)
from core.utils import (
    compute_mdhash_id,
    logger,
    make_relation_chunk_key,
    safe_vdb_operation_with_exception,
    strip_summary_marker,
)

VALID_KINDS = {"entity", "relation"}


def _zone_split(description_list: list[str], raw_tail_size: int) -> tuple[str | None, list[str], list[str]]:
    """(prior_summary, old_descriptions beyond raw_tail_size oldest-first, keep_tail newest)."""
    prior_summary, raw_tail = _split_summary_zones(description_list)
    keep_tail = raw_tail[len(raw_tail) - raw_tail_size :] if raw_tail_size else []
    old_descriptions = raw_tail[: len(raw_tail) - len(keep_tail)]
    return prior_summary, old_descriptions, keep_tail


async def _read_pending_records(storage_instances: dict, limit: int) -> list[tuple[str, dict]]:
    pending_summaries = storage_instances.get("pending_summaries")
    if pending_summaries is None:
        return []
    items = list((await pending_summaries.get_all_items()).items())
    items.sort(key=lambda kv: kv[1].get("created_at", 0))
    return items[:limit]


async def _fetch_pending_description(graph, record: dict) -> str:
    kind = record.get("kind")
    name = record.get("name")
    src = record.get("src")
    tgt = record.get("tgt")
    if kind == "entity":
        node = await graph.get_node(name)
        return (node or {}).get("description", "")
    if kind == "relation" and src and tgt:
        edge = await graph.get_edge(src, tgt)
        return (edge or {}).get("description", "")
    return ""


async def list_pending_summaries(storage_instances: dict, global_config: dict, limit: int = 50) -> dict:
    """List entities/relations whose descriptions have outgrown their bounds and
    need agent compression, with the live content to summarize. Recomputed from
    the current graph node/edge on every call — the pending record is a work
    queue, not authoritative content, so descriptions that grew since deferral
    are reflected here.

    Each item's `description_count` must be echoed back as `submit_summary`'s
    `expected_description_count` — it is an optimistic-concurrency token that
    guards against a concurrent ingest silently dropping content between this
    call and the eventual submit.
    """
    try:
        graph = storage_instances["graph"]
        raw_tail_size = resolve_raw_tail_size(global_config)
        records = await _read_pending_records(storage_instances, limit)
        descriptions = await asyncio.gather(
            *(_fetch_pending_description(graph, record) for _, record in records)
        )

        items: list[dict[str, Any]] = []
        for (key, record), description in zip(records, descriptions):
            description_list = description.split(GRAPH_FIELD_SEP) if description else []
            prior_summary, old_descriptions, keep_tail = _zone_split(description_list, raw_tail_size)
            items.append(
                {
                    "key": key,
                    "kind": record.get("kind"),
                    "name": record.get("name"),
                    "src": record.get("src"),
                    "tgt": record.get("tgt"),
                    "description_count": len(description_list),
                    "content_to_summarize": {
                        "prior_summary": prior_summary,
                        "old_descriptions": old_descriptions,
                        "keep_tail": keep_tail,
                    },
                }
            )
        return {
            "status": "success",
            "message": f"{len(items)} pending summaries",
            "items": items,
        }
    except Exception as exc:
        logger.exception("list_pending_summaries failed")
        return {"status": "error", "message": str(exc)}


async def submit_summary(
    storage_instances: dict,
    global_config: dict,
    name: str,
    kind: str,
    summary_text: str,
    expected_description_count: int,
    src: str | None = None,
    tgt: str | None = None,
) -> dict:
    """Store the agent's rolling summary and clear (or re-flag) the pending record.

    The whole read-check-write happens under the same per-entity/per-relation
    lock `ingest/pipeline.py` takes for merges (`get_storage_keyed_lock`), so a
    concurrent ingest can't land between the concurrency check and the write.

    `expected_description_count` must be the `description_count` the agent last
    read from `list_pending_summaries` for this key. If a merge landed new
    descriptions in between, the live count won't match; a submission whose
    `summary_text` doesn't already match what's currently stored is rejected
    (re-fetch via list_pending_summaries and retry) rather than silently
    discarding whatever changed. If `summary_text` already matches the stored
    summary, the count mismatch is treated as a safe no-op retry.

    After writing, the assembled field is re-checked against the compression
    bounds — if `keep_tail` alone is still over budget, the entity/relation is
    re-flagged pending instead of the record being silently cleared.
    """
    try:
        if kind not in VALID_KINDS:
            return {"status": "error", "message": f"kind must be one of {sorted(VALID_KINDS)}"}
        summary_text = (summary_text or "").strip()
        if not summary_text:
            return {"status": "error", "message": "summary_text is required"}
        if kind == "entity":
            entity_name = str(name).strip()
            if not entity_name:
                return {"status": "error", "message": "name is required for kind='entity'"}
        else:
            if not src or not tgt:
                return {"status": "error", "message": "relation submit requires both src and tgt"}

        graph = storage_instances["graph"]
        pending_summaries = storage_instances.get("pending_summaries")
        raw_tail_size = resolve_raw_tail_size(global_config)
        summary_context_size = global_config["summary_context_size"]
        summary_max_tokens = global_config["summary_max_tokens"]
        tokenizer = global_config["tokenizer"]

        # Only cap the agent's own text against the summary budget when it
        # overruns summary_context_size — otherwise store it verbatim, untruncated.
        if len(tokenizer.encode(summary_text)) > summary_context_size:
            summary_text = _enforce_summary_budget(summary_text, tokenizer, summary_max_tokens)

        lock_key = f"node:{entity_name}" if kind == "entity" else f"edge:{':'.join(sorted((src, tgt)))}"
        async with get_storage_keyed_lock(lock_key):
            if kind == "entity":
                node = await graph.get_node(entity_name)
                if node is None:
                    return {"status": "error", "message": f"entity `{entity_name}` does not exist"}
                description = node.get("description", "")
            else:
                if not await graph.has_edge(src, tgt):
                    return {"status": "error", "message": f"relation `{src}`~`{tgt}` does not exist"}
                edge = await graph.get_edge(src, tgt)
                description = (edge or {}).get("description", "")

            current_description_list = description.split(GRAPH_FIELD_SEP) if description else []
            prior_summary, _old_descriptions, keep_tail = _zone_split(current_description_list, raw_tail_size)

            if len(current_description_list) != expected_description_count:
                if prior_summary == summary_text:
                    # Safe no-op: this exact summary is already the stored rolling
                    # summary (e.g. a client retried after a timeout on a write
                    # that actually succeeded) — nothing new to apply.
                    pending_key = entity_name if kind == "entity" else make_relation_chunk_key(src, tgt)
                    return {
                        "status": "success",
                        "message": f"summary already stored for {kind} `{name if kind == 'entity' else pending_key}`",
                        "kind": kind,
                        "name": name,
                        "src": src,
                        "tgt": tgt,
                    }
                return {
                    "status": "error",
                    "message": (
                        f"pending content changed since it was last listed (expected "
                        f"{expected_description_count} descriptions, found {len(current_description_list)}); "
                        "call list_pending_summaries again and resubmit"
                    ),
                }

            new_description = _assemble_description(summary_text, keep_tail, GRAPH_FIELD_SEP)
            new_description_list = new_description.split(GRAPH_FIELD_SEP) if new_description else []
            # Re-derive whether the freshly assembled field is still over bounds
            # (e.g. keep_tail alone exceeds the token budget) instead of blindly
            # clearing the pending flag.
            _final_description, summary_reason = _handle_entity_relation_summary(
                new_description_list, GRAPH_FIELD_SEP, global_config
            )

            if kind == "entity":
                updated_node = {**node, "description": new_description}
                await graph.upsert_node(entity_name, node_data=updated_node)
                entity_vdb = storage_instances.get("entities_vdb")
                if entity_vdb is not None:
                    entity_vdb_id = compute_mdhash_id(entity_name, prefix="ent-")
                    entity_content = f"{entity_name}\n{strip_summary_marker(new_description)}"
                    await safe_vdb_operation_with_exception(
                        operation=lambda payload={
                            entity_vdb_id: {
                                "entity_name": entity_name,
                                "entity_type": updated_node.get("entity_type", "UNKNOWN"),
                                "content": entity_content,
                                "source_id": updated_node.get("source_id", ""),
                                "file_path": updated_node.get("file_path", "unknown_source"),
                            }
                        }: entity_vdb.upsert(payload),
                        operation_name="submit_summary_entity_upsert",
                        entity_name=entity_name,
                        max_retries=3,
                        retry_delay=0.1,
                    )
                await graph.index_done_callback()
                if entity_vdb is not None:
                    await entity_vdb.index_done_callback()
                pending_key = entity_name
            else:
                updated_edge = {**edge, "description": new_description}
                await graph.upsert_edge(src, tgt, updated_edge)
                relationships_vdb = storage_instances.get("relationships_vdb")
                if relationships_vdb is not None:
                    sorted_src, sorted_tgt = sorted((src, tgt))
                    rel_vdb_id = compute_mdhash_id(sorted_src + sorted_tgt, prefix="rel-")
                    # Guard against a stale reverse-order entry from before sorted-id
                    # normalization existed (mirrors core/merge.py's upsert path).
                    rel_vdb_id_reverse = compute_mdhash_id(sorted_tgt + sorted_src, prefix="rel-")
                    try:
                        await relationships_vdb.delete([rel_vdb_id, rel_vdb_id_reverse])
                    except Exception:
                        pass
                    keywords = updated_edge.get("keywords", "")
                    rel_content = f"{keywords}\t{sorted_src}\n{sorted_tgt}\n{strip_summary_marker(new_description)}"
                    await safe_vdb_operation_with_exception(
                        operation=lambda payload={
                            rel_vdb_id: {
                                "src_id": sorted_src,
                                "tgt_id": sorted_tgt,
                                "source_id": updated_edge.get("source_id", ""),
                                "content": rel_content,
                                "keywords": keywords,
                                "description": strip_summary_marker(new_description),
                                "weight": updated_edge.get("weight", 1.0),
                                "file_path": updated_edge.get("file_path", "unknown_source"),
                            }
                        }: relationships_vdb.upsert(payload),
                        operation_name="submit_summary_relation_upsert",
                        entity_name=f"{sorted_src}-{sorted_tgt}",
                        max_retries=3,
                        retry_delay=0.2,
                    )
                await graph.index_done_callback()
                if relationships_vdb is not None:
                    await relationships_vdb.index_done_callback()
                pending_key = make_relation_chunk_key(src, tgt)

            if pending_summaries is not None:
                await _sync_pending_summary_record(
                    pending_summaries,
                    key=pending_key,
                    kind=kind,
                    name=name,
                    src=src if kind == "relation" else None,
                    tgt=tgt if kind == "relation" else None,
                    summary_reason=summary_reason,
                    description_count=len(new_description_list),
                )
                await pending_summaries.index_done_callback()

        return {
            "status": "success",
            "message": f"summary stored for {kind} `{name if kind == 'entity' else pending_key}`",
            "kind": kind,
            "name": name,
            "src": src,
            "tgt": tgt,
        }
    except Exception as exc:
        logger.exception("submit_summary failed (kind=%s, name=%s)", kind, name)
        return {"status": "error", "message": str(exc)}
