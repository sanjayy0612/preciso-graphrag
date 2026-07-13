#!/usr/bin/env python
"""
Manual reconciliation CLI script.
Test ingest_with_reconciliation without MCP client connectivity.

Usage:
    python reconcile_manual.py extractions/doc_extracted.json \
        extractions/doc_patch_entities.json \
        extractions/doc_patch_relationships.json \
        extractions/doc_patch_orphans.json

If no arguments are provided, this script will generate a base extraction
and patch files under extractions/ and reconcile those.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import build_default_embedding_func, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.utils import BasicTokenizer
from preciso_mcp.tools.reconcile_tool import ingest_with_reconciliation


def _write_demo_extractions() -> list[str]:
    demo_dir = Path("extractions")
    demo_dir.mkdir(exist_ok=True)

    base_path = demo_dir / "demo_doc_extracted.json"
    patch_entities_path = demo_dir / "demo_doc_patch_entities.json"
    patch_relationships_path = demo_dir / "demo_doc_patch_relationships.json"
    patch_orphans_path = demo_dir / "demo_doc_patch_orphans.json"

    base = {
        "document_id": "demo_doc",
        "file_path": "demo_doc.txt",
        "timestamp": int(time.time()),
        "entities": [
            {
                "entity_name": "Apple Inc.",
                "entity_type": "COMPANY",
                "description": "Apple Inc. is a technology company.",
                "source_id": "chunk_001",
                "file_path": "demo_doc.txt",
            },
            {
                "entity_name": "Tim Cook",
                "entity_type": "PERSON",
                "description": "Tim Cook is the CEO of Apple Inc.",
                "source_id": "chunk_001",
                "file_path": "demo_doc.txt",
            },
            {
                "entity_name": "Apple Incorporated",
                "entity_type": "COMPANY",
                "description": "Apple Incorporated reports net sales growth.",
                "source_id": "chunk_002",
                "file_path": "demo_doc.txt",
            },
            {
                "entity_name": "Net Sales FY2024",
                "entity_type": "FINANCIAL_METRIC",
                "description": "Net sales increased 2 percent in FY2024.",
                "source_id": "chunk_002",
                "file_path": "demo_doc.txt",
            },
        ],
        "relationships": [
            {
                "src_id": "Apple Inc.",
                "tgt_id": "Tim Cook",
                "description": "Apple Inc. employs Tim Cook as CEO.",
                "keywords": "EMPLOYS,role=CEO",
                "source_id": "chunk_001",
                "weight": 1.0,
                "file_path": "demo_doc.txt",
            },
            {
                "src_id": "Apple Incorporated",
                "tgt_id": "Net Sales FY2024",
                "description": "Apple Incorporated reported net sales growth for FY2024.",
                "keywords": "REPORTED_METRIC,period=FY2024",
                "source_id": "chunk_002",
                "weight": 1.0,
                "file_path": "demo_doc.txt",
            }
        ],
        "chunks": [
            {
                "chunk_id": "chunk_002",
                "content": "Apple Incorporated reported net sales growth in FY2024.",
                "chunk_order_index": 2,
                "file_path": "demo_doc.txt",
            }
        ],
    }

    patch_entities = {
        "merge_entities": [
            {
                "keep": "Apple Inc.",
                "remove": ["Apple Incorporated"],
                "reason": "same company, variant names",
            }
        ]
    }

    patch_relationships = {
        "remove_relationships": [],
        "flag_conflicts": [],
    }

    patch_orphans = {
        "broken_relationships": [],
        "suggested_fixes": [],
    }

    base_path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    patch_entities_path.write_text(json.dumps(patch_entities, indent=2), encoding="utf-8")
    patch_relationships_path.write_text(json.dumps(patch_relationships, indent=2), encoding="utf-8")
    patch_orphans_path.write_text(json.dumps(patch_orphans, indent=2), encoding="utf-8")

    return [
        str(base_path),
        str(patch_entities_path),
        str(patch_relationships_path),
        str(patch_orphans_path),
    ]


async def reconcile_manual(file_paths: list[str]) -> None:
    print("Initializing config and storage...")
    tokenizer = BasicTokenizer()
    global_config = build_global_config(
        working_dir="GRAPH_IS_HERE",
        tokenizer=tokenizer,
        embedding_func=build_default_embedding_func(),
    )
    storage_instances = build_storage_instances(global_config)
    await initialize_storage_instances(storage_instances)

    print("\nReconciling and ingesting...")
    result = await ingest_with_reconciliation(
        extraction_files=file_paths,
        storage_instances=storage_instances,
        global_config=global_config,
    )

    print("\nReconciliation Result:")
    print(f"  Status: {result.get('status', 'unknown')}")
    if "message" in result:
        print(f"  Message: {result['message']}")
    if "unified_file" in result:
        print(f"  Unified File: {result['unified_file']}")
    print(f"  Files Reconciled: {result.get('files_reconciled', 0)}")
    print(f"  Duplicate Entities Merged: {result.get('duplicate_entities_merged', 0)}")
    print(f"  Entities Added: {result.get('entities_added', 0)}")
    print(f"  Relationships Added: {result.get('relationships_added', 0)}")
    print(f"  Chunks Stored: {result.get('chunks_stored', 0)}")

    stats = result.get("reconciliation_stats", {})
    if stats:
        print("  Reconciliation Stats:")
        print(json.dumps(stats, indent=2))

    errors = result.get("errors", []) or []
    if errors:
        print("  Errors:")
        for error in errors:
            print(f"    - {error}")

    if result.get("status") != "success":
        sys.exit(1)

    print("\nGraph is ready in GRAPH_IS_HERE/")
    print("Next: python query_manual.py \"What is Tim Cook's role?\"")


def main() -> None:
    file_paths = sys.argv[1:]
    if not file_paths:
        print("No extraction files provided. Creating demo extraction + patch files...")
        file_paths = _write_demo_extractions()
        print("Demo files:")
        for path in file_paths:
            print(f"  - {path}")

    for path in file_paths:
        if not Path(path).exists():
            print(f"File not found: {path}")
            sys.exit(1)

    asyncio.run(reconcile_manual(file_paths))


if __name__ == "__main__":
    main()
