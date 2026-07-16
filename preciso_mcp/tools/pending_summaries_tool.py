from __future__ import annotations

from typing import Any

from config import GRAPH_FIELD_SEP
from core.summary import _assemble_description, _enforce_summary_budget, _split_summary_zones
from core.utils import (
    compute_mdhash_id,
    logger,
    make_relation_chunk_key,
    safe_vdb_operation_with_exception,
    strip_summary_marker,
)

VALID_KINDS = {"entity", "relation"}


def _raw_tail_size(global_config: dict) -> int:
    return max(
        0,
        int(global_config.get("raw_tail_size", global_config.get("force_llm_summary_on_merge", 4))),
    )


def _zone_split(description: str, raw_tail_size: int) -> tuple[str | None, list[str], list[str]]:
    """(prior_summary, old_descriptions beyond raw_tail_size oldest-first, keep_tail newest)."""
    description_list = description.split(GRAPH_FIELD_SEP) if description else []
    prior_summary, raw_tail = _split_summary_zones(description_list)
    keep_tail = raw_tail[len(raw_tail) - raw_tail_size :] if raw_tail_size else []
    old_descriptions = raw_tail[: len(raw_tail) - len(keep_tail)]
    return prior_summary, old_descriptions, keep_tail


async def _read_pending_records(storage_instances: dict, limit: int) -> list[tuple[str, dict]]:
    pending_summaries = storage_instances.get("pending_summaries")
    if pending_summaries is None:
        return []
    async with pending_summaries._storage_lock:
        items = list(pending_summaries._data.items())
    items.sort(key=lambda kv: kv[1].get("created_at", 0))
    return items[:limit]


async def list_pending_summaries(storage_instances: dict, global_config: dict, limit: int = 50) -> dict:
    """List entities/relations deferred in summary_mode="agent", with the live
    content the agent should compress. Recomputed from the current graph node/edge
    on every call — the pending record is a work queue, not authoritative content,
    so descriptions that grew since deferral are reflected here.
    """
    try:
        graph = storage_instances["graph"]
        raw_tail_size = _raw_tail_size(global_config)
        records = await _read_pending_records(storage_instances, limit)
        items: list[dict[str, Any]] = []
        for key, record in records:
            kind = record.get("kind")
            name = record.get("name")
            src = record.get("src")
            tgt = record.get("tgt")
            if kind == "entity":
                node = await graph.get_node(name)
                description = (node or {}).get("description", "")
            elif kind == "relation" and src and tgt:
                edge = await graph.get_edge(src, tgt)
                description = (edge or {}).get("description", "")
            else:
                description = ""
            prior_summary, old_descriptions, keep_tail = _zone_split(description, raw_tail_size)
            items.append(
                {
                    "key": key,
                    "kind": kind,
                    "name": name,
                    "src": src,
                    "tgt": tgt,
                    "reason": record.get("reason"),
                    "description_count": record.get("description_count"),
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
    src: str | None = None,
    tgt: str | None = None,
) -> dict:
    """Store the agent's rolling summary and clear the pending record.

    Re-reads the CURRENT description rather than trusting a stale
    list_pending_summaries snapshot, since new merges may have arrived in
    between. Idempotent: submitting the same summary_text again when the
    description already reflects it is a no-op success (same value re-upserted).
    """
    try:
        if kind not in VALID_KINDS:
            return {"status": "error", "message": f"kind must be one of {sorted(VALID_KINDS)}"}
        summary_text = (summary_text or "").strip()
        if not summary_text:
            return {"status": "error", "message": "summary_text is required"}

        graph = storage_instances["graph"]
        pending_summaries = storage_instances.get("pending_summaries")
        raw_tail_size = _raw_tail_size(global_config)
        summary_context_size = global_config["summary_context_size"]
        summary_max_tokens = global_config["summary_max_tokens"]
        tokenizer = global_config["tokenizer"]

        if kind == "entity":
            entity_name = str(name).strip()
            if not entity_name:
                return {"status": "error", "message": "name is required for kind='entity'"}
            node = await graph.get_node(entity_name)
            if node is None:
                return {"status": "error", "message": f"entity `{entity_name}` does not exist"}
            description = node.get("description", "")
        else:
            if not src or not tgt:
                return {"status": "error", "message": "relation submit requires both src and tgt"}
            if not await graph.has_edge(src, tgt):
                return {"status": "error", "message": f"relation `{src}`~`{tgt}` does not exist"}
            edge = await graph.get_edge(src, tgt)
            description = (edge or {}).get("description", "")

        _prior_summary, _old_descriptions, keep_tail = _zone_split(description, raw_tail_size)

        # Only cap the agent's own text against the LLM budget when it overruns
        # summary_context_size — otherwise store it verbatim, untruncated.
        if len(tokenizer.encode(summary_text)) > summary_context_size:
            summary_text = _enforce_summary_budget(summary_text, tokenizer, summary_max_tokens)

        new_description = _assemble_description(summary_text, keep_tail, GRAPH_FIELD_SEP)

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
            await pending_summaries.delete([pending_key])
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
