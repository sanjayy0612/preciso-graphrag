from __future__ import annotations

import asyncio
import os
from typing import Literal

# Run as `python -m preciso_mcp.server` from the repo root, or after
# `pip install -e .` — never via sys.path manipulation. `mcp` below is the
# MCP SDK package; the local package is preciso_mcp precisely so the two
# can no longer shadow each other.
from mcp.server.fastmcp import FastMCP

from config import build_default_embedding_func, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.query import kg_query
from core.profiles import SUPPLY_CHAIN_WORKSPACE
from core.runtime_status import update_artifact_manifest
from core.storage.base import QueryParam
from core.supply_chain import query_facility_unavailable
from core.utils import BasicTokenizer, logger
from ingest.pipeline import ingest_extracted_json
from ingest.validator import validate_extraction_structure
from preciso_mcp.tools.export_tool import export_to_neo4j, export_to_qdrant
from preciso_mcp.tools.ingest_from_file_tool import ingest_from_file, reingest_from_file
from preciso_mcp.tools.pending_summaries_tool import list_pending_summaries, submit_summary
from preciso_mcp.tools.reconcile_tool import ingest_with_reconciliation
from preciso_mcp.tools.status_tool import get_server_status


tokenizer = BasicTokenizer()
global_config = build_global_config(
    # Leave the path configurable for an external MCP client.  Passing the old
    # literal here bypassed build_global_config's GRAPHRAG_MCP_WORKDIR support,
    # causing every server process to write into the checkout's fixed folder.
    working_dir=os.getenv("GRAPHRAG_MCP_WORKDIR") or None,
    tokenizer=tokenizer,
    embedding_func=None,  # populated in initialize_runtime(); building it may probe Ollama
)
# Populated by initialize_runtime(). Must stay the same dict object: every tool
# closure below captures this name, so it is mutated in place, never rebound.
storage_instances: dict = {}
# Non-default workspaces are created lazily.  The default runtime remains the
# existing finance/general workspace, so legacy tool calls are unchanged.
workspace_storage_instances: dict[str, dict] = {}
workspace_initialization_lock = asyncio.Lock()
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


async def _get_workspace_storage_instances(workspace: str | None) -> dict:
    """Return isolated storage for a configured workspace.

    Profiles are a server-side contract: callers select a workspace, not an
    arbitrary profile name in their extraction payload.
    """
    workspace_name = (workspace or "").strip()
    if not workspace_name:
        return storage_instances
    if workspace_name != SUPPLY_CHAIN_WORKSPACE:
        raise ValueError(f"Unknown workspace `{workspace_name}`")
    async with workspace_initialization_lock:
        existing = workspace_storage_instances.get(workspace_name)
        if existing is not None:
            return existing
        instances = build_storage_instances(global_config, workspace=workspace_name)
        await initialize_storage_instances(instances)
        workspace_storage_instances[workspace_name] = instances
        return instances


@mcp.tool(
    name="get_server_status",
    description="Return the current MCP runtime status and local graph health summary.",
)
async def get_server_status_tool(workspace: Literal["supply_chain"] | None = None) -> dict:
    instances = await _get_workspace_storage_instances(workspace)
    return await get_server_status(instances, global_config)


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
async def ingest_graph_tool(
    payload: dict,
    workspace: Literal["supply_chain"] | None = None,
) -> dict:
    """
    TOOL: ingest_graph_tool
    
    PURPOSE: Additively ingest a reviewed extraction payload (in-memory, no file read)
    
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
            "entities_merged": int,  # legacy total processed
            "relationships_merged": int,  # legacy total processed
            "chunks_ingested": int,  # legacy total processed
            "ingestion_counts": {
                "entities": {"added": int, "merged": int, "skipped_duplicate": int},
                "relationships": {"added": int, "merged": int, "skipped_duplicate": int},
                "chunks": {"added": int, "merged": int, "skipped_duplicate": int}
            },
            "message": "status message"
        }
    
    WHEN TO USE:
        - Agent extracts document and passes JSON directly (not from file)
        - Quick in-memory ingestion without file I/O
        - Adds new evidence; does not replace an earlier document version
    """
    validation_errors = validate_extraction_structure(payload)
    if validation_errors:
        return {
            "status": "validation_failed",
            "entities_merged": 0,
            "relationships_merged": 0,
            "chunks_ingested": 0,
            "errors": validation_errors,
        }

    instances = await _get_workspace_storage_instances(workspace)
    result = await ingest_extracted_json(payload, instances, global_config)
    if result.get("status") == "partial_success":
        result["status"] = "validation_failed"
    return result


@mcp.tool(
    name="ingest_from_file",
    description="Add a reviewed extraction file to the graph. Ingestion is additive, not replacement.",
)
async def ingest_from_file_tool(
    file_path: str,
    workspace: Literal["supply_chain"] | None = None,
) -> dict:
    """
    TOOL: ingest_from_file
    
    PURPOSE: Read a reviewed extraction file and add it to the knowledge graph
    
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
        - After the user confirms the source and extraction are correct and current
        - You want to manually control when ingestion happens
        - File-based workflow for audit trail

    IMPORTANT:
        - Ingestion is additive and does not replace prior document contributions.
        - Correcting already-ingested content requires a clean full rebuild from
          the corrected extraction plus every other valid extraction.
    
    RELATED:
        → Used after agent calls: ingest_from_file("extractions/document_extracted.json")
        → Calls: ingest/pipeline.py → ingest_extracted_json()
    """
    instances = await _get_workspace_storage_instances(workspace)
    return await ingest_from_file(file_path, instances, global_config)


@mcp.tool(
    name="reingest_from_file",
    description="Replay the identical extraction after an operational failure. Never use for correction or replacement.",
)
async def reingest_from_file_tool(
    file_path: str,
    workspace: Literal["supply_chain"] | None = None,
) -> dict:
    """
    TOOL: reingest_from_file
    
    PURPOSE: Replay the identical extraction WITHOUT re-running extraction
    Useful only for debugging/recovering from ingestion failures
    
    INPUT:
        file_path (str): Path to extraction file (same as ingest_from_file)
    
    PROCESS:
        1. Reads extraction file from disk
        2. Replays the entire ingestion pipeline:
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
        - You want to retry the identical extraction without re-calling LLM/agent
        - Saves API costs during development/debugging
        - File already exists: extractions/document_extracted.json

    DO NOT USE:
        - To correct or replace an already-ingested document
        - With a changed extraction under an existing document_id
        Changed content requires an empty graph rebuilt from the complete valid corpus.
    
    WORKFLOW:
        Scenario: Agent extracted "document.md" → extractions/document_extracted.json
        But ingest_from_file() failed due to storage error.
        Now DB is fixed, so call: reingest_from_file("extractions/document_extracted.json")
        → Replays ingestion without asking agent again (saves $)
    
    DIFFERENCE FROM ingest_from_file:
        - ingest_from_file: Initial ingestion (default behavior)
        - reingest_from_file: Identical recovery replay (same internal logic, different intent)
        Internally: Both call _ingest_file() with same logic
    """
    instances = await _get_workspace_storage_instances(workspace)
    return await reingest_from_file(file_path, instances, global_config)


@mcp.tool()
async def ingest_with_reconciliation_tool(
    extraction_files: list[str],
    workspace: Literal["supply_chain"] | None = None,
) -> dict:
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
    instances = await _get_workspace_storage_instances(workspace)
    return await ingest_with_reconciliation(
        extraction_files=extraction_files,
        storage_instances=instances,
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
    workspace: Literal["supply_chain"] | None = None,
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
        instances = await _get_workspace_storage_instances(workspace)
        result = await kg_query(
            query=query,
            knowledge_graph_inst=instances["graph"],
            entities_vdb=instances["entities_vdb"],
            relationships_vdb=instances["relationships_vdb"],
            text_chunks_db=instances["text_chunks"],
            query_param=QueryParam(mode=mode, include_references=True),
            global_config=global_config,
            hashing_kv=instances.get("llm_cache"),
            chunks_vdb=instances["chunks_vdb"],
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


@mcp.tool(
    name="query_facility_unavailable",
    description=(
        "Evaluate a hypothetical facility-unavailable scenario against documented "
        "supply-chain dependencies. Returns deterministic FACILITY → COMPONENT → PRODUCT "
        "paths with source evidence; it does not predict delay or severity."
    ),
)
async def query_facility_unavailable_tool(
    facility_id: str,
    max_paths: int = 100,
    workspace: Literal["supply_chain"] = SUPPLY_CHAIN_WORKSPACE,
) -> dict:
    try:
        instances = await _get_workspace_storage_instances(workspace)
        return await query_facility_unavailable(
            facility_id,
            instances,
            global_config,
            max_paths=max_paths,
        )
    except Exception as exc:
        logger.exception("query_facility_unavailable_tool failed")
        return {"status": "error", "message": str(exc)}


@mcp.tool(
    name="list_pending_summaries",
    description=(
        "List entities/relationships whose descriptions have outgrown their bounds "
        "and need agent summarization, with the live verbatim content to compress: "
        "prior_summary, old_descriptions (aged out of the raw tail), and keep_tail "
        "(the most recent descriptions, kept verbatim regardless). Each item's "
        "description_count must be echoed back to submit_summary as "
        "expected_description_count."
    ),
)
async def list_pending_summaries_tool(limit: int = 50) -> dict:
    return await list_pending_summaries(storage_instances, global_config, limit=limit)


@mcp.tool(
    name="submit_summary",
    description=(
        "Submit an agent-written rolling summary for a pending entity or relationship "
        "(from list_pending_summaries). Replaces the old_descriptions zone with the "
        "summary, keeps keep_tail verbatim, re-embeds, and clears the pending record. "
        "kind='entity' needs name; kind='relation' needs src and tgt (name is informational). "
        "expected_description_count must be the description_count value list_pending_summaries "
        "returned for this item — if a new merge landed since then, the submission is "
        "rejected so nothing is silently dropped; re-fetch and retry."
    ),
)
async def submit_summary_tool(
    name: str,
    kind: Literal["entity", "relation"],
    summary_text: str,
    expected_description_count: int,
    src: str | None = None,
    tgt: str | None = None,
) -> dict:
    return await submit_summary(
        storage_instances,
        global_config,
        name=name,
        kind=kind,
        summary_text=summary_text,
        expected_description_count=expected_description_count,
        src=src,
        tgt=tgt,
    )


async def startup() -> None:
    initialize_runtime()
    await initialize_storage_instances(storage_instances)
    await update_artifact_manifest(storage_instances, global_config)


if __name__ == "__main__":
    import asyncio

    asyncio.run(startup())
    mcp.run()
