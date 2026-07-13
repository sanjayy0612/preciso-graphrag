from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
MCP_DIR = Path(__file__).resolve().parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from config import build_default_embedding_func, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.query import kg_query
from core.runtime_status import update_artifact_manifest
from core.storage.base import QueryParam
from core.utils import BasicTokenizer, logger
from ingest.pipeline import ingest_extracted_json
from mcp.server.fastmcp import FastMCP
from tools.export_tool import export_to_neo4j, export_to_qdrant
from tools.ingest_from_file_tool import ingest_from_file, reingest_from_file
from tools.reconcile_tool import ingest_with_reconciliation
from tools.status_tool import get_server_status


tokenizer = BasicTokenizer()
global_config = build_global_config(
    working_dir="GRAPH_IS_HERE",  # Custom folder for graph storage
    tokenizer=tokenizer,
    embedding_func=None,  # populated in initialize_runtime(); building it may probe Ollama
)
# Populated by initialize_runtime(). Must stay the same dict object: every tool
# closure below captures this name, so it is mutated in place, never rebound.
storage_instances: dict = {}
mcp = FastMCP("graphrag-mcp")


def initialize_runtime() -> None:
    """Build the embedding function and storage instances.

    Kept out of module import: build_default_embedding_func() may hit the
    network to probe the embedding dimension, and the vector stores consume
    that dimension (and touch the working dir) at construction. Importing
    this module must perform no network or storage I/O.
    """
    if storage_instances:
        return
    global_config["embedding_func"] = build_default_embedding_func()
    storage_instances.update(build_storage_instances(global_config))


@mcp.tool(
    name="get_server_status",
    description="Return the current MCP runtime status and local graph health summary.",
)
async def get_server_status_tool() -> dict:
    return await get_server_status(storage_instances, global_config)


@mcp.tool(
    name="export_graph_to_neo4j",
    description="Export the local NetworkX graph artifact in GRAPH_IS_HERE to Neo4j as workspace-scoped nodes and relationships.",
)
async def export_graph_to_neo4j_tool(
    uri: str | None = None,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict:
    return await export_to_neo4j(
        storage_instances,
        uri=uri,
        username=username,
        password=password,
        database=database,
        workspace=workspace,
        clear_existing=clear_existing,
    )


@mcp.tool(
    name="export_vectors_to_qdrant",
    description="Export the local vector artifacts in GRAPH_IS_HERE to Qdrant collections for entities, relationships, and chunks.",
)
async def export_vectors_to_qdrant_tool(
    url: str | None = None,
    api_key: str | None = None,
    collection_prefix: str | None = None,
    workspace: str | None = None,
    clear_existing: bool = False,
) -> dict:
    return await export_to_qdrant(
        storage_instances,
        global_config,
        url=url,
        api_key=api_key,
        collection_prefix=collection_prefix,
        workspace=workspace,
        clear_existing=clear_existing,
    )


@mcp.tool()
async def ingest_graph_tool(payload: dict) -> dict:
    """
    TOOL: ingest_graph_tool
    
    PURPOSE: Ingest extraction JSON payload directly into knowledge graph (in-memory, no file read)
    
    INPUT:
        payload (dict): Extraction JSON with this structure:
            {
                "document_id": "unique_id",
                "entities": [{"entity_name": "APPLE", "entity_type": "company", ...}],
                "relationships": [{"src_id": "APPLE", "tgt_id": "MICROSOFT", ...}],
                "chunks": [{"chunk_id": "...", "content": "original text", ...}]
            }
    
    PROCESS:
        1. Validates payload structure
        2. Calls ingest_extracted_json() → ingest/pipeline.py
        3. Transforms entities → nodes, relationships → edges
        4. Merges with existing graph (if entities already exist)
        5. Stores in vector DB for semantic search
    
    OUTPUT:
        {
            "status": "success" | "error" | "validation_failed",
            "entities_added": int,
            "relationships_added": int,
            "chunks_stored": int,
            "message": "status message"
        }
    
    WHEN TO USE:
        - Agent extracts document and passes JSON directly (not from file)
        - Quick in-memory ingestion without file I/O
    """
    return await ingest_extracted_json(payload, storage_instances, global_config)


@mcp.tool(
    name="ingest_from_file",
    description="Read agent extraction JSON from disk and ingest into knowledge graph",
)
async def ingest_from_file_tool(file_path: str) -> dict:
    """
    TOOL: ingest_from_file
    
    PURPOSE: Read extraction JSON file from disk and ingest it into knowledge graph
    
    INPUT:
        file_path (str): Path to extraction file
            Supported formats:
              - extractions/{filename}_extracted.json  (JSON format)
              - extractions/{filename}_extracted.md     (Markdown format)
              - extractions/{filename}_extracted.txt    (Text format)
    
    PROCESS:
        1. Resolves file path (handles relative/absolute paths)
        2. Checks if file exists
        3. Reads and parses based on file extension:
           - .json → parsed as JSON directly
           - .md/.txt → parsed via parse_markdown_extraction()
        4. Validates payload structure
        5. Calls ingest_extracted_json() → ingest/pipeline.py
        6. Merges new data with existing graph
    
    OUTPUT:
        {
            "status": "success" | "error" | "validation_failed",
            "file_path": "path/to/file",
            "entities_added": int,
            "relationships_added": int,
            "chunks_stored": int,
            "message": "status message"
        }
    
    WHEN TO USE:
        - After agent extraction (extraction file is written to disk)
        - You want to manually control when ingestion happens
        - File-based workflow for audit trail
    
    RELATED:
        → Used after agent calls: ingest_from_file("extractions/document_extracted.json")
        → Calls: ingest/pipeline.py → ingest_extracted_json()
    """
    return await ingest_from_file(file_path, storage_instances, global_config)


@mcp.tool(
    name="reingest_from_file",
    description="Re-run ingestion on existing extraction file. Use when pipeline failed but extraction is intact.",
)
async def reingest_from_file_tool(file_path: str) -> dict:
    """
    TOOL: reingest_from_file
    
    PURPOSE: Re-ingest an already-extracted file WITHOUT re-running extraction
    Useful for debugging/recovering from ingestion failures
    
    INPUT:
        file_path (str): Path to extraction file (same as ingest_from_file)
    
    PROCESS:
        1. Reads extraction file from disk
        2. Skips "already ingested" checks
        3. Replays entire ingestion pipeline:
           - Validation
           - Transformation (entities → nodes, relations → edges)
           - Merging with existing graph
           - Vector DB updates
    
    OUTPUT:
        {
            "status": "success" | "error",
            "file_path": "path/to/file",
            "entities_added": int,
            "relationships_added": int,
            "chunks_stored": int,
            "message": "status message"
        }
    
    WHEN TO USE:
        - Extraction succeeded but ingestion failed (e.g., DB error, network issue)
        - Want to retry ingestion without re-calling LLM/agent
        - Saves API costs during development/debugging
        - File already exists: extractions/document_extracted.json
    
    WORKFLOW:
        Scenario: Agent extracted "document.md" → extractions/document_extracted.json
        But ingest_from_file() failed due to storage error.
        Now DB is fixed, so call: reingest_from_file("extractions/document_extracted.json")
        → Replays ingestion without asking agent again (saves $)
    
    DIFFERENCE FROM ingest_from_file:
        - ingest_from_file: Initial ingestion (default behavior)
        - reingest_from_file: Recovery/replay (same internal logic, different intent)
        Internally: Both call _ingest_file() with same logic
    """
    return await reingest_from_file(file_path, storage_instances, global_config)


@mcp.tool()
async def ingest_with_reconciliation_tool(extraction_files: list[str]) -> dict:
    """
    Reconcile multiple subagent extraction files and ingest
    as a single unified knowledge graph.

    Use this after spawning subagents on a large document.
    Pass the list of all subagent output file paths.

    Args:
        extraction_files: list of paths to subagent JSON extraction files

    Returns:
        status, unified_file path, counts of entities/relationships added,
        reconciliation stats showing how many duplicates were merged
    """
    return await ingest_with_reconciliation(
        extraction_files=extraction_files,
        storage_instances=storage_instances,
        global_config=global_config,
    )


@mcp.tool()
async def ingest_checkpoint_tool(payload: dict) -> dict:
    """
    TOOL: ingest_checkpoint_tool
    
    PURPOSE: Save checkpoint data during long-running ingestion tasks
    Allows resuming from saved state if process is interrupted
    
    INPUT:
        payload (dict): Any data to checkpoint
            Example:
            {
                "checkpoint_id": "batch_1_of_100",
                "processed_documents": ["doc1.md", "doc2.md"],
                "timestamp": 1621234567,
                "custom_data": {...}
            }
    
    PROCESS:
        1. Extracts or generates checkpoint_id (default: "checkpoint")
        2. Stores payload in KV storage ("checkpoints" namespace)
        3. Triggers index callback (makes data queryable)
        4. Returns success/error status
    
    OUTPUT:
        {
            "status": "success" | "error",
            "checkpoint_id": "batch_1_of_100",
            "message": "checkpoint saved" | "error message"
        }
    
    STORAGE:
        Stored in: storage_instances["checkpoints"] (JsonKVStorage)
        Location: graphrag_mcp_data/checkpoints/
        Persists across server restarts
    
    WHEN TO USE:
        - Batch processing many documents (save progress every N documents)
        - Long-running ingestion tasks that may timeout
        - Want to resume from last checkpoint if interrupted
        - Tracking ingestion progress across multiple calls
    
    WORKFLOW EXAMPLE:
        for batch_num in range(1, 101):  # 100 batches
            ingest_from_file(f"extractions/batch_{batch_num}.json")
            ingest_checkpoint_tool({
                "checkpoint_id": f"batch_{batch_num}",
                "processed": batch_num * 10
            })
        
        # If interrupted at batch 50, can resume from there
    """
    try:
        checkpoints = storage_instances["checkpoints"]
        checkpoint_id = str(payload.get("checkpoint_id") or "checkpoint")
        await checkpoints.upsert({checkpoint_id: {"payload": payload}})
        await checkpoints.index_done_callback()
        return {"status": "success", "checkpoint_id": checkpoint_id}
    except Exception as exc:
        logger.exception("ingest_checkpoint_tool failed")
        return {"status": "error", "message": str(exc)}


@mcp.tool()
async def query_graph_tool(
    query: str,
    mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"] = "mix",
) -> dict:
    """
    TOOL: query_graph_tool
    
    PURPOSE: Query the knowledge graph and retrieve relevant context with answer
    Main tool for end-users to ask questions about ingested documents
    
    INPUT:
        query (str): User question/query
            Examples:
              - "What is Apple's market cap?"
              - "List all financial metrics"
              - "How is Apple related to Microsoft?"
        
        mode (str): Query strategy (default: "mix")
            Options (must be one of exactly these six values):
              - "local"  → Entity-centric: low-level keywords → entity vector search → graph neighborhood
              - "global" → Relationship-centric: high-level keywords → relationship vector search
              - "hybrid" → local + global combined (comparison queries auto-upgrade to "global")
              - "mix"    → hybrid + direct chunk vector search (best for most cases)
              - "naive"  → Accepted for compatibility; currently retrieves the same as "hybrid"
              - "bypass" → Accepted for compatibility; currently retrieves the same as "hybrid"
    
    PROCESS:
        1. Extracts keywords from query
        2. Searches vector DB for similar entities/relationships/chunks
        3. Traverses graph to find connected entities
        4. Ranks and deduplicates results
        5. Assembles context from top matches
        6. Formats context and returns
        (LLM response generation skipped if llm_model_func is None)
    
    OUTPUT:
        {
            "status": "success" | "error",
            "message": "query complete" | "no results" | "error message",
            "content": "response text (if LLM enabled)",
            "raw_data": {
                "entities": [...],
                "relationships": [...],
                "text_chunks": [...],
                "references": [...]
            },
            "is_streaming": False
        }
    
    CONTEXT ASSEMBLY (what's included in response):
        1. Matched entities from vector search
        2. Relationships between matched entities
        3. Text chunks linked to entities (original document text)
        4. Reference list (file_path, source_id, chunk_id)
    
    WHEN TO USE:
        - End-user wants to search/query the knowledge graph
        - After ingest_from_file() has populated the graph
        - Get context for financial analysis, research, reporting
    
    WORKFLOW:
        1. Agent extracts documents → extractions/file.json
        2. ingest_from_file("extractions/file.json") → builds graph
        3. query_graph_tool("What companies are mentioned?") → retrieves answer
    
    INTERNAL FLOW:
        → Calls: core/query.py → kg_query()
        → Uses: storage_instances["graph"] (NetworkX graph)
        → Uses: storage_instances["entities_vdb"] (semantic search)
        → Uses: storage_instances["relationships_vdb"] (semantic search)
        → Uses: storage_instances["text_chunks"] (KV lookup)
    """
    try:
        result = await kg_query(
            query=query,
            knowledge_graph_inst=storage_instances["graph"],
            entities_vdb=storage_instances["entities_vdb"],
            relationships_vdb=storage_instances["relationships_vdb"],
            text_chunks_db=storage_instances["text_chunks"],
            query_param=QueryParam(mode=mode, include_references=True),
            global_config=global_config,
            hashing_kv=storage_instances.get("llm_cache"),
            chunks_vdb=storage_instances["chunks_vdb"],
        )
        if result is None:
            return {"status": "success", "message": "no results", "data": {}}
        return {
            "status": "success",
            "message": "query complete",
            "content": result.content,
            "raw_data": result.raw_data,
            "is_streaming": result.is_streaming,
        }
    except Exception as exc:
        logger.exception("query_graph_tool failed (mode=%s)", mode)
        return {"status": "error", "message": str(exc)}


async def startup() -> None:
    initialize_runtime()
    await initialize_storage_instances(storage_instances)
    await update_artifact_manifest(storage_instances, global_config)


if __name__ == "__main__":
    import asyncio

    asyncio.run(startup())
    mcp.run()
