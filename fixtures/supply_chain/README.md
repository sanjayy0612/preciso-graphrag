# Synthetic supply-chain snapshot

This is a deliberately synthetic, conflict-free corpus for Preciso's first
supply-chain capability. It is not a representation of a real company or a
complete private supply chain.

- **Effective snapshot date:** 2026-01-15
- **Scope:** documented company, facility, component, and product dependencies
- **Excludes:** orders, inventory, capacity, alternatives, delay prediction, and severity

`sources/` contains the raw documents. `canonical_id_registry.json` records
the canonical IDs and only aliases explicitly established by those documents.
`ground_truth.json` is independently authored evaluation data: it records the
expected facts, evidence references, and future facility-to-product paths. It
is not an ingestion payload or a query implementation.

`invalid/` contains intentionally invalid examples. They are never part of the
valid corpus and must remain rejected by the supply-chain profile.
