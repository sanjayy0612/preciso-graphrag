from __future__ import annotations

from config import GRAPH_FIELD_SEP


def namespace_source_id(source_id: str, document_id: str) -> str:
    """Prefix each chunk id referenced by `source_id` with `{document_id}::`.

    `source_id` may hold multiple chunk ids joined by GRAPH_FIELD_SEP; each
    one is namespaced individually so it keeps matching the chunk keys
    written by the ingest pipeline (see ingest/pipeline.py), which are
    namespaced the same way to stay unique across documents that reuse the
    same raw chunk numbering (chunk_001, chunk_002, ...).
    """
    if not source_id:
        return source_id
    parts = source_id.split(GRAPH_FIELD_SEP)
    return GRAPH_FIELD_SEP.join(f"{document_id}::{part}" if part else part for part in parts)


def agent_json_to_nodes_data(agent_entity: dict, timestamp: int, document_id: str) -> tuple[str, list]:
    entity_name = str(agent_entity["entity_name"]).strip()
    node = {
        "entity_type": str(agent_entity.get("entity_type", "UNKNOWN")).strip() or "UNKNOWN",
        "description": str(agent_entity.get("description", "")).strip(),
        "source_id": namespace_source_id(str(agent_entity.get("source_id", "")).strip(), document_id),
        "file_path": str(agent_entity.get("file_path", "unknown_source")).strip() or "unknown_source",
        "timestamp": int(agent_entity.get("timestamp", timestamp)),
    }
    return entity_name, [node]


def agent_json_to_edges_data(agent_rel: dict, timestamp: int, document_id: str) -> tuple[str, str, list]:
    src_id = str(agent_rel.get("src_id") or agent_rel.get("source_entity")).strip()
    tgt_id = str(agent_rel.get("tgt_id") or agent_rel.get("target_entity")).strip()
    edge = {
        "description": str(agent_rel.get("description", "")).strip(),
        "keywords": str(agent_rel.get("keywords", "")).strip(),
        "source_id": namespace_source_id(str(agent_rel.get("source_id", "")).strip(), document_id),
        "file_path": str(agent_rel.get("file_path", "unknown_source")).strip() or "unknown_source",
        "weight": float(agent_rel.get("weight", 1.0)),
        "timestamp": int(agent_rel.get("timestamp", timestamp)),
    }
    return src_id, tgt_id, [edge]
