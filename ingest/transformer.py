from __future__ import annotations

from config import GRAPH_FIELD_SEP


def resolve_source_id(
    source_id: str,
    document_id: str,
    chunk_id_map: dict[str, list[str]] | None = None,
) -> tuple[str, list[str]]:
    """Namespace each chunk id in `source_id` and expand split chunks to their parts.

    `source_id` may hold multiple chunk ids joined by GRAPH_FIELD_SEP. Each one is
    namespaced with `{document_id}::` so it keeps matching the chunk keys written by
    the ingest pipeline (see ingest/pipeline.py), which are namespaced the same way to
    stay unique across documents that reuse the same raw chunk numbering (chunk_001,
    chunk_002, ...).

    When the pipeline splits an oversized chunk it stores the pieces under derived keys
    (`chunk-1` -> `chunk-1-p1`, `chunk-1-p2`, ...), so the id the agent cited no longer
    exists on its own. `chunk_id_map` maps each raw chunk id to the keys actually
    written; a citation to a split chunk expands to every one of its parts, which keeps
    the evidence reachable instead of silently dangling.

    Returns `(resolved_source_id, unresolved_raw_ids)`. Ids absent from `chunk_id_map`
    are still namespaced and kept in the result — they may belong to an earlier ingest
    of the same document — but are reported back so the caller can verify them against
    storage.
    """
    if not source_id:
        return source_id, []

    resolved: list[str] = []
    unresolved: list[str] = []
    for part in source_id.split(GRAPH_FIELD_SEP):
        if not part:
            resolved.append(part)
            continue
        mapped = (chunk_id_map or {}).get(part)
        if mapped:
            resolved.extend(mapped)
        else:
            resolved.append(f"{document_id}::{part}")
            unresolved.append(part)

    deduplicated = list(dict.fromkeys(resolved))
    return GRAPH_FIELD_SEP.join(deduplicated), unresolved


def namespace_source_id(source_id: str, document_id: str) -> str:
    """Backwards-compatible wrapper around `resolve_source_id`."""
    return resolve_source_id(source_id, document_id)[0]


def agent_json_to_nodes_data(
    agent_entity: dict,
    timestamp: int,
    document_id: str,
    chunk_id_map: dict[str, list[str]] | None = None,
) -> tuple[str, list, list[str]]:
    entity_name = str(agent_entity["entity_name"]).strip()
    source_id, unresolved = resolve_source_id(
        str(agent_entity.get("source_id", "")).strip(), document_id, chunk_id_map
    )
    node = {
        "entity_type": str(agent_entity.get("entity_type", "UNKNOWN")).strip() or "UNKNOWN",
        "description": str(agent_entity.get("description", "")).strip(),
        "source_id": source_id,
        "file_path": str(agent_entity.get("file_path", "unknown_source")).strip() or "unknown_source",
        "timestamp": int(agent_entity.get("timestamp", timestamp)),
    }
    return entity_name, [node], unresolved


def agent_json_to_edges_data(
    agent_rel: dict,
    timestamp: int,
    document_id: str,
    chunk_id_map: dict[str, list[str]] | None = None,
) -> tuple[str, str, list, list[str]]:
    src_id = str(agent_rel.get("src_id") or agent_rel.get("source_entity")).strip()
    tgt_id = str(agent_rel.get("tgt_id") or agent_rel.get("target_entity")).strip()
    source_id, unresolved = resolve_source_id(
        str(agent_rel.get("source_id", "")).strip(), document_id, chunk_id_map
    )
    edge = {
        "description": str(agent_rel.get("description", "")).strip(),
        "keywords": str(agent_rel.get("keywords", "")).strip(),
        "source_id": source_id,
        "file_path": str(agent_rel.get("file_path", "unknown_source")).strip() or "unknown_source",
        "weight": float(agent_rel.get("weight", 1.0)),
        "timestamp": int(agent_rel.get("timestamp", timestamp)),
    }
    return src_id, tgt_id, [edge], unresolved
