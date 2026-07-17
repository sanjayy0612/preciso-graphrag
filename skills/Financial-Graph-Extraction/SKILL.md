---
name: financial-graph-extraction
description: >
  Use this skill whenever a user wants to extract entities and relationships from
  financial documents for graph-based ingestion in this repo. Triggers include: 10-K or
  10-Q filings, earnings call transcripts, analyst reports, investor presentations,
  SEC filings, financial news articles, risk disclosures, balance sheets, income
  statements, MD&A sections, or any structured/unstructured financial text.
  Also trigger when the user says things like "build a graph from this report",
  "map the relationships in this filing", "connect entities in this earnings call",
  "I want to query this financial doc", or "extract for graph-rag".
  This skill enforces HIGH PRECISION extraction — financial data must be factually exact,
  numerically grounded, and relationship-typed with financial semantics. Always use this
  over the general extraction skill when the source material is financial in nature.
  Output must follow the GraphRAG ingestion schema: document_id + entities + relationships + chunks,
  with chunk-based source_id values.
---

# Financial Graph Extraction Skill

## Agent Runtime Contract

Use this skill when the raw input file is in `to_be_extracted/` and the content is financial in nature.

Execution contract:
- Call `get_server_status()` before starting extraction.
- If overall is `degraded`, explain what is degraded, what still works, and ask whether to proceed or fix first.
- Read one raw source file from `to_be_extracted/` at a time unless the user explicitly asks for a combined extraction.
- Write exactly one extraction JSON for that source file to `extractions/{source_filename}_extracted.json`.
- Validate entity, relationship, and chunk integrity before ingestion.
- If you detect duplicates, orphaned references, or conflicts that need cleanup, use the reconciliation skill before calling ingestion.
- After a clean extraction is written, call `ingest_from_file` with the generated extraction path.

## Why This Skill Exists

Financial analysts read enormous volumes of documents — 10-Ks, earnings transcripts, research notes — to surface connections between companies, people, risks, and metrics. This skill turns that reading into a structured knowledge graph that an agent can query with precision.

Financial extraction demands **higher fidelity than general text** because:
- Numbers, dates, and percentages must be exact (no approximation)
- Entity disambiguation matters (Apple Inc. ≠ Apple the fruit; Tim Cook as CEO ≠ Tim Cook as person)
- Relationships carry financial weight (subsidiary, acquired, guarantees, competes with)
- Temporal context is critical (Q3 2024 revenue ≠ Q3 2023 revenue)

---

## Extraction Output Format (Repo-Compatible)

Always write to `extractions/{source_filename}_extracted.json`.

This repo **requires** `document_id`, `entities`, `relationships`, and `chunks`.

```json
{
  "document_id": "apple_10k_2024",
  "file_path": "apple_10k_2024.pdf",
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
  "chunk_id": "chunk_003",
  "content": "Net sales increased 2 percent or $7.8 billion during 2024 compared to 2023.",
  "chunk_order_index": 3,
  "file_path": "apple_10k_2024.pdf"
}

**Chunk size limits (required):**
- Target 400-800 characters per chunk (or ~200-300 tokens if you have a tokenizer).
- Hard cap 1,000 characters per chunk. If a paragraph/table is longer, split it.
```

---

## Entity Types for Financial Documents

Extract the following entity types with strict typing:

### Core Entity Types

| Type | Description | Example |
|------|-------------|---------|
| `COMPANY` | Legal corporate entity | Apple Inc., Foxconn Technology Group |
| `PERSON` | Named individual with role | Tim Cook (CEO), Luca Maestri (CFO) |
| `FINANCIAL_METRIC` | Quantitative figure | Revenue $394.3B, Net Income $97B, EPS $6.16 |
| `PRODUCT_LINE` | Named revenue segment | iPhone, Mac, Services, Wearables |
| `GEOGRAPHIC_SEGMENT` | Revenue or ops region | Americas, Greater China, Europe |
| `RISK_FACTOR` | Disclosed risk | Supply chain concentration, FX exposure, litigation |
| `LEGAL_ENTITY` | Subsidiary or JV | Apple Operations International Ltd |
| `REGULATION` | Named law or standard | ASC 606, GDPR, Sarbanes-Oxley |
| `TRANSACTION` | M&A, investment, divestiture | Acquired Beats Electronics for $3B |
| `DATE_PERIOD` | Fiscal period | FY2024, Q3 2024, YE September 28 2024 |

### Entity Schema (Required Fields)

```json
{
  "entity_name": "apple_inc",
  "entity_type": "COMPANY",
  "description": "Apple Inc. (AAPL) is a consumer electronics company with FY end in September.",
  "source_id": "chunk_001",
  "file_path": "apple_10k_2024.pdf"
}
```

For `FINANCIAL_METRIC`, keep **all numeric detail inside `description`** so ingestion preserves it:

```json
{
  "entity_name": "net_sales_fy2024",
  "entity_type": "FINANCIAL_METRIC",
  "description": "Total net sales were $391,035 million for FY2024 (YoY +2.0%; source section MD&A).",
  "source_id": "chunk_003",
  "file_path": "apple_10k_2024.pdf"
}
```

---

## Relationship Types for Financial Documents

Relationships use `src_id` and `tgt_id` (entity_name values). Encode relationship type and qualifiers in `keywords`.

### Relationship Type Keywords (Controlled Vocabulary)

| Keyword | Usage |
|---------|-------|
| `EMPLOYS` | Company → Person (with role) |
| `SUBSIDIARY_OF` | Legal entity → Parent company |
| `ACQUIRED` | Company → Target (with date, value) |
| `COMPETES_WITH` | Company ↔ Company |
| `SUPPLIES_TO` | Supplier → Customer |
| `REPORTED_METRIC` | Company → Financial metric |
| `OPERATES_IN` | Company → Geographic segment |
| `EXPOSED_TO` | Company/Segment → Risk factor |
| `GOVERNED_BY` | Company/Practice → Regulation |
| `ISSUED` | Company → Debt/Equity instrument |
| `AUDITED_BY` | Company → Audit firm |
| `MENTIONS` | Document section → Any entity |

### Relationship Schema (Required Fields)

```json
{
  "src_id": "apple_inc",
  "tgt_id": "tim_cook",
  "description": "Tim Cook served as Chief Executive Officer of Apple Inc. (since 2011; FY2024).",
  "keywords": "EMPLOYS,role=Chief Executive Officer,period=FY2024",
  "source_id": "chunk_014",
  "weight": 1.0,
  "file_path": "apple_10k_2024.pdf"
}
```

### Year-over-Year (YoY) Relationships

When the same metric appears in two or more fiscal years
within the document, ALWAYS:

1. Extract each year as a separate entity
   entity_name must include the fiscal year
   CORRECT:   "NET SALES FY2023", "NET SALES FY2022"
   INCORRECT: "NET SALES" (ambiguous, which year?)

2. Calculate the YoY change yourself from the document values:
   abs_change = FY2023_value - FY2022_value
   pct_change = (abs_change / FY2022_value) × 100
   direction  = "increased" or "decreased"

3. Create a COMPARED_TO relationship between them:

{
  "src_id": "NET SALES FY2023",
  "tgt_id": "NET SALES FY2022",
  "keywords": "COMPARED_TO,yoy_direction=increased,yoy_abs=+38.1B,yoy_pct=+6.7%",
  "description": "Net sales increased $38.1 billion (+6.7%) 
                  from $567.8B in FY2022 to $605.9B in FY2023.",
  "weight": 1.0,
  "source_id": "chunk_id_where_both_values_appear"
}

The description field MUST contain:
  - direction (increased/decreased)
  - absolute change with dollar sign and B/M suffix
  - percentage change with + or - sign
  - both base year value and current year value
  - both fiscal years named explicitly

This relationship is what the query engine uses to answer
comparison questions. If this relationship is missing,
YoY questions will return incomplete answers.

Always attach `period` or `as_of` in `description` or `keywords` for relationships that change over time.

---

## Precision Rules (Non-Negotiable)

1. **Exact numbers**: Copy figures verbatim from the source. Never round unless the source rounds.
2. **Units always present**: Put units directly in `description` (e.g., `$391,035 million`).
3. **Period always present**: Every metric must reference a fiscal period in `description` or `keywords`.
4. **Disambiguate entities**: If "the Company" refers to Apple Inc., resolve it. Don't leave pronouns.
5. **No inference beyond the text**: If the document does not say X acquired Y, don't add that relationship.
6. **Source section tagging**: Add `source_section=...` in `keywords` or `description`.
7. **Conflicting data**: If two sections report different values for the same metric, create separate entities (e.g., `net_sales_fy2024_mda`, `net_sales_fy2024_financials`) and mark `conflict=true` in `keywords`.
8. **Evidence linkage**: Every entity and relationship must use a `source_id` that matches a real `chunk_id` in `chunks`.

---

## Analyst-Oriented Extraction Focus

When processing financial documents, prioritize connections analysts actually query:

### Tier 1 — Always Extract
- Executive team (name, title, compensation, tenure)
- Revenue by segment (product line + geography)
- Key metrics: revenue, gross margin, operating income, net income, EPS, FCF
- Debt structure: long-term debt, credit facilities, maturities
- Top risks disclosed in Risk Factors section
- Auditor and audit opinion
- Related-party transactions
- COMPARED_TO relationships for all metrics present in both fiscal years

### Tier 2 — Extract When Present
- Customer concentration (if any customer > 10% revenue)
- Supplier/manufacturing dependencies
- Pending litigation with materiality estimates
- Off-balance-sheet arrangements
- Goodwill and intangibles by acquisition
- Share repurchase programs

### Tier 3 — Extract for Deep Analysis
- Non-GAAP reconciliation items
- Geographic revenue breakdowns
- Employee headcount and changes
- Capital expenditure breakdown
- Lease obligations schedule

---

## Step-by-Step Agent Workflow

```
1. CALL get_server_status() and surface degraded modes before continuing
2. READ the document in full (or in chunked passes for long filings)
3. IDENTIFY document type (10-K, 10-Q, earnings transcript, etc.)
4. CHUNK the text into evidence blocks (sections, paragraphs, or tables)
   - Assign chunk_id like chunk_001, chunk_002...
5. EXTRACT metadata for document_id + file_path
6. PASS 1 — Entity extraction:
   - Scan for all named companies, people, metrics, segments, risks
   - Assign entity_name (snake_case, unique within the file)
   - Set source_id to the chunk_id that contains the evidence
7. PASS 2 — Relationship extraction:
   - For each entity pair, determine if a typed relationship exists in the text
   - Encode relationship type in keywords (EMPLOYS, ACQUIRED, etc.)
   - Attach period/as_of in description or keywords
8. PASS 3 — Validation:
   - All entities have entity_name, entity_type, description, source_id
   - All relationships have src_id, tgt_id, description, source_id
   - No entity_name is duplicated
   - No relationship references an undefined entity_name
   - Every source_id matches an existing chunk_id
   - Output path matches the current source file name
9. WRITE to extractions/{filename}_extracted.json
10. CALL ingest_from_file MCP tool with the output path
11. CALL list_pending_summaries() and resolve any flagged entities/relationships
    via submit_summary(...) before finishing — Preciso never compresses
    descriptions itself, so this step is the only way descriptions
    exceeding raw_tail_size ever get summarized
```

---

## Common Financial Document Patterns

### 10-K Structure Mapping
```
Item 1 (Business)        → COMPANY, PRODUCT_LINE, GEOGRAPHIC_SEGMENT, COMPETES_WITH
Item 1A (Risk Factors)   → RISK_FACTOR, EXPOSED_TO
Item 7 (MD&A)            → FINANCIAL_METRIC, REPORTED_METRIC, trends
Item 8 (Financial Stmts) → FINANCIAL_METRIC (authoritative source)
Item 9A (Controls)       → REGULATION, GOVERNED_BY
Proxy Statement          → PERSON, EMPLOYS, compensation
```

### Earnings Call Transcript Pattern
- Speaker turns → `PERSON` entities with `EMPLOYS` linking to company
- Guidance → `FINANCIAL_METRIC` with `period=FY2025E` in description/keywords (E = estimate)
- Analyst questions → tag `source_section=Q&A` in keywords

---

## Example Extraction (Apple 10-K Excerpt)

**Input text:**
> "Apple's net sales increased 2 percent or $7.8 billion during 2024 compared to 2023. iPhone net sales decreased 1 percent or $2.0 billion during 2024 compared to 2023."

**Output:**
```json
{
  "document_id": "apple_10k_2024",
  "file_path": "apple_10k_2024.pdf",
  "timestamp": 1727740800,
  "chunks": [
    {
      "chunk_id": "chunk_003",
      "content": "Apple's net sales increased 2 percent or $7.8 billion during 2024 compared to 2023. iPhone net sales decreased 1 percent or $2.0 billion during 2024 compared to 2023.",
      "chunk_order_index": 3,
      "file_path": "apple_10k_2024.pdf"
    }
  ],
  "entities": [
    {
      "entity_name": "apple_inc",
      "entity_type": "COMPANY",
      "description": "Apple Inc. is the reporting company referenced in the 2024 filing.",
      "source_id": "chunk_003",
      "file_path": "apple_10k_2024.pdf"
    },
    {
      "entity_name": "iphone",
      "entity_type": "PRODUCT_LINE",
      "description": "iPhone product line referenced in the net sales discussion.",
      "source_id": "chunk_003",
      "file_path": "apple_10k_2024.pdf"
    },
    {
      "entity_name": "net_sales_fy2024_change",
      "entity_type": "FINANCIAL_METRIC",
      "description": "Net sales increased 2% or $7.8B during 2024 compared to 2023 (source_section=MD&A).",
      "source_id": "chunk_003",
      "file_path": "apple_10k_2024.pdf"
    },
    {
      "entity_name": "iphone_net_sales_fy2024_change",
      "entity_type": "FINANCIAL_METRIC",
      "description": "iPhone net sales decreased 1% or $2.0B during 2024 compared to 2023 (source_section=MD&A).",
      "source_id": "chunk_003",
      "file_path": "apple_10k_2024.pdf"
    }
  ],
  "relationships": [
    {
      "src_id": "apple_inc",
      "tgt_id": "net_sales_fy2024_change",
      "description": "Apple Inc. reported a net sales increase in 2024 compared to 2023.",
      "keywords": "REPORTED_METRIC,period=FY2024_vs_FY2023,source_section=MD&A",
      "source_id": "chunk_003",
      "weight": 1.0,
      "file_path": "apple_10k_2024.pdf"
    },
    {
      "src_id": "iphone",
      "tgt_id": "iphone_net_sales_fy2024_change",
      "description": "iPhone net sales decreased in 2024 compared to 2023.",
      "keywords": "REPORTED_METRIC,period=FY2024_vs_FY2023,source_section=MD&A",
      "source_id": "chunk_003",
      "weight": 1.0,
      "file_path": "apple_10k_2024.pdf"
    }
  ]
}
```

---

## After Extraction

- Run `ingest_from_file("extractions/{filename}_extracted.json")` via MCP
- If ingestion fails but extraction file exists, use `reingest_from_file(...)` — no re-extraction needed
- Call `list_pending_summaries()`. Preciso never compresses descriptions with an LLM — if any entity/relationship is flagged (its raw description history exceeded `raw_tail_size`), write a concise summary of `prior_summary` + `old_descriptions` and call `submit_summary(...)` for each one before considering the job done
- Sample queries to verify the graph:
  - `"What was Apple's revenue in FY2024?"`
  - `"Who are Apple's top executives and their compensation?"`
  - `"What are the top 5 risk factors disclosed?"`
  - `"Which segments saw revenue decline?"`

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
