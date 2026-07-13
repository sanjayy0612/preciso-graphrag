from __future__ import annotations

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from ingest.parser import parse_markdown_extraction
from ingest.pipeline import ingest_extracted_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_MARKDOWN_SUFFIXES = {".md", ".txt"}
SUPPORTED_JSON_SUFFIXES = {".json"}


async def ingest_from_file(file_path: str, storage_instances: dict, global_config: dict) -> dict:
    """
    Reads agent extraction output from disk and ingests into graph.

    Supports:
      .json files: parsed directly as JSON
      .md / .txt files: parsed via parse_markdown_extraction()

    Steps:
      1. Check file exists
      2. Read and parse based on extension
      3. Validate payload structure
      4. Call ingest_extracted_json pipeline
      5. Return result with file_path in response
    """

    return await _ingest_file(file_path, storage_instances, global_config)


async def reingest_from_file(file_path: str, storage_instances: dict, global_config: dict) -> dict:
    """
    Re-runs ingestion on an already-extracted file.
    Skips any 'already ingested' checks.
    Identical logic to ingest_from_file.
    Used when pipeline failed but extraction file is intact.
    """

    return await _ingest_file(file_path, storage_instances, global_config)


async def _ingest_file(file_path: str, storage_instances: dict, global_config: dict) -> dict:
    resolved_path = _resolve_file_path(file_path)
    if not resolved_path.exists() or not resolved_path.is_file():
        return {
            "status": "error",
            "file_path": str(file_path),
            "entities_added": 0,
            "relationships_added": 0,
            "chunks_stored": 0,
            "message": "file not found",
        }

    try:
        payload = _load_payload(resolved_path)
    except ValueError as exc:
        return {
            "status": "error",
            "file_path": str(file_path),
            "entities_added": 0,
            "relationships_added": 0,
            "chunks_stored": 0,
            "message": str(exc),
        }

    validation_errors = _validate_payload_structure(payload)
    if validation_errors:
        return {
            "status": "validation_failed",
            "file_path": str(file_path),
            "entities_added": 0,
            "relationships_added": 0,
            "chunks_stored": 0,
            "errors": validation_errors,
        }

    normalized_payload = _normalize_payload(payload, resolved_path)
    result = await ingest_extracted_json(normalized_payload, storage_instances, global_config)

    response = {
        "status": result.get("status", "error"),
        "file_path": str(file_path),
        "entities_added": int(result.get("entities_merged", 0) or 0),
        "relationships_added": int(result.get("relationships_merged", 0) or 0),
        "chunks_stored": int(result.get("chunks_ingested", 0) or 0),
    }
    if "message" in result:
        response["message"] = result["message"]
    if result.get("summary_events"):
        response["summary_events"] = result["summary_events"]
    if result.get("errors"):
        response["errors"] = result["errors"]

    if result.get("status") == "partial_success":
        response["status"] = "validation_failed"
    elif result.get("status") == "error":
        response["message"] = result.get("message", "pipeline error")

    return response


def _resolve_file_path(file_path: str) -> Path:
    candidate = Path(file_path).expanduser()
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _load_payload(resolved_path: Path) -> dict[str, Any]:
    suffix = resolved_path.suffix.lower()
    content = resolved_path.read_text(encoding="utf-8")

    if suffix in SUPPORTED_JSON_SUFFIXES:
        try:
            payload = json.loads(content)
        except JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from exc
    elif suffix in SUPPORTED_MARKDOWN_SUFFIXES:
        payload = parse_markdown_extraction(content)
    else:
        raise ValueError(f"unsupported file extension: {suffix or '<none>'}")

    if not isinstance(payload, dict):
        raise ValueError("parsed payload must be a JSON object")
    return payload


def _validate_payload_structure(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
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


def _normalize_payload(payload: dict[str, Any], resolved_path: Path) -> dict[str, Any]:
    normalized = dict(payload)
    source_file = str(
        normalized.get("source_file")
        or normalized.get("file_path")
        or _infer_source_file_from_chunks(normalized.get("chunks"))
        or resolved_path.name
    )
    normalized["file_path"] = source_file

    normalized_chunks = []
    for index, chunk in enumerate(normalized.get("chunks", [])):
        if not isinstance(chunk, dict):
            normalized_chunks.append(chunk)
            continue
        normalized_chunk = dict(chunk)
        normalized_chunk.setdefault("chunk_id", f"chunk_{index + 1:03d}")
        normalized_chunk.setdefault("file_path", source_file)
        normalized_chunks.append(normalized_chunk)
    normalized["chunks"] = normalized_chunks

    normalized_entities = []
    for entity in normalized.get("entities", []):
        if not isinstance(entity, dict):
            normalized_entities.append(entity)
            continue
        normalized_entity = dict(entity)
        normalized_entity.setdefault("file_path", source_file)
        normalized_entities.append(normalized_entity)
    normalized["entities"] = normalized_entities

    normalized_relationships = []
    for relationship in normalized.get("relationships", []):
        if not isinstance(relationship, dict):
            normalized_relationships.append(relationship)
            continue
        normalized_relationship = dict(relationship)
        normalized_relationship.setdefault("file_path", source_file)
        normalized_relationships.append(normalized_relationship)
    normalized["relationships"] = normalized_relationships

    return normalized


def _infer_source_file_from_chunks(chunks: Any) -> str | None:
    if not isinstance(chunks, list):
        return None
    for chunk in chunks:
        if isinstance(chunk, dict):
            file_path = str(chunk.get("file_path", "")).strip()
            if file_path:
                return file_path
    return None
