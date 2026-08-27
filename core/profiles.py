"""Dataset-profile contracts for domain-specific ingestion.

Profiles are selected by the storage workspace, never by an extraction payload.
That keeps a payload intended for a permissive corpus from weakening the
validation contract of the supply-chain workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import GRAPH_FIELD_SEP


GENERIC_PROFILE = "generic"
SUPPLY_CHAIN_PROFILE = "supply_chain"
SUPPLY_CHAIN_WORKSPACE = "supply_chain"


@dataclass(frozen=True)
class DatasetProfile:
    name: str
    strict_source_ids: bool = False
    allowed_entity_types: frozenset[str] | None = None
    relationship_rules: dict[str, tuple[str, str]] | None = None


PROFILES = {
    GENERIC_PROFILE: DatasetProfile(name=GENERIC_PROFILE),
    SUPPLY_CHAIN_PROFILE: DatasetProfile(
        name=SUPPLY_CHAIN_PROFILE,
        strict_source_ids=True,
        allowed_entity_types=frozenset({"COMPANY", "FACILITY", "COMPONENT", "PRODUCT"}),
        relationship_rules={
            "OPERATES": ("COMPANY", "FACILITY"),
            "MANUFACTURES": ("FACILITY", "COMPONENT"),
            "USED_IN": ("COMPONENT", "PRODUCT"),
        },
    ),
}


def resolve_dataset_profile(global_config: dict[str, Any], workspace: str | None) -> DatasetProfile:
    """Resolve a profile from trusted runtime configuration and workspace."""
    workspace_name = (workspace or "").strip()
    workspace_profiles = global_config.get("workspace_profiles", {}) or {}
    profile_name = workspace_profiles.get(workspace_name, global_config.get("dataset_profile", GENERIC_PROFILE))
    try:
        return PROFILES[str(profile_name)]
    except KeyError as exc:
        raise ValueError(f"Unknown dataset profile `{profile_name}` for workspace `{workspace_name or 'default'}`") from exc


def relationship_type(record: dict[str, Any]) -> str:
    """Return the controlled relationship type from the first keyword token."""
    keywords = record.get("keywords", "")
    if not isinstance(keywords, str):
        return ""
    return keywords.split(",", 1)[0].strip().upper()


def validate_profile_records(
    profile: DatasetProfile,
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    resolvable_source_ids: set[str],
) -> list[str]:
    """Validate profile-specific types, typed endpoints, and strict evidence.

    Generic validation remains intentionally permissive.  A strict profile is
    preflighted before chunk/vector/graph writes so a rejected supply-chain
    payload cannot leave partial artifacts behind.
    """
    if profile.name == GENERIC_PROFILE:
        return []

    errors: list[str] = []
    entity_types: dict[str, str] = {}
    for index, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("entity_name", "")).strip()
        entity_type = str(entity.get("entity_type", "")).strip().upper()
        if entity_type not in (profile.allowed_entity_types or frozenset()):
            errors.append(
                f"profile `{profile.name}` rejects entity `{entity_name or index}` type `{entity_type or 'missing'}`"
            )
        if entity_name:
            prior_type = entity_types.get(entity_name)
            if prior_type is not None and prior_type != entity_type:
                errors.append(
                    f"profile `{profile.name}` has conflicting types for entity `{entity_name}`: `{prior_type}` and `{entity_type}`"
                )
            entity_types[entity_name] = entity_type
        _validate_sources(
            errors,
            profile,
            f"entity `{entity_name or index}`",
            str(entity.get("source_id", "")).strip(),
            resolvable_source_ids,
        )

    for index, relationship in enumerate(relationships):
        if not isinstance(relationship, dict):
            continue
        src_id = str(relationship.get("src_id") or relationship.get("source_entity") or "").strip()
        tgt_id = str(relationship.get("tgt_id") or relationship.get("target_entity") or "").strip()
        rel_type = relationship_type(relationship)
        expected_types = (profile.relationship_rules or {}).get(rel_type)
        if expected_types is None:
            errors.append(
                f"profile `{profile.name}` rejects relationship `{src_id or index}->{tgt_id or index}` type `{rel_type or 'missing'}`"
            )
        elif (entity_types.get(src_id), entity_types.get(tgt_id)) != expected_types:
            errors.append(
                f"profile `{profile.name}` requires `{rel_type}` to connect "
                f"{expected_types[0]} -> {expected_types[1]}`, got "
                f"{entity_types.get(src_id, 'missing')} -> {entity_types.get(tgt_id, 'missing')}`"
            )
        _validate_sources(
            errors,
            profile,
            f"relationship `{src_id or index}->{tgt_id or index}`",
            str(relationship.get("source_id", "")).strip(),
            resolvable_source_ids,
        )
    return errors


def _validate_sources(
    errors: list[str],
    profile: DatasetProfile,
    record_name: str,
    source_id: str,
    resolvable_source_ids: set[str],
) -> None:
    source_ids = [source for source in source_id.split(GRAPH_FIELD_SEP) if source]
    if not source_ids:
        errors.append(f"profile `{profile.name}` requires evidence for {record_name}")
        return
    unresolved = [source for source in source_ids if source not in resolvable_source_ids]
    if unresolved:
        errors.append(
            f"profile `{profile.name}` rejects {record_name} with unresolvable source_id(s): {', '.join(unresolved)}"
        )
