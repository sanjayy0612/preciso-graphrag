"""Workspace-local directed supply-chain relationship storage and queries."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from core.profiles import SUPPLY_CHAIN_PROFILE, resolve_dataset_profile, relationship_type
from core.utils import compute_mdhash_id, split_source_ids


DIRECTED_RELATIONSHIPS_KEY = "directed_relationships"
METADATA_KEY = "supply_chain_metadata"
COMMITS_KEY = "supply_chain_commits"
_SNAPSHOT_RECORD_ID = "snapshot"


def directed_relationship_id(src_id: str, rel_type: str, tgt_id: str) -> str:
    """Stable ID for an ordered, typed relationship identity."""
    return compute_mdhash_id(
        json.dumps([src_id, rel_type, tgt_id], ensure_ascii=False, separators=(",", ":")),
        prefix="supply-rel-",
    )


async def validate_snapshot_metadata(
    payload: dict[str, Any], metadata_storage: Any,
) -> list[str]:
    """Keep v1 supply-chain workspaces on one optional effective snapshot date."""
    snapshot_date = str(payload.get("snapshot_effective_date", "")).strip()
    if not snapshot_date:
        return []
    existing = await metadata_storage.get_by_id(_SNAPSHOT_RECORD_ID)
    dates = set((existing or {}).get("effective_dates", []))
    if dates and dates != {snapshot_date}:
        return [
            "supply-chain workspace already contains snapshot date(s) "
            f"{', '.join(sorted(dates))}; cannot mix `{snapshot_date}` without rebuilding the workspace"
        ]
    return []


async def begin_supply_chain_commit(commits_storage: Any, document_id: str) -> None:
    await commits_storage.upsert(
        {
            document_id: {
                "status": "pending",
                "document_id": document_id,
                "started_at": int(time.time()),
            }
        }
    )
    await commits_storage.index_done_callback()


async def finish_supply_chain_commit(commits_storage: Any, document_id: str) -> None:
    existing = await commits_storage.get_by_id(document_id) or {}
    await commits_storage.upsert(
        {
            document_id: {
                **{key: value for key, value in existing.items() if key not in {"_id", "create_time", "update_time"}},
                "status": "complete",
                "document_id": document_id,
                "completed_at": int(time.time()),
            }
        }
    )
    await commits_storage.index_done_callback()


async def fail_supply_chain_commit(commits_storage: Any, document_id: str, reason: str) -> None:
    existing = await commits_storage.get_by_id(document_id) or {}
    await commits_storage.upsert(
        {
            document_id: {
                **{key: value for key, value in existing.items() if key not in {"_id", "create_time", "update_time"}},
                "status": "failed",
                "document_id": document_id,
                "failed_at": int(time.time()),
                "failure_reason": reason,
            }
        }
    )
    await commits_storage.index_done_callback()


async def persist_directed_relationships(
    relationships: list[dict[str, Any]],
    *,
    document_id: str,
    directed_relationships_storage: Any,
) -> None:
    """Append evidence observations without overwriting prior relationship proof."""
    updates: dict[str, dict[str, Any]] = {}
    for relationship in relationships:
        src_id = str(relationship.get("src_id") or relationship.get("source_entity") or "").strip()
        tgt_id = str(relationship.get("tgt_id") or relationship.get("target_entity") or "").strip()
        rel_type = relationship_type(relationship)
        record_id = directed_relationship_id(src_id, rel_type, tgt_id)
        existing = updates.get(record_id) or await directed_relationships_storage.get_by_id(record_id) or {}
        evidence_by_id = {
            item["observation_id"]: item
            for item in existing.get("evidence", [])
            if isinstance(item, dict) and item.get("observation_id")
        }
        for source_id in split_source_ids([str(relationship.get("source_id", ""))]):
            observation = {
                "source_id": source_id,
                "description": str(relationship.get("description", "")).strip(),
                "file_path": str(relationship.get("file_path", "unknown_source")).strip() or "unknown_source",
                "document_id": document_id,
            }
            observation["observation_id"] = compute_mdhash_id(
                json.dumps(observation, ensure_ascii=False, sort_keys=True), prefix="supply-evidence-"
            )
            evidence_by_id.setdefault(observation["observation_id"], observation)
        updates[record_id] = {
            "src_id": src_id,
            "relationship_type": rel_type,
            "tgt_id": tgt_id,
            "evidence": sorted(
                evidence_by_id.values(),
                key=lambda item: (item["source_id"], item["document_id"], item["observation_id"]),
            ),
        }
    if updates:
        await directed_relationships_storage.upsert(updates)
        await directed_relationships_storage.index_done_callback()


async def persist_snapshot_metadata(
    payload: dict[str, Any], metadata_storage: Any,
) -> None:
    snapshot_date = str(payload.get("snapshot_effective_date", "")).strip()
    if not snapshot_date:
        return
    existing = await metadata_storage.get_by_id(_SNAPSHOT_RECORD_ID) or {}
    dates = set(existing.get("effective_dates", []))
    dates.add(snapshot_date)
    await metadata_storage.upsert(
        {
            _SNAPSHOT_RECORD_ID: {
                "effective_dates": sorted(dates),
                "document_ids": sorted(
                    set(existing.get("document_ids", [])) | {str(payload["document_id"])}
                ),
            }
        }
    )
    await metadata_storage.index_done_callback()


async def query_facility_unavailable(
    facility_id: str,
    storage_instances: dict[str, Any],
    global_config: dict[str, Any],
    *,
    max_paths: int = 100,
) -> dict[str, Any]:
    """Return documented FACILITY → COMPONENT → PRODUCT paths only."""
    graph = storage_instances.get("graph")
    workspace = str(getattr(graph, "workspace", "") or "")
    profile = resolve_dataset_profile(global_config, workspace)
    if profile.name != SUPPLY_CHAIN_PROFILE:
        return {
            "status": "profile_not_supported",
            "workspace": workspace,
            "message": "Facility-unavailable queries require the supply_chain workspace.",
        }
    if max_paths <= 0:
        return {
            "status": "invalid_request",
            "workspace": workspace,
            "message": "max_paths must be greater than zero.",
        }
    required_storage = (DIRECTED_RELATIONSHIPS_KEY, METADATA_KEY, COMMITS_KEY, "text_chunks")
    if any(storage_instances.get(key) is None for key in required_storage):
        return {
            "status": "inconsistent_storage",
            "workspace": workspace,
            "message": "Supply-chain sidecar storage is unavailable; reinitialize the workspace.",
        }

    facility_id = str(facility_id).strip()
    node = await graph.get_node(facility_id) if facility_id else None
    if node is None:
        return {
            "status": "unknown_facility",
            "workspace": workspace,
            "scenario": {"type": "facility_unavailable", "facility_id": facility_id, "hypothetical": True},
            "message": "The requested facility is not documented in this workspace.",
        }
    if str(node.get("entity_type", "")).upper() != "FACILITY":
        return {
            "status": "wrong_entity_type",
            "workspace": workspace,
            "scenario": {"type": "facility_unavailable", "facility_id": facility_id, "hypothetical": True},
            "message": "The requested entity is documented but is not a FACILITY.",
        }

    commits = await storage_instances[COMMITS_KEY].get_all_items()
    incomplete_documents = sorted(
        document_id
        for document_id, record in commits.items()
        if record.get("status") != "complete"
    )
    if incomplete_documents:
        return {
            "status": "inconsistent_storage",
            "workspace": workspace,
            "scenario": {"type": "facility_unavailable", "facility_id": facility_id, "hypothetical": True},
            "message": "Supply-chain ingestion is incomplete; reingest or repair before querying.",
            "incomplete_documents": incomplete_documents,
        }

    records = list((await storage_instances[DIRECTED_RELATIONSHIPS_KEY].get_all_items()).values())
    evidence_documents = {
        str(observation.get("document_id", ""))
        for record in records
        for observation in record.get("evidence", [])
        if isinstance(observation, dict) and observation.get("document_id")
    }
    uncommitted_evidence_documents = sorted(
        document_id
        for document_id in evidence_documents
        if commits.get(document_id, {}).get("status") != "complete"
    )
    if uncommitted_evidence_documents:
        return {
            "status": "inconsistent_storage",
            "workspace": workspace,
            "scenario": {"type": "facility_unavailable", "facility_id": facility_id, "hypothetical": True},
            "message": "Directed relationship evidence references an uncommitted document.",
            "incomplete_documents": uncommitted_evidence_documents,
        }
    manufacturing = sorted(
        (
            record
            for record in records
            if record.get("src_id") == facility_id and record.get("relationship_type") == "MANUFACTURES"
        ),
        key=lambda record: str(record.get("tgt_id", "")),
    )
    used_in_by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("relationship_type") == "USED_IN":
            used_in_by_component[str(record.get("src_id", ""))].append(record)
    for relations in used_in_by_component.values():
        relations.sort(key=lambda record: str(record.get("tgt_id", "")))

    raw_paths: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for manufactures in manufacturing:
        for used_in in used_in_by_component.get(str(manufactures.get("tgt_id", "")), []):
            raw_paths.append((manufactures, used_in))
    raw_paths.sort(key=lambda pair: (str(pair[1].get("tgt_id", "")), str(pair[0].get("tgt_id", ""))))

    if not raw_paths:
        return _base_response(
            "no_documented_paths",
            workspace,
            facility_id,
            await _snapshot_metadata(storage_instances[METADATA_KEY]),
            message="No documented component-to-product paths start at this facility. This does not prove no real-world exposure.",
            products=[],
            is_truncated=False,
            max_paths=max_paths,
        )

    selected_paths = raw_paths[:max_paths]
    try:
        rendered_paths = []
        for manufactures, used_in in selected_paths:
            rendered_paths.append(
                {
                    "nodes": [facility_id, manufactures["tgt_id"], used_in["tgt_id"]],
                    "edges": [
                        await _render_edge(manufactures, storage_instances["text_chunks"]),
                        await _render_edge(used_in, storage_instances["text_chunks"]),
                    ],
                }
            )
    except InconsistentEvidenceError as exc:
        response = _base_response(
            "inconsistent_evidence",
            workspace,
            facility_id,
            await _snapshot_metadata(storage_instances[METADATA_KEY]),
            message="Directed relationship evidence is missing or inconsistent; no partial paths were returned.",
            products=[],
            is_truncated=False,
            max_paths=max_paths,
        )
        response["missing_source_ids"] = exc.missing_source_ids
        return response
    products: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in rendered_paths:
        products[path["nodes"][-1]].append(path)
    summary = [
        {
            "product_id": product_id,
            "conclusion": "Potentially exposed through documented dependencies.",
            "paths": paths,
        }
        for product_id, paths in sorted(products.items())
    ]
    return _base_response(
        "success",
        workspace,
        facility_id,
        await _snapshot_metadata(storage_instances[METADATA_KEY]),
        message="Documented dependency paths found.",
        products=summary,
        is_truncated=len(raw_paths) > len(selected_paths),
        max_paths=max_paths,
    )


async def _render_edge(record: dict[str, Any], text_chunks: Any) -> dict[str, Any]:
    evidence = []
    missing_source_ids = []
    for observation in record.get("evidence", []):
        source_id = str(observation.get("source_id", ""))
        chunk = await text_chunks.get_by_id(source_id)
        if chunk is None:
            missing_source_ids.append(source_id)
            continue
        evidence.append(
            {
                "source_id": source_id,
                "file_path": observation.get("file_path", "unknown_source"),
                "description": observation.get("description", ""),
                "chunk": {"chunk_id": source_id, "content": chunk.get("content", "")},
            }
        )
    if missing_source_ids or not evidence:
        raise InconsistentEvidenceError(missing_source_ids or ["missing directed relationship evidence"])
    return {
        "src_id": record["src_id"],
        "relationship_type": record["relationship_type"],
        "tgt_id": record["tgt_id"],
        "evidence": evidence,
    }


async def _snapshot_metadata(metadata_storage: Any) -> dict[str, Any]:
    record = await metadata_storage.get_by_id(_SNAPSHOT_RECORD_ID) or {}
    return {
        "effective_dates": sorted(record.get("effective_dates", [])),
        "document_ids": sorted(record.get("document_ids", [])),
    }


def _base_response(
    status: str,
    workspace: str,
    facility_id: str,
    snapshot: dict[str, Any],
    *,
    message: str,
    products: list[dict[str, Any]],
    is_truncated: bool,
    max_paths: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "workspace": workspace,
        "snapshot": snapshot,
        "scenario": {"type": "facility_unavailable", "facility_id": facility_id, "hypothetical": True},
        "message": message,
        "potentially_exposed_products": products,
        "completeness": {"is_truncated": is_truncated, "max_paths": max_paths},
        "limitations": [
            "Results are potential exposure through documented dependencies only.",
            "Results do not establish delay, severity, inventory shortage, capacity, or business impact.",
        ],
    }


class InconsistentEvidenceError(RuntimeError):
    def __init__(self, missing_source_ids: list[str]):
        super().__init__("Missing or inconsistent directed relationship evidence")
        self.missing_source_ids = sorted(set(missing_source_ids))
