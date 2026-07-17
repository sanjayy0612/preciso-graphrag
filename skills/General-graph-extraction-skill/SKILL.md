---
name: general-graph-extraction
description: >
  Use this skill whenever a user wants to extract entities and relationships from
  non-financial documents to build a queryable knowledge graph. Triggers include:
  codebases and technical documentation, internal wikis, product specs, README files,
  meeting notes, support tickets, HR or policy documents, onboarding materials,
  knowledge bases, Notion/Confluence exports, API docs, and general text corpora.
  Also trigger when the user says "map dependencies in my codebase", "graph this doc",
  "extract knowledge from this wiki", "connect these notes", "build a graph from my repo",
  or "index this for querying". Use the financial-graph-extraction skill instead when
  the document is a financial filing, earnings report, or analyst document.
  Output must follow the GraphRAG ingestion schema: document_id + entities + relationships + chunks,
  with chunk-based source_id values.
---

# General Graph Extraction Skill

## Agent Runtime Contract

Use this skill when the raw input file is in `to_be_extracted/` and the content is not primarily financial or academic literature.

Execution contract:
- Call `get_server_status()` before starting extraction.
- If overall is `degraded`, explain what is degraded, what still works, and ask whether to proceed or fix first.
- Read one raw source file from `to_be_extracted/` at a time unless the user explicitly asks for a combined extraction.
- For the best graph quality, prefer `.md` and `.txt` source files. If the source is PDF or heavily formatted export text, assume there is no built-in repo parser or OCR layer and be conservative about layout noise.
- Write exactly one extraction JSON for that source file to `extractions/{source_filename}_extracted.json`.
- Validate entity, relationship, and chunk integrity before ingestion.
- If the extraction has duplicates, orphaned relationships, or consistency issues, run reconciliation before ingestion.
- After a clean extraction is written, call `ingest_from_file`.

## What This Skill Does

This skill guides an agent to read a document or codebase and produce a structured JSON extraction — entities and relationships — that can be ingested into a knowledge graph via the `ingest_from_file` MCP tool.

Unlike financial extraction (which enforces strict numerical precision), general extraction prioritizes **semantic richness and connection density** — the goal is to enable natural-language queries like:
- "What does the AuthService depend on?"
- "Which modules are owned by the payments team?"
- "What broke when we changed the database schema?"
- "Who are the experts on the recommendation engine?"

The goal is not to graph every noun. The goal is to build a graph with high-signal, reusable structure:
- who owns what
- what depends on what
- what decisions were made
- what actions, risks, blockers, and systems matter
- what evidence supports those links

---

## General Reading Strategy

Read the source in layers instead of linearly dumping entities:

### Pass 1 — Identify document subtype and signal

Classify the source before extracting:
- codebase or technical docs
- internal wiki or knowledge base
- meeting notes or ticket
- policy or process document
- product spec or decision record

During this pass, decide:
- what the main units of knowledge are
- what should count as entities versus plain context
- which sections are likely high signal
- whether the source is clean enough for dense extraction

### Pass 2 — Extract structure first

Prioritize:
- named systems, modules, APIs, tools, teams, people, processes, decisions, projects
- explicit ownership
- explicit dependencies
- procedural order
- blockers, action items, and operational constraints

### Pass 3 — Add only useful semantic detail

Add broader concepts only when they help downstream queries:
- architecture patterns
- operational policies
- domain concepts
- known issues

If the text is noisy, repetitive, or weakly structured, reduce extraction density instead of inflating the graph.

---

## Extraction Output Format (Repo-Compatible)

Write to `extractions/{source_filename}_extracted.json`.

This repo **requires** `document_id`, `entities`, `relationships`, and `chunks`.

```json
{
  "document_id": "backend_readme",
  "file_path": "README.md",
  "timestamp": 1727740800,
  "entities": [...],
  "relationships": [...],
  "chunks": [...]
}
```

### Chunks (Required)
Chunks are the evidence base. Every entity and relationship must point to a chunk via `source_id`.

```json
{
  "chunk_id": "chunk_001",
  "content": "The OrderService is owned by the Payments team and calls InventoryService.",
  "chunk_order_index": 1,
  "file_path": "README.md"
}

**Chunk size limits (required):**
- Target 400-800 characters per chunk (or ~200-300 tokens if you have a tokenizer).
- Hard cap 1,000 characters per chunk. If a paragraph/table is longer, split it.
```

---

## Domain Subtypes and Entity Profiles

Choose the right entity profile based on what you're processing:

### 1. Codebase / Technical Docs

| Entity Type | Examples |
|-------------|---------|
| `MODULE` | `auth_service`, `payment_processor` |
| `FUNCTION` | `validate_token()`, `create_order()` |
| `CLASS` | `UserRepository`, `InvoiceBuilder` |
| `API_ENDPOINT` | `POST /api/v2/orders` |
| `DATABASE_TABLE` | `users`, `transactions` |
| `CONFIG_KEY` | `MAX_RETRY_COUNT`, `DB_URL` |
| `DEPENDENCY` | `numpy`, `FastAPI`, `Redis` |
| `TEAM` | `Platform Team`, `Payments Squad` |
| `CONCEPT` | `idempotency`, `circuit breaker` |

Relevant relationship keywords: `IMPORTS`, `CALLS`, `OWNS`, `DEPENDS_ON`, `DOCUMENTS`, `IMPLEMENTS`, `EXTENDS`, `STORES_IN`, `EXPOSES`

### 2. Internal Wiki / Knowledge Base

| Entity Type | Examples |
|-------------|---------|
| `PERSON` | Sanjay Elango (author/owner) |
| `TEAM` | Backend Infrastructure |
| `PROCESS` | Deployment Pipeline, Oncall Rotation |
| `TOOL` | Datadog, PagerDuty, Terraform |
| `CONCEPT` | SLA definition, retry policy |
| `DOCUMENT` | Runbook: DB failover |
| `DECISION` | ADR-042: Use Postgres over MySQL |

Relevant relationship keywords: `OWNS`, `WRITTEN_BY`, `REFERENCES`, `SUPERSEDES`, `IMPLEMENTS`, `USES`, `DESCRIBES`, `DECIDES`

### 3. Meeting Notes / Tickets

| Entity Type | Examples |
|-------------|---------|
| `PERSON` | Named attendees/assignees |
| `ACTION_ITEM` | "Fix token expiry bug by Friday" |
| `DECISION` | "We'll use Redis for session storage" |
| `BLOCKER` | "Waiting on security review" |
| `PROJECT` | Project Phoenix |
| `DATE` | 2024-10-15 (target date) |

Relevant relationship keywords: `ASSIGNED_TO`, `BLOCKS`, `RELATES_TO`, `DECIDED_IN`, `DUE_BY`

---

## Universal Entity Schema (Required Fields)

```json
{
  "entity_name": "auth_service",
  "entity_type": "MODULE",
  "description": "Authentication service that issues and validates JWTs; owner=platform_team; path=services/auth/.",
  "source_id": "chunk_002",
  "file_path": "services/auth/README.md"
}
```

**Rules:**
- `entity_name`: unique, `snake_case`, no spaces
- `entity_type`: pick from the relevant profile list
- `description`: include concrete facts and properties from the text (owner, path, constraints)
- `source_id`: must match a real `chunk_id` in `chunks`
- prefer one strong entity per real concept, not many aliases for the same thing

---

## Universal Relationship Schema (Required Fields)

```json
{
  "src_id": "order_service",
  "tgt_id": "payment_processor",
  "description": "OrderService calls PaymentProcessor to charge the customer during checkout.",
  "keywords": "CALLS,context=checkout_flow",
  "source_id": "chunk_001",
  "weight": 1.0,
  "file_path": "README.md"
}
```

**Rules:**
- `src_id` and `tgt_id` must be valid `entity_name` values
- `keywords` should include the relationship type (e.g., `CALLS`, `OWNS`, `DEPENDS_ON`)
- Add `context=...` or `source_section=...` in `keywords` when useful
- Directed relationships: source → target; for bidirectional add both directions
- only create a relationship when the source text gives direct support for it or the implication is extremely clear from local context

---

## What To Ignore

Do not extract:
- repeated headers, navigation text, sidebars, export boilerplate, template labels
- generic filler like "this document describes" unless it carries a real relationship
- every file path, import, or config key in bulk when they add no query value
- every attendee name in notes if they are not tied to a decision, action, or ownership signal
- duplicate concepts restated in multiple sections

Prefer signal over completeness. A smaller, cleaner graph is better than a bloated one.

---

## Extraction Quality Principles

### For Codebases
- Trace dependencies: what imports what, what calls what
- Map ownership: which team/person owns which module
- Surface configuration: key env vars and their consuming modules
- Capture architectural patterns mentioned (MVC, event-driven, etc.)
- Note TODOs/FIXMEs as `KNOWN_ISSUE` entities if they're significant
- Distinguish between implementation details and stable concepts; prefer stable concepts unless the user clearly wants code-level granularity

### For Documentation
- Every named concept becomes an entity
- Every "see also", "refer to", "documented in" becomes a `REFERENCES` relationship
- Every "owned by", "maintained by" becomes an `OWNS` relationship
- Procedural steps → sequence of `PROCESS` entities linked by `PRECEDES`
- Collapse repeated explanations into one canonical entity with the clearest supporting chunk

### For Notes/Tickets
- Every named person is an entity
- Every task or action item is an entity
- Capture blockers as explicit `BLOCKER` entities with `BLOCKS` relationships
- Favor decisions, owners, due dates, and blockers over conversational filler

---

## Extraction Density vs. Precision Trade-off

| Dimension | Financial Extraction | General Extraction |
|-----------|---------------------|--------------------|
| Precision | Exact (numbers must match) | Semantic (meaning over literal match) |
| Coverage | Deep in financial semantics | Broad across any domain |
| Ambiguity | Resolve strictly (no inference) | Light inference OK if clearly implied |
| Missing values | Leave blank, don't guess | Infer from context if confident |

In general extraction, if a README says "the auth module handles login and token refresh", it's reasonable to extract a `CONCEPT` entity `token_refresh` and link it to the auth module via `IMPLEMENTS` even if not explicitly stated that way.

But do not stretch this into speculation. If the text is ambiguous, prefer fewer entities and relationships.

---

## Better Chunking For General Sources

Use chunk boundaries that preserve meaning:
- one architectural paragraph
- one decision paragraph
- one action-item block
- one API section
- one process step group

Avoid chunks that mix unrelated topics just to hit a length target. Semantic coherence matters more than uniform size.

---

## Better Graph Outcomes

Optimize extraction for the questions users actually ask later:
- ownership: who owns this system, process, or document
- dependency: what calls, uses, imports, or relies on what
- decision history: what changed, why, and what it replaced
- operations: what blocks what, what must happen first, what is risky
- expertise: who is associated with which subsystem or domain

If an extracted entity or relationship is unlikely to help answer one of those question types, think twice before adding it.

---

## Step-by-Step Agent Workflow

```
1. CALL get_server_status() and surface degraded modes before continuing
2. READ the full document (or repo files in scope)
3. IDENTIFY domain subtype (codebase, wiki, notes, etc.)
4. CHUNK the text into evidence blocks (sections, paragraphs, or tables)
   - Assign chunk_id like chunk_001, chunk_002...
5. SCAN for all named entities — people, modules, tools, concepts, processes
6. EXTRACT entities with required fields and chunk-based source_id
7. EXTRACT relationships by asking: "how does X relate to Y in this text?"
8. VALIDATE:
   - No entity_name duplicates
   - All relationships reference existing entity_name values
   - Every entity/relationship source_id matches a chunk_id
   - Output path matches the current source file name
9. WRITE to extractions/{filename}_extracted.json
10. CALL ingest_from_file MCP tool
11. CALL list_pending_summaries() and resolve any flagged entities/relationships
    via submit_summary(...) before finishing — Preciso never compresses
    descriptions itself, so this step is the only way descriptions
    exceeding raw_tail_size ever get summarized
```

---

## Example: Codebase README Extraction

**Input text:**
> "The OrderService module is owned by the Payments team. It calls InventoryService to check stock before calling PaymentProcessor to charge the customer. It stores order records in the orders table of the PostgreSQL database."

**Output:**
```json
{
  "document_id": "backend_readme",
  "file_path": "README.md",
  "timestamp": 1727740800,
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "content": "The OrderService module is owned by the Payments team. It calls InventoryService to check stock before calling PaymentProcessor to charge the customer. It stores order records in the orders table of the PostgreSQL database.",
      "chunk_order_index": 1,
      "file_path": "README.md"
    }
  ],
  "entities": [
    {
      "entity_name": "order_service",
      "entity_type": "MODULE",
      "description": "OrderService module owned by Payments team.",
      "source_id": "chunk_001",
      "file_path": "README.md"
    },
    {
      "entity_name": "inventory_service",
      "entity_type": "MODULE",
      "description": "InventoryService called to check stock before charging.",
      "source_id": "chunk_001",
      "file_path": "README.md"
    },
    {
      "entity_name": "payment_processor",
      "entity_type": "MODULE",
      "description": "PaymentProcessor used to charge the customer.",
      "source_id": "chunk_001",
      "file_path": "README.md"
    },
    {
      "entity_name": "payments_team",
      "entity_type": "TEAM",
      "description": "Payments team owns OrderService.",
      "source_id": "chunk_001",
      "file_path": "README.md"
    },
    {
      "entity_name": "orders_table",
      "entity_type": "DATABASE_TABLE",
      "description": "orders table in PostgreSQL storing order records.",
      "source_id": "chunk_001",
      "file_path": "README.md"
    }
  ],
  "relationships": [
    {
      "src_id": "payments_team",
      "tgt_id": "order_service",
      "description": "Payments team owns OrderService.",
      "keywords": "OWNS",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "README.md"
    },
    {
      "src_id": "order_service",
      "tgt_id": "inventory_service",
      "description": "OrderService calls InventoryService to check stock.",
      "keywords": "CALLS,context=stock_check",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "README.md"
    },
    {
      "src_id": "order_service",
      "tgt_id": "payment_processor",
      "description": "OrderService calls PaymentProcessor to charge the customer.",
      "keywords": "CALLS,context=checkout_flow",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "README.md"
    },
    {
      "src_id": "order_service",
      "tgt_id": "orders_table",
      "description": "OrderService stores order records in orders table.",
      "keywords": "STORES_IN,db=PostgreSQL",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "README.md"
    }
  ]
}
```

---

## After Extraction

- Run `ingest_from_file("extractions/{filename}_extracted.json")` via MCP
- If ingestion fails, use `reingest_from_file(...)` to replay without re-extracting
- Call `list_pending_summaries()`. Preciso never compresses descriptions with an LLM — if any entity/relationship is flagged (its raw description history exceeded `raw_tail_size`), write a concise summary of `prior_summary` + `old_descriptions` and call `submit_summary(...)` for each one before considering the job done
- Sample queries after ingestion:
  - `"What does OrderService depend on?"`
  - `"Which tables does the Payments team write to?"`
  - `"Who owns InventoryService?"`

---

## Large Document Handling

### When to use subagents

Never for extraction.
Always extract the full document yourself.
Your entity registry prevents name drift.

Spawn reconciliation subagents only when:
  - Extraction produces more than 150 entities
  - Extraction produces more than 300 relationships
  - You want a quality check before ingestion

### How to spawn reconciliation subagents

After writing your extraction file:

Step 1: Spawn 3 reconciliation subagents in parallel.
        Give each subagent the extraction JSON.

        Subagent 1 gets: entities list only
          Task: find entity name variants and duplicates
          Output: extractions/{filename}_patch_entities.json

        Subagent 2 gets: relationships list only
          Task: find duplicate relationships and conflicts
          Output: extractions/{filename}_patch_relationships.json

        Subagent 3 gets: entities + relationships
          Task: find broken references (src_id or tgt_id
                not in entities list)
          Output: extractions/{filename}_patch_orphans.json

Step 2: Wait for all 3 subagents to finish.

Step 3: Call ingest_with_reconciliation([
          "extractions/{filename}_extracted.json",
          "extractions/{filename}_patch_entities.json",
          "extractions/{filename}_patch_relationships.json",
          "extractions/{filename}_patch_orphans.json"
        ])

### Reconciliation subagent instructions

When you are a reconciliation subagent:
  - You are cleaning data, not extracting from documents
  - Be conservative: only flag what is clearly wrong
  - For entity merges: only merge if you are certain
    they refer to the same real-world entity
  - For temporal entities (FY2024, Q3 2024):
    NEVER merge even if semantically similar
    Different periods = different entities always
  - Write your patch file and stop
  - Do not call any ingest tool
