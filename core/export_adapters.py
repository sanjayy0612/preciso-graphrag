from __future__ import annotations

import base64
import json
import os
import re
import uuid
import zlib
from typing import Any

import numpy as np

from config import GRAPH_FIELD_SEP
from core.utils import strip_summary_marker


def _sanitize_workspace(value: str | None) -> str:
    raw = (value or "").strip() or "default"
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_") or "default"


def _batch(items: list[dict[str, Any]], size: int = 200) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _coerce_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        coerced = [_coerce_scalar(item) for item in value]
        return [item for item in coerced if item is not None]
    return json.dumps(value, ensure_ascii=False)


def _split_graph_field(value: Any, separator: str = GRAPH_FIELD_SEP) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split(separator) if item.strip()]


def _split_keywords(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _node_properties(node: dict[str, Any], workspace: str) -> dict[str, Any]:
    entity_id = str(node.get("id", "")).strip()
    properties = {
        "workspace_id": workspace,
        "entity_id": entity_id,
        "entity_name": entity_id,
        "entity_type": str(node.get("entity_type", "UNKNOWN")).strip() or "UNKNOWN",
        "description": strip_summary_marker(str(node.get("description", "")).strip()),
        "source_id": str(node.get("source_id", "")).strip(),
        "source_ids": _split_graph_field(node.get("source_id")),
        "file_path": str(node.get("file_path", "")).strip(),
        "timestamp": int(float(node.get("timestamp", 0) or 0)),
    }
    return {key: _coerce_scalar(value) for key, value in properties.items() if value not in (None, "", [])}


def _edge_properties(edge: dict[str, Any], workspace: str) -> dict[str, Any]:
    src_id = str(edge.get("source", "")).strip()
    tgt_id = str(edge.get("target", "")).strip()
    properties = {
        "workspace_id": workspace,
        "edge_key": f"{src_id}->{tgt_id}",
        "src_id": src_id,
        "tgt_id": tgt_id,
        "description": strip_summary_marker(str(edge.get("description", "")).strip()),
        "keywords": str(edge.get("keywords", "")).strip(),
        "keywords_list": _split_keywords(edge.get("keywords")),
        "source_id": str(edge.get("source_id", "")).strip(),
        "source_ids": _split_graph_field(edge.get("source_id")),
        "file_path": str(edge.get("file_path", "")).strip(),
        "weight": float(edge.get("weight", 0.0) or 0.0),
        "timestamp": int(float(edge.get("timestamp", 0) or 0)),
    }
    return {key: _coerce_scalar(value) for key, value in properties.items() if value not in (None, "", [])}


async def export_graph_to_neo4j(
    storage_instances: dict,
    *,
    uri: str | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict[str, Any]:
    try:
        from neo4j import AsyncGraphDatabase
    except Exception as exc:
        return {
            "status": "error",
            "message": "Neo4j export requires the `neo4j` package. Reinstall dependencies with `pip install -r requirements.txt`.",
            "error": str(exc),
        }

    uri = uri or os.getenv("GRAPHRAG_NEO4J_URI") or os.getenv("NEO4J_URI")
    username = (
        username
        or os.getenv("GRAPHRAG_NEO4J_USERNAME")
        or os.getenv("NEO4J_USERNAME")
        or os.getenv("NEO4J_USER")
    )
    password = password or os.getenv("GRAPHRAG_NEO4J_PASSWORD") or os.getenv("NEO4J_PASSWORD")
    database = database or os.getenv("GRAPHRAG_NEO4J_DATABASE") or os.getenv("NEO4J_DATABASE") or "neo4j"
    workspace_id = _sanitize_workspace(
        workspace or os.getenv("GRAPHRAG_EXPORT_WORKSPACE") or os.getenv("NEO4J_WORKSPACE")
    )

    if not uri or not username or not password:
        return {
            "status": "error",
            "message": (
                "Neo4j export requires connection settings. Provide uri/username/password "
                "or set GRAPHRAG_NEO4J_URI, GRAPHRAG_NEO4J_USERNAME, and GRAPHRAG_NEO4J_PASSWORD."
            ),
        }

    graph_storage = storage_instances["graph"]
    nodes = await graph_storage.get_all_nodes()
    edges = await graph_storage.get_all_edges()

    node_rows = [
        {
            "workspace_id": workspace_id,
            "entity_id": str(node.get("id", "")).strip(),
            "properties": _node_properties(node, workspace_id),
        }
        for node in nodes
        if str(node.get("id", "")).strip()
    ]
    edge_rows = [
        {
            "workspace_id": workspace_id,
            "source": str(edge.get("source", "")).strip(),
            "target": str(edge.get("target", "")).strip(),
            "edge_key": f"{str(edge.get('source', '')).strip()}->{str(edge.get('target', '')).strip()}",
            "properties": _edge_properties(edge, workspace_id),
        }
        for edge in edges
        if str(edge.get("source", "")).strip() and str(edge.get("target", "")).strip()
    ]

    driver = AsyncGraphDatabase.driver(uri, auth=(username, password))
    try:
        async with driver.session(database=database) as session:
            if clear_existing:
                delete_result = await session.run(
                    "MATCH (n:PrecisoEntity {workspace_id: $workspace_id}) DETACH DELETE n",
                    workspace_id=workspace_id,
                )
                await delete_result.consume()

            for rows in _batch(node_rows):
                result = await session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (n:PrecisoEntity {workspace_id: row.workspace_id, entity_id: row.entity_id})
                    SET n += row.properties
                    """,
                    rows=rows,
                )
                await result.consume()

            for rows in _batch(edge_rows):
                result = await session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (src:PrecisoEntity {workspace_id: row.workspace_id, entity_id: row.source})
                    MATCH (tgt:PrecisoEntity {workspace_id: row.workspace_id, entity_id: row.target})
                    MERGE (src)-[r:PRECISO_RELATION {workspace_id: row.workspace_id, edge_key: row.edge_key}]->(tgt)
                    SET r += row.properties
                    """,
                    rows=rows,
                )
                await result.consume()
    except Exception as exc:
        return {
            "status": "error",
            "message": "Neo4j export failed.",
            "error": str(exc),
            "workspace": workspace_id,
            "database": database,
        }
    finally:
        await driver.close()

    return {
        "status": "success",
        "backend": "neo4j",
        "workspace": workspace_id,
        "database": database,
        "nodes_exported": len(node_rows),
        "relationships_exported": len(edge_rows),
        "message": "Local graph artifact exported to Neo4j.",
    }


def _decode_vector_record(record: dict[str, Any]) -> list[float] | None:
    vector = record.get("__vector__")
    if vector is not None:
        if hasattr(vector, "tolist"):
            return vector.tolist()
        return list(vector)
    encoded = record.get("vector")
    if not encoded:
        return None
    compressed = base64.b64decode(encoded)
    return (
        np.frombuffer(zlib.decompress(compressed), dtype=np.float16)
        .astype(np.float32)
        .tolist()
    )


async def _collect_local_vectors(storage) -> list[dict[str, Any]]:
    client = await storage._get_client()
    data = getattr(client, "_NanoVectorDB__storage", {}).get("data", [])
    return [dict(item) for item in data]


def _strip_marker_deep(value: Any) -> Any:
    if isinstance(value, str):
        return strip_summary_marker(value)
    if isinstance(value, list):
        return [_strip_marker_deep(item) for item in value]
    return value


def _vector_payload(record: dict[str, Any], namespace: str, workspace: str) -> dict[str, Any]:
    # Strip SUMMARY_MARKER from every value, not just known keys — VDB records are
    # copied wholesale, so any upstream slip would otherwise ship the marker to Qdrant.
    payload = {
        key: _strip_marker_deep(_coerce_scalar(value))
        for key, value in record.items()
        if key not in {"__id__", "__vector__", "__metrics__", "vector"}
    }
    payload["workspace_id"] = workspace
    payload["namespace"] = namespace
    payload["source"] = "preciso-graphrag"
    return payload


async def export_vectors_to_qdrant(
    storage_instances: dict,
    global_config: dict,
    *,
    url: str | None = None,
    api_key: str | None = None,
    collection_prefix: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict[str, Any]:
    try:
        from qdrant_client import QdrantClient, models
    except Exception as exc:
        return {
            "status": "error",
            "message": "Qdrant export requires the `qdrant-client` package. Reinstall dependencies with `pip install -r requirements.txt`.",
            "error": str(exc),
        }

    url = url or os.getenv("GRAPHRAG_QDRANT_URL") or os.getenv("QDRANT_URL")
    api_key = api_key or os.getenv("GRAPHRAG_QDRANT_API_KEY") or os.getenv("QDRANT_API_KEY")
    collection_prefix = _sanitize_workspace(
        collection_prefix or os.getenv("GRAPHRAG_QDRANT_COLLECTION_PREFIX") or "preciso"
    )
    workspace_id = _sanitize_workspace(
        workspace or os.getenv("GRAPHRAG_EXPORT_WORKSPACE") or os.getenv("QDRANT_WORKSPACE")
    )

    if not url:
        return {
            "status": "error",
            "message": "Qdrant export requires a URL. Provide url or set GRAPHRAG_QDRANT_URL / QDRANT_URL.",
        }

    client = QdrantClient(url=url, api_key=api_key)
    vector_size = int(global_config["embedding_func"].embedding_dim)
    collections_exported: list[dict[str, Any]] = []
    namespaces = {
        "entities_vdb": "entities",
        "relationships_vdb": "relationships",
        "chunks_vdb": "chunks",
    }

    try:
        for storage_key, namespace in namespaces.items():
            collection_name = f"{collection_prefix}_{workspace_id}_{namespace}"
            if not client.collection_exists(collection_name):
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=vector_size,
                        distance=models.Distance.COSINE,
                    ),
                )
            else:
                info = client.get_collection(collection_name)
                existing_size = info.config.params.vectors.size
                if existing_size != vector_size:
                    return {
                        "status": "error",
                        "message": (
                            f"Qdrant collection `{collection_name}` expects vectors of size {existing_size}, "
                            f"but local export uses {vector_size}."
                        ),
                        "collection": collection_name,
                    }

            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name="workspace_id",
                    field_schema=models.KeywordIndexParams(
                        type=models.KeywordIndexType.KEYWORD,
                        is_tenant=True,
                    ),
                )
            except Exception:
                pass

            if clear_existing:
                client.delete(
                    collection_name=collection_name,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="workspace_id",
                                    match=models.MatchValue(value=workspace_id),
                                )
                            ]
                        )
                    ),
                )

            local_records = await _collect_local_vectors(storage_instances[storage_key])
            points = []
            for record in local_records:
                vector = _decode_vector_record(record)
                record_id = str(record.get("__id__", "")).strip()
                if not record_id or vector is None:
                    continue
                points.append(
                    models.PointStruct(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}:{record_id}")),
                        vector=vector,
                        payload=_vector_payload(record, namespace, workspace_id),
                    )
                )

            if points:
                client.upsert(collection_name=collection_name, points=points, wait=True)

            collections_exported.append(
                {
                    "collection": collection_name,
                    "namespace": namespace,
                    "points_exported": len(points),
                }
            )
    except Exception as exc:
        return {
            "status": "error",
            "message": "Qdrant export failed.",
            "error": str(exc),
            "workspace": workspace_id,
        }
    finally:
        client.close()

    return {
        "status": "success",
        "backend": "qdrant",
        "workspace": workspace_id,
        "collections": collections_exported,
        "message": "Local vector artifacts exported to Qdrant.",
    }
