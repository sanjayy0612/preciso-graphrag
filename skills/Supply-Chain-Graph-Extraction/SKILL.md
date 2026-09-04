---
name: supply-chain-graph-extraction
description: >
  Extract documented supply-chain dependencies from facility registers,
  manufacturing records, bills of materials, and product dependency documents
  into Preciso's strict supply-chain workspace. Use for facility-to-component-to-product
  dependency mapping, not order impact, inventory, delay forecasting, or live tracking.
---

# Supply-Chain Graph Extraction

Use this skill for the `supply_chain` workspace only. Its purpose is to create
an evidence-backed dependency map from documents, with no unsupported impact
or forecasting conclusions.

## Runtime contract

- Call `get_server_status(workspace="supply_chain")` before extraction.
- Read one verified source document at a time. Write one extraction JSON per
  source to `extractions/{source_filename}_extracted.json`.
- Ingest only with `ingest_from_file(..., workspace="supply_chain")`.
- The supply-chain profile rejects records outside its controlled vocabulary;
  do not fall back to the generic workspace to bypass validation.
- The dataset owner must provide a snapshot/effective date. Include it in each
  entity and relationship description. Do not describe the result as live.

## Controlled vocabulary

Only these entity types are accepted:

| Entity type | Canonical-ID pattern | Example |
|---|---|---|
| `COMPANY` | `company:{company-slug}` | `company:arkon-components` |
| `FACILITY` | `facility:{company-slug}:{facility-slug}` | `facility:arkon-components:northbridge` |
| `COMPONENT` | `component:{company-slug}:{component-slug}` | `component:arkon-components:control-unit-c17` |
| `PRODUCT` | `product:{product-family}:{model}` | `product:aquapump:300` |

Only these directed relationships are accepted. Put the relationship type as
the first comma-separated token in the string `keywords` field:

| Source | `keywords` | Target |
|---|---|---|
| `COMPANY` | `OPERATES` | `FACILITY` |
| `FACILITY` | `MANUFACTURES` | `COMPONENT` |
| `COMPONENT` | `USED_IN` | `PRODUCT` |

Do not use synonyms such as `MAKES`, `PRODUCES`, `CONTAINS`, or `DEPENDS_ON`.
Do not add relationships for alternatives, capacity, inventory, orders,
severity, delay, or forecasts in this version.

## Identity rules

Use the supplied canonical-ID registry when one exists.

- Reuse a canonical ID only when the document states it or explicitly proves a
  documented alias.
- Record the canonical ID as `entity_name`; put the human-readable name and
  any documented alias in `description`.
- Never merge a name merely because it looks similar. If a reference is
  ambiguous, do not create a dependency relationship from it. Surface it for
  human review instead.
- Do not invent a company, facility, component, product, or relationship to
  complete a path.

## Evidence rules

Every entity and relationship requires a `source_id` that names a real chunk
in the same extraction. Make each chunk a coherent source passage and preserve
its source file path.

```json
{
  "src_id": "facility:arkon-components:northbridge",
  "tgt_id": "component:arkon-components:control-unit-c17",
  "keywords": "MANUFACTURES,source_section=facility_register",
  "description": "Northbridge Fabrication Facility manufactures Control Unit C-17; effective snapshot 2026-01-15.",
  "source_id": "chunk_004",
  "file_path": "facility_register.md",
  "weight": 1.0
}
```

## Extraction procedure

1. Confirm the source is approved and identify its snapshot/effective date.
2. Read the canonical-ID registry before extracting aliases or cross-document
   references.
3. Chunk the source into evidence passages and create canonical entities only
   for directly documented facts.
4. Add only allowed typed relationships whose source and target entities both
   appear in the extraction payload.
5. Validate before ingestion:
   - `document_id`, `entities`, `relationships`, and `chunks` are present;
   - every entity and relationship has a real source chunk;
   - canonical IDs exactly match the registry or a documented scoped-ID rule;
   - every relationship matches one allowed source/type/target combination;
   - no ambiguous alias was merged automatically.
6. Ingest into the `supply_chain` workspace. If validation fails, correct the
   extraction rather than weakening the profile.

## Output shape

```json
{
  "document_id": "facility_register_2026_01_15",
  "file_path": "facility_register.md",
  "entities": [
    {
      "entity_name": "facility:arkon-components:northbridge",
      "entity_type": "FACILITY",
      "description": "Northbridge Fabrication Facility; effective snapshot 2026-01-15.",
      "source_id": "chunk_001",
      "file_path": "facility_register.md"
    }
  ],
  "relationships": [],
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "content": "Arkon Components operates Northbridge Fabrication Facility.",
      "chunk_order_index": 1,
      "file_path": "facility_register.md"
    }
  ]
}
```

## Boundaries

This skill produces documented graph facts only. A facility-unavailable event
is a future query input, not an entity or relationship to ingest. Any future
answer must say “potentially exposed through documented dependencies,” never
that a product will be delayed or materially affected.
