# Supply-chain evaluation (Stage 4)

This is a synthetic, single-snapshot evaluation. It establishes neither
production readiness nor general GraphRAG superiority.

## Frozen protocol and commands

The corpus, questions, scoring rules, retrieval method, and `top_k=2` were
frozen in [supply_chain_protocol.json](../evals/supply_chain_protocol.json)
before the results were run.

```bash
python3 -m evals.supply_chain_eval run-curated \
  --output evals/results/supply_chain_stage4_results.json

python3 -m evals.supply_chain_eval demo-northbridge \
  --output /tmp/northbridge-demo.json
```

The first command emits full per-question output; the committed result is a
concise recorded summary. The second emits the full cited response.

## Results

| Evaluation | Result | Meaning |
|---|---:|---|
| Curated extraction → traversal | paths P/R 1.00; products P/R 1.00; edge-evidence recall 1.00 | Deterministic traversal is correct for the four independently authored paths. This is not extraction accuracy. |
| Curated negative cases | 2/2 | `Plant 7` is unresolved; deleted BOM evidence fails closed. |
| Actual extraction → end-to-end | Blocked | No repository-owned external-agent/model runner is configured. No hand-authored extraction was substituted. |
| Retrieval baseline | evidence P/R 0.50; complete evidence 1/3 | Existing chunk vector retrieval, unchanged question, deterministic fallback embedding, `top_k=2`, no answer generation. |

The retrieval result includes losses: it retrieved the wrong evidence for
Northbridge, half of the required evidence for Lakeside, and all evidence for
Harbor. It cannot construct dependency paths; it was not presented as though
it could.

## Northbridge demonstration

The full result contains two paths, `is_truncated: false`, snapshot
`2026-01-15`, and edge-level citations:

- Northbridge → Control Unit C-17 → AquaPump 300
- Northbridge → Control Unit C-17 → AquaPump 500

Both `MANUFACTURES` edges cite `facility_001` from
`facility_register.md`: “Northbridge Fabrication Facility manufactures Control
Unit C-17.” Both `USED_IN` edges cite `bom_001` from `product_bom.md`:
“Control Unit C-17 is used in AquaPump 300 and AquaPump 500.” These establish
documented dependency paths only—not delays, inventory shortages, or business
impact.

## External extraction protocol

```bash
python3 -m evals.supply_chain_eval prepare-extractor --output-dir /tmp/supply-extraction-input
# Run an external agent once using only that directory; save its untouched output.
python3 -m evals.supply_chain_eval score-extraction \
  --input /tmp/supply-extraction-input/extraction_output.json \
  --output /tmp/extraction-score.json \
  --model '<model>' --settings '<settings>' --run-count 1
```

The bundle contains only source documents, the permitted canonical-ID registry,
and the extraction skill. It excludes gold extraction, ground truth, questions,
and answers. The scorer records strict validation rejections, entity and typed
relation precision/recall, canonical-ID accuracy, evidence support, ingestion,
and downstream path/product effects without correcting output.

## Storage limitation

Supply-chain sidecars use per-file atomic replacement plus a commit gate;
pending or failed ingestions block deterministic queries. This is not a
cross-file durable transaction with `fsync` guarantees.
