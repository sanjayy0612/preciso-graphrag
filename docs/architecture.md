# Architecture

## The flow in one diagram

```
┌─────────────────────────┐
│ Human verifies sources  │
│ (correct/current/final) │
└────────────┬────────────┘
             │
             ▼
┌─────────────────────┐
│ Raw Documents       │
│ (to_be_extracted/)  │
└──────────┬──────────┘
           │
           ▼
    ┌──────────────┐
    │ Agent reads  │
    │ & selects    │
    │ skill        │
    └──────┬───────┘
           │
           ▼
┌──────────────────────────┐
│ Skill (domain-specific)  │
│ - Entity types           │
│ - Relationship types     │
│ - Extraction rules       │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│ Agent extracts           │
│ Entities + Relationships │
│ → extractions/*.json     │
└──────────┬───────────────┘
           │
           ▼
    ┌──────────────┐
    │ MCP Ingests  │
    │ (validates)  │
    └──────┬───────┘
           │
           ▼
┌──────────────────────────────────────┐
│ GRAPH_IS_HERE/                       │
│ - graph_graph.graphml (NetworkX)     │
│ - vdb_entities.json (vectors)        │
│ - vdb_relationships.json (vectors)   │
│ - kv_store_text_chunks.json (text)   │
└──────────┬───────────────────────────┘
           │
           ▼
    ┌──────────────┐
    │ Query Graph  │
    │ (traverse +  │
    │  vector)     │
    └──────┬───────┘
           │
           ▼
     ┌────────────┐
     │  Answer    │
     │ (with      │
     │  evidence) │
     └────────────┘
```

## Storage Model

### The Local Graph (Operational Source of Truth)

Everything Preciso builds lands in `GRAPH_IS_HERE/`:

```
GRAPH_IS_HERE/
       graph_graph.graphml           ← the graph (nodes + edges)
       kv_store_text_chunks.json     ← original text evidence
       kv_store_entity_chunks.json   ← which chunks each entity came from
       kv_store_relation_chunks.json ← which chunks each relation came from
       kv_store_pending_summaries.json ← descriptions awaiting agent compression
       kv_store_llm_cache.json       ← cached query/LLM responses when enabled
       kv_store_checkpoints.json     ← long-running ingestion checkpoints
       vdb_entities.json             ← entity embeddings for vector search
       vdb_relationships.json        ← relationship embeddings
       vdb_chunks.json               ← chunk embeddings
       artifact_manifest.json        ← summary of what is in the graph
```

This folder is the complete operational graph. It is self-contained for querying and export, and you can copy or back it up as a unit. Source documents and reviewed extraction files remain the reproducible inputs required to build a corrected graph.

### Downstream Exports (Optional)

Neo4j and Qdrant are export targets, not storage backends.

```
Ingest flow:
       extractions/ → MCP → GRAPH_IS_HERE/    ← always happens

Export flow (optional, manual):
       GRAPH_IS_HERE/ → export tool → Neo4j   ← you trigger this
       GRAPH_IS_HERE/ → export tool → Qdrant  ← you trigger this
```

Exports are one-way and do not auto-update. After additive ingestion or a full rebuild, `GRAPH_IS_HERE/` changes first. Re-export when you want the downstream copy refreshed.

### Why Local First

- works offline with no external services
- zero infrastructure to set up for v1
- graph is portable — move the folder, graph moves with it
- exports are for teams or production systems that need shared access or scale beyond a single machine

## What each layer does

### Pre-extraction trust and cost boundary

The human review of the source corpus is the first required gate in this architecture. Before an agent reads a document, a person must confirm that it is accurate, current, complete, and the intended version. Preciso and its extraction skills can validate generated structure and evidence references, but they cannot establish whether the original source is true.

This boundary also prevents avoidable cost. Extraction may consume paid agent or language-model credits. Later, ingestion generates vectors for chunks, entities, and relationships and may consume paid embedding-provider credits. If flawed source material reaches the graph, fixing it can require re-extraction and a full rebuild of graph-wide artifacts from the complete valid corpus.

### `to_be_extracted/`
Human-verified source files waiting to be processed. Place a file here only after confirming that it is correct, current, complete, and intended for the graph. Agents then read it and decide which skill applies. For the best graph quality, prefer `.md` and `.txt` inputs over PDFs.

Preciso does not perform document normalization, OCR, or PDF parsing as part of the core architecture. Those concerns stay outside the repo boundary.

### Skills
Domain-specific instructions defining:
- What entities to extract (and their structure)
- What relationships to capture
- How to normalize IDs and handle ambiguity

Examples: Financial 10-K skill, research paper skill, medical records skill.

### Extraction (`extractions/`)
Structured JSON output from the agent, containing:
- **Entities:** List with `entity_name`, `entity_type`, `description`, `source_id`.
- **Relationships:** List with `src_id`, `tgt_id`, `description`, `source_id`.
- **Chunks:** Text fragments with `chunk_id`, `content`, `file_path`.

Agent also validates: every `source_id` in an entity must map to a real chunk; all relationship endpoints must be defined entities.

### MCP Ingestion
The `ingest_from_file` tool:
1. Validates extraction JSON structure.
2. Deduplicates entities (same entity name = same node).
3. Transforms entities → graph nodes, relationships → edges.
4. Generates vector embeddings for entities and relationships.
5. Stores text chunks in a key-value store linked by `chunk_id`.
6. Writes artifacts to `GRAPH_IS_HERE/`.

### `GRAPH_IS_HERE/` Storage

**Graph Storage: NetworkX (`graph_graph.graphml`)**
- Nodes: entities (e.g., company, executive, metric)
- Edges: relationships (e.g., EMPLOYS, COMPETES_WITH)
- Attributes: `entity_type`, `description`, `created_at`, `file_path`, `source_id`

**Vector Databases:**
- `vdb_entities.json` — embeddings for entity names and descriptions (semantic search)
- `vdb_relationships.json` — embeddings for relationship types and descriptions
- `vdb_chunks.json` — embeddings for text chunks (used in "mix" mode)

**Key-Value Stores:**
- `kv_store_text_chunks.json` — original text chunks (proof)
- `kv_store_entity_chunks.json` — which chunks mention which entities
- `kv_store_relation_chunks.json` — which chunks mention which relationships

**Manifest:**
- `artifact_manifest.json` — metadata: entity count, relationship count, embedding dimensions, last updated.

### Document identity and incremental ingestion

Preciso builds one graph from one or more extraction payloads. The supported ingestion semantics are:

- **Initial graph build:** ingest each source document with a stable, unique `document_id`.
- **New data later:** ingest the new source under a new `document_id`; matching entities and relationships accumulate the new evidence.
- **Recovery replay:** reingesting an identical extraction with the same `document_id` is idempotent.
- **Changed existing document:** reusing an existing `document_id` with changed content is not a replacement operation. The merge pipeline is additive, so evidence produced by the earlier version can remain stored and cited.

Preciso intentionally does not expose document replacement or deletion. Merged descriptions, relationship weights, summaries, and shared graph connections do not retain enough per-document contribution history for safe subtraction.

When an ingested source is flawed, perform a full rebuild:

1. Preserve every unaffected source document and reviewed extraction.
2. Remove the flawed extraction from the rebuild inputs and generate its corrected replacement.
3. Stop the active ingestion session and back up the current graph if recovery may be needed.
4. Remove every generated artifact from `GRAPH_IS_HERE/` and start a fresh session with an empty graph.
5. Ingest the corrected extraction plus every other valid extraction in the complete corpus.
6. Re-run representative queries or evaluations and regenerate downstream exports.

The extraction files may be ingested sequentially. Their shared participation in the same empty graph is what allows entity merging and cross-document relationships to be recomputed consistently.

### Query Execution

When you ask a question, the system:

1. **Embed the query** (using Ollama or your configured embedding provider).
2. **Vector search** in `vdb_entities.json` and `vdb_relationships.json` to find likely-relevant entities and edges.
3. **Graph traversal** starting from matched entities, following edges to find connected entities.
4. **Collect evidence candidates** from entity source IDs, relationship source IDs, and direct chunk-vector search.
5. **Strict vector selection** — deduplicate candidates, require a stored vector for every chunk, score all candidates against the original query, reject weak matches, and keep one global top-K.
6. **Assemble context** — entities, relationships, and the selected text chunks formatted as structured JSON within the final token budget.
7. **Return or augment** — either return context as-is (for inspection) or pass to LLM for synthesis.

Evidence selection is fail-closed: Preciso does not fall back to an unranked or weighted method when a chunk vector is missing. Ingestion verifies each chunk vector before making its text available for graph evidence. A missing vector during query indicates a corrupt or legacy index and returns an explicit error instructing the caller to replay the identical extraction or perform a full rebuild.

The evidence selector is configured with:

- `GRAPHRAG_KG_EVIDENCE_TOP_K` — global evidence cap after all sources are merged (default `8`).
- `GRAPHRAG_KG_EVIDENCE_MIN_SIMILARITY` — minimum cosine similarity to the original query (default `0.35`).

Three query modes:
- **local**: Graph-only traversal from entities, no relationship-level search.
- **global**: Relationship-focused search (good for "how are these concepts related?").
- **hybrid/mix**: Both entity and relationship vectors + chunks (best for most queries).

## Why local-first

**Local storage means:**
- No cloud dependency; graph lives in your repo.
- Fast additive iteration: ingest new documents and re-query in seconds.
- Full privacy: documents never leave your machine.
- Version control: `GRAPH_IS_HERE/` can be committed, tracked, and diffed.

**Trade-offs:**
- NetworkX is single-threaded (good for local, not for 100M nodes).
- Vector DB is in-memory JSON (fast for <1M embeddings, slow for larger).
- No automatic scaling (but you can export to Neo4j or Qdrant).

For production systems with large document corpora, consider exporting to:
- **Neo4j** for graph queries and multi-user access.
- **Qdrant** for high-throughput vector similarity.

Both are optional; local storage is always the primary artifact.

## Optional exports

### Neo4j Export

Use `export_graph_to_neo4j` to push your local graph to a Neo4j instance:

```bash
mcp_graphrag-mcp_export_graph_to_neo4j(
  uri="bolt://localhost:7687",
  username="neo4j",
  password="your_password",
  clear_existing=True
)
```

**When to use:** Multi-user access, complex graph queries, production deployments.

### Qdrant Export

Use `export_vectors_to_qdrant` to push embeddings to a Qdrant instance:

```bash
mcp_graphrag-mcp_export_vectors_to_qdrant(
  url="http://localhost:6333",
  api_key="your_key",
  collection_prefix="walmart_2023"
)
```

**When to use:** Distributed vector search, long-running similarity queries, integration with other vector-based systems.

---

**Next steps:**
- Read [getting-started.md](getting-started.md) to build your first graph.
- See [skills-guide.md](skills-guide.md) to customize extraction for your domain.
