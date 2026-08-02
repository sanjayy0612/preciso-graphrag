"""
reconciler.py

Reconciles multiple subagent extraction outputs into a single
unified extraction dict ready for ingestion.

Called by ingest_with_reconciliation MCP tool.
"""

import re
import time


def _normalize(name: str) -> str:
    """
    Normalize entity name for comparison.
    Uppercase, remove punctuation, collapse spaces.
    """
    name = name.upper().strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name)
    return name


def collect_all_entities(extraction_list: list[dict]) -> list[dict]:
    """
    Flatten all entities from all subagent extractions into one list.
    Tag each with _source_file for traceability.
    """
    all_entities = []
    for extraction in extraction_list:
        source_file = extraction.get("file_path", "unknown")
        for entity in extraction.get("entities", []):
            entity = dict(entity)
            entity["_source_file"] = source_file
            all_entities.append(entity)
    return all_entities


def _relationship_key(rel: dict) -> tuple[str, str, str]:
    first_keyword = rel.get("keywords", "").split(",")[0].strip()
    return (rel.get("src_id", ""), rel.get("tgt_id", ""), first_keyword)


def _coerce_relationship_key(rel_key: object) -> tuple[str, str, str] | None:
    if isinstance(rel_key, dict):
        src_id = rel_key.get("src_id")
        tgt_id = rel_key.get("tgt_id")
        keyword = (rel_key.get("keyword") or rel_key.get("keywords") or "").split(",")[0].strip()
        if src_id and tgt_id:
            return (src_id, tgt_id, keyword)
        return None
    if isinstance(rel_key, (list, tuple)) and len(rel_key) >= 3:
        src_id, tgt_id, keyword = rel_key[0], rel_key[1], rel_key[2]
        if src_id and tgt_id:
            return (str(src_id), str(tgt_id), str(keyword))
    return None


def apply_patches(extraction: dict, patch_list: list[dict]) -> dict:
    """
    Apply reconciliation patch files to a single extraction payload.

    Patch keys supported:
      - merge_entities: [{"keep": "CANON", "remove": ["VARIANT", ...], "reason": "..."}]
      - remove_relationships: list of dicts or tuples (src_id, tgt_id, keyword)
      - flag_conflicts: list of conflict objects to attach for review
      - broken_relationships: list of dicts or tuples (src_id, tgt_id, keyword)
    """
    patched = dict(extraction)
    rename_map: dict[str, str] = {}
    remove_keys: set[tuple[str, str, str]] = set()
    conflicts: list[dict] = []

    for patch in patch_list:
        for merge in patch.get("merge_entities", []) or []:
            canonical = merge.get("keep")
            if not canonical:
                continue
            for variant in merge.get("remove", []) or []:
                if variant and variant != canonical:
                    rename_map[variant] = canonical

        for rel_key in patch.get("remove_relationships", []) or []:
            coerced = _coerce_relationship_key(rel_key)
            if coerced:
                remove_keys.add(coerced)

        for rel_key in patch.get("broken_relationships", []) or []:
            coerced = _coerce_relationship_key(rel_key)
            if coerced:
                remove_keys.add(coerced)

        conflicts.extend(patch.get("flag_conflicts", []) or [])

    entity_count_before = len(patched.get("entities", []) or [])
    relationship_count_before = len(patched.get("relationships", []) or [])

    entity_map: dict[str, dict] = {}
    for entity in patched.get("entities", []) or []:
        entity = dict(entity)
        name = rename_map.get(entity.get("entity_name"), entity.get("entity_name"))
        entity["entity_name"] = name
        if not name:
            continue
        existing = entity_map.get(name)
        if not existing:
            entity_map[name] = entity
            continue
        if len(entity.get("description", "")) > len(existing.get("description", "")):
            entity_map[name] = entity

    updated_relationships = []
    for rel in patched.get("relationships", []) or []:
        rel = dict(rel)
        rel["src_id"] = rename_map.get(rel.get("src_id"), rel.get("src_id"))
        rel["tgt_id"] = rename_map.get(rel.get("tgt_id"), rel.get("tgt_id"))
        key = _relationship_key(rel)
        if key in remove_keys:
            continue
        updated_relationships.append(rel)

    patched["entities"] = list(entity_map.values())
    patched["relationships"] = updated_relationships
    patched["_reconciliation_stats_override"] = {
        "total_entities_before": entity_count_before,
        "total_relationships_before": relationship_count_before,
    }
    if conflicts:
        patched["_conflicts"] = conflicts

    return patched


def merge_into_unified(
    updated_extractions: list[dict],
    document_id: str,
) -> dict:
    """
    Merge all extractions into one unified dict after canonical names applied.

    Deduplication rules:
      Entities:      same entity_name -> keep longest description
      Relationships: same (src_id, tgt_id, first_keyword) -> merge descriptions, keep max weight
      Chunks:        all kept, no deduplication (chunks are evidence)
    """
    # Entities: keep longest description per canonical name
    entity_map = {}
    conflicts: list[dict] = []
    total_entities_before = 0
    total_relationships_before = 0
    for extraction in updated_extractions:
        conflicts.extend(extraction.get("_conflicts", []) or [])
        override = extraction.get("_reconciliation_stats_override", {}) or {}
        total_entities_before += override.get(
            "total_entities_before", len(extraction.get("entities", []))
        )
        total_relationships_before += override.get(
            "total_relationships_before", len(extraction.get("relationships", []))
        )
        for entity in extraction.get("entities", []):
            name = entity["entity_name"]
            if name not in entity_map:
                entity_map[name] = dict(entity)
            else:
                existing_len = len(entity_map[name].get("description", ""))
                new_len = len(entity.get("description", ""))
                if new_len > existing_len:
                    entity_map[name] = dict(entity)

    # Relationships: deduplicate by (src, tgt, first keyword)
    rel_map = {}
    total_rels_before = total_relationships_before
    for extraction in updated_extractions:
        for rel in extraction.get("relationships", []):
            first_keyword = rel.get("keywords", "").split(",")[0].strip()
            key = (rel["src_id"], rel["tgt_id"], first_keyword)

            if key not in rel_map:
                rel_map[key] = dict(rel)
            else:
                existing_desc = rel_map[key].get("description", "")
                new_desc = rel.get("description", "")
                if new_desc and new_desc not in existing_desc:
                    rel_map[key]["description"] = f"{existing_desc} {new_desc}".strip()
                rel_map[key]["weight"] = max(
                    rel_map[key].get("weight", 1.0),
                    rel.get("weight", 1.0),
                )

    # Chunks: collect all, deduplicate by chunk_id only
    all_chunks = []
    seen_chunk_ids = set()
    for extraction in updated_extractions:
        for chunk in extraction.get("chunks", []):
            cid = chunk.get("chunk_id")
            if cid and cid not in seen_chunk_ids:
                all_chunks.append(chunk)
                seen_chunk_ids.add(cid)

    final_entities = [
        {key: value for key, value in entity.items() if not key.startswith("_")}
        for entity in entity_map.values()
    ]

    unified = {
        "document_id": document_id,
        "file_path": updated_extractions[0].get("file_path", "unknown")
        if updated_extractions
        else "unknown",
        "timestamp": int(time.time()),
        "entities": final_entities,
        "relationships": list(rel_map.values()),
        "chunks": all_chunks,
        "_reconciliation_stats": {
            "input_files": len(updated_extractions),
            "total_entities_before": total_entities_before,
            "total_entities_after": len(entity_map),
            "total_relationships_before": total_rels_before,
            "total_relationships_after": len(rel_map),
            "total_chunks": len(all_chunks),
        },
    }

    if conflicts:
        unified["_conflicts"] = conflicts

    return unified


def reconcile_extractions(
    base_extraction: dict,
    patch_list: list[dict],
    *,
    document_id: str | None = None,
) -> dict:
    """
    Main entry point. Called by ingest_with_reconciliation MCP tool.

        Steps:
            1. Apply reconciliation patches to the base extraction
            2. Merge into a unified extraction (dedupe + stats)
    """
    patched = apply_patches(base_extraction, patch_list or [])
    final_document_id = document_id or patched.get("document_id", "unknown")
    return merge_into_unified([patched], final_document_id)
