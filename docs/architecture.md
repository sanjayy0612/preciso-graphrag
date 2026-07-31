# Architecture

## The flow in one diagram

```
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

### The Local Graph (Source of Truth)

Everything Preciso builds lands in `GRAPH_IS_HERE/`:

```
GRAPH_IS_HERE/
       graph_graph.graphml           ← the graph (nodes + edges)
       kv_store_text_chunks.json     ← original text evidence
       kv_store_entity_chunks.json   ← which chunks each entity came from
       kv_store_relation_chunks.json ← which chunks each relation came from
       vdb_entities.json             ← entity embeddings for vector search
       vdb_relationships.json        ← relationship embeddings
       vdb_chunks.json               ← chunk embeddings
       artifact_manifest.json        ← summary of what is in the graph
```

This folder is the graph. It is self-contained. You can copy it, back it up, or rebuild it from your extraction files.

### Downstream Exports (Optional)

Neo4j and Qdrant are export targets, not storage backends.

```
Ingest flow:
       extractions/ → MCP → GRAPH_IS_HERE/    ← always happens

Export flow (optional, manual):
       GRAPH_IS_HERE/ → export tool → Neo4j   ← you trigger this
       GRAPH_IS_HERE/ → export tool → Qdrant  ← you trigger this
```

Exports are one-way and do not auto-update. If you re-ingest locally, `GRAPH_IS_HERE/` changes first. Re-export when you want the downstream copy refreshed.

### Why Local First

- works offline with no external services
- zero infrastructure to set up for v1
- graph is portable — move the folder, graph moves with it
- exports are for teams or production systems that need shared access or scale beyond a single machine

## What each layer does

### `to_be_extracted/`
Source files waiting to be processed. Developers drop files here; agents read them and decide which skill applies. For the best graph quality, prefer `.md` and `.txt` inputs over PDFs.

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

The ingest pipeline re-checks the `source_id` rule rather than trusting the agent. A `source_id` matching no chunk — in the current payload or already in storage from an earlier ingest of the same document — is reported under `warnings` in the ingest result. Set `GRAPHRAG_STRICT_SOURCE_IDS=true` to reject those entities and relationships instead of ingesting them.

Oversized chunks are split on ingest into `-p1`, `-p2`, ... parts (see `GRAPHRAG_CHUNK_CHAR_LIMIT`). A `source_id` citing the original chunk id is expanded to every part it became, so the evidence stays reachable at query time.

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

### Query Execution

When you ask a question, the system:

1. **Embed the query** (using Ollama or your configured embedding provider).
2. **Vector search** in `vdb_entities.json` and `vdb_relationships.json` to find likely-relevant entities and edges.
3. **Graph traversal** starting from matched entities, following edges to find connected entities.
4. **Text retrieval** from `kv_store_text_chunks.json` using chunk IDs associated with entities and relationships.
5. **Assemble context** — entities, relationships, text chunks formatted as structured JSON.
6. **Return or augment** — either return context as-is (for inspection) or pass to LLM for synthesis.

Three query modes:
- **local**: Graph-only traversal from entities, no relationship-level search.
- **global**: Relationship-focused search (good for "how are these concepts related?").
- **hybrid/mix**: Both entity and relationship vectors + chunks (best for most queries).

## Why local-first

**Local storage means:**
- No cloud dependency; graph lives in your repo.
- Fast iteration: modify extraction, re-ingest, re-query in seconds.
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
