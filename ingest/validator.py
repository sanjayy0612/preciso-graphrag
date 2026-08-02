from __future__ import annotations

from typing import Any

from config import GRAPH_FIELD_SEP


def validate_extraction_structure(payload: Any) -> list[str]:
    """Validate the required top-level shape shared by all ingestion APIs."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]

    for field in ("document_id", "entities", "relationships", "chunks"):
        if field not in payload:
            errors.append(f"missing required field `{field}`")

    if "document_id" in payload and not str(payload.get("document_id", "")).strip():
        errors.append("`document_id` must be a non-empty string")
    if "entities" in payload and not isinstance(payload.get("entities"), list):
        errors.append("`entities` must be a list")
    if "relationships" in payload and not isinstance(payload.get("relationships"), list):
        errors.append("`relationships` must be a list")
    if "chunks" in payload and not isinstance(payload.get("chunks"), list):
        errors.append("`chunks` must be a list")
    return errors


def _unresolvable_source_ids(source_id: str, resolvable_source_ids: set[str]) -> list[str]:
    return [
        chunk_id
        for chunk_id in source_id.split(GRAPH_FIELD_SEP)
        if chunk_id and chunk_id not in resolvable_source_ids
    ]


def validate_entity(
    entity: dict,
    *,
    resolvable_source_ids: set[str] | None = None,
    strict_source_ids: bool = False,
) -> tuple[bool, str]:
    if not isinstance(entity, dict):
        return False, "entity must be an object"
    entity_name = str(entity.get("entity_name", "")).strip()
    if not entity_name:
        return False, "entity_name is required"
    description = str(entity.get("description", "")).strip()
    if not description:
        return False, f"entity `{entity_name}` requires description"
    source_id = str(entity.get("source_id", "")).strip()
    if not source_id:
        return False, f"entity `{entity_name}` requires source_id"
    if strict_source_ids and resolvable_source_ids is not None:
        unresolved = _unresolvable_source_ids(source_id, resolvable_source_ids)
        if unresolved:
            return False, (
                f"entity `{entity_name}` has unresolvable source_id(s): "
                f"{', '.join(unresolved)}"
            )
    entity_type = str(entity.get("entity_type", "")).strip()
    if not entity_type:
        return False, f"entity `{entity_name}` requires entity_type"
    return True, ""


def validate_relationship(
    rel: dict,
    known_entities: set,
    *,
    resolvable_source_ids: set[str] | None = None,
    strict_source_ids: bool = False,
) -> tuple[bool, str]:
    if not isinstance(rel, dict):
        return False, "relationship must be an object"
    src_id = str(rel.get("src_id") or rel.get("source_entity") or "").strip()
    tgt_id = str(rel.get("tgt_id") or rel.get("target_entity") or "").strip()
    if not src_id or not tgt_id:
        return False, "relationship requires src_id/source_entity and tgt_id/target_entity"
    if src_id == tgt_id:
        return False, f"self-loop relationship is not allowed for `{src_id}`"
    if src_id not in known_entities or tgt_id not in known_entities:
        return False, f"relationship `{src_id}->{tgt_id}` references unknown entities"
    description = str(rel.get("description", "")).strip()
    if not description:
        return False, f"relationship `{src_id}->{tgt_id}` requires description"
    source_id = str(rel.get("source_id", "")).strip()
    if not source_id:
        return False, f"relationship `{src_id}->{tgt_id}` requires source_id"
    if strict_source_ids and resolvable_source_ids is not None:
        unresolved = _unresolvable_source_ids(source_id, resolvable_source_ids)
        if unresolved:
            return False, (
                f"relationship `{src_id}->{tgt_id}` has unresolvable source_id(s): "
                f"{', '.join(unresolved)}"
            )
    weight = rel.get("weight", 1.0)
    try:
        float(weight)
    except (TypeError, ValueError):
        return False, f"relationship `{src_id}->{tgt_id}` weight must be numeric"
    return True, ""
