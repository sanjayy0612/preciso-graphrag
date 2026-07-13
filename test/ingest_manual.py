#!/usr/bin/env python
"""
Manual ingestion CLI script.
Test extraction -> graph creation without depending on MCP client connectivity.

Usage:
    python ingest_manual.py extractions/apple_10k_excerpt_extracted.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import build_default_embedding_func, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.utils import BasicTokenizer
from ingest.parser import parse_markdown_extraction
from preciso_mcp.tools.ingest_from_file_tool import ingest_from_file


def load_extraction_summary(file_path: Path) -> dict:
    content = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() == ".json":
        payload = json.loads(content)
    else:
        payload = parse_markdown_extraction(content)
    if not isinstance(payload, dict):
        raise ValueError("extraction file must parse to a JSON object")
    return {
        "document_id": payload.get("document_id", "unknown"),
        "entities": len(payload.get("entities", []) or []),
        "relationships": len(payload.get("relationships", []) or []),
        "chunks": len(payload.get("chunks", []) or []),
        "source_file": payload.get("source_file") or payload.get("file_path") or "unknown_source",
    }


async def ingest_file_manual(file_path: str) -> None:
    extraction_file = Path(file_path)
    if not extraction_file.exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    print(f"Reading extraction file: {file_path}")
    try:
        summary = load_extraction_summary(extraction_file)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to read extraction file: {exc}")
        sys.exit(1)

    print("Initializing config and storage...")
    tokenizer = BasicTokenizer()
    global_config = build_global_config(
        working_dir="GRAPH_IS_HERE",
        tokenizer=tokenizer,
        embedding_func=build_default_embedding_func(),
    )
    storage_instances = build_storage_instances(global_config)
    await initialize_storage_instances(storage_instances)

    print("\nExtraction Summary:")
    print(f"  Document ID: {summary['document_id']}")
    print(f"  Source File: {summary['source_file']}")
    print(f"  Entities: {summary['entities']}")
    print(f"  Relationships: {summary['relationships']}")
    print(f"  Chunks: {summary['chunks']}")

    print("\nIngesting into graph via ingest_from_file...")
    result = await ingest_from_file(file_path, storage_instances, global_config)

    print("\nIngestion Result:")
    print(f"  Status: {result.get('status', 'unknown')}")
    print(f"  Entities Added: {result.get('entities_added', 0)}")
    print(f"  Relationships Added: {result.get('relationships_added', 0)}")
    print(f"  Chunks Stored: {result.get('chunks_stored', 0)}")
    if "message" in result:
        print(f"  Message: {result['message']}")
    if "errors" in result and result["errors"]:
        print("  Errors:")
        for error in result["errors"]:
            print(f"    - {error}")

    if result.get("status") == "success":
        print("\nGraph is ready in GRAPH_IS_HERE/")
        print(f"Location: {Path(global_config['working_dir']).resolve()}")
        print('\nNext: python query_manual.py "What is Tim Cook\'s role?"')
        return

    sys.exit(1)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python ingest_manual.py <extraction_file_path>")
        print("Example: python ingest_manual.py extractions/apple_10k_excerpt_extracted.json")
        sys.exit(1)

    asyncio.run(ingest_file_manual(sys.argv[1]))


if __name__ == "__main__":
    main()
