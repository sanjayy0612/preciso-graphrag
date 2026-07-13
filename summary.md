# Description-Merge Fix: Two-Zone Rolling Summary + Marker-Stripping Contract

## The defect

Entity and relationship descriptions are stored as one `<SEP>`-joined string and
re-merged every time a document is ingested. The old policy in
`core/summary.py::_handle_entity_relation_summary` was count-based: as soon as an
entity accumulated `force_llm_summary_on_merge` (default 3) descriptions, the LLM
rewrote **all** of them into a single paraphrase — even when the combined text was
tiny. This had four compounding consequences:

1. **Premature and silent** — a hardcoded engine-side policy fired with no agent
   involvement, long before any size limit demanded it.
2. **Provenance destruction** — the raw, source-grounded descriptions were deleted
   and replaced by one LLM-authored string. (Real evidence in the shipped graph:
   node `walmart_u_s_net_sales_2022` contains the phrase "(included here for YoY
   comparison)", which appears in no source document — it is the LLM's own
   synthesis, now the only surviving copy.)
3. **Summary-of-summary drift** — the returned summary carried no marker, so the
   next merge treated it as just another raw description and re-summarized it
   together with fresh text. Each pass lost more original wording.
4. **Retrieval contamination** — the description *is* the retrieval surface: it is
   embedded into `entities_vdb`/`relationships_vdb` and returned by query
   synthesis. Collapsing raw text into paraphrase meant vector search matched
   against, and answers cited, model-authored text instead of verbatim source —
   in a system that advertises faithfulness ~1.0.

The constraint that makes "just don't summarize" impossible: popular entities
appear in dozens of documents, and unbounded raw text would blow past
`summary_context_size` and the embedding budget. Compression is mandatory at some
point; the fix has to bound size, not merely avoid the LLM.

## The fix: two zones inside the same field

Each description field now holds **two clearly separated zones** inside the same
`<SEP>`-joined string — no schema change, no storage-format change:

```
<<SUM>> <one bounded rolling summary of OLD mentions>
<SEP> <verbatim recent description 1>
<SEP> ...up to raw_tail_size verbatim items (default 4)
```

Merge algorithm (one shared helper for entities and relations, so the two paths
cannot drift):

- Split the field into the (at most one) `<<SUM>>`-tagged rolling summary and the
  untagged verbatim tail; append incoming descriptions to the tail, deduplicated
  byte-for-byte so re-ingesting the same file is a no-op.
- If the tail is within `raw_tail_size` **and** the whole field fits in
  `summary_context_size`: store as-is. **No LLM call.** This is the common case
  the old code got wrong.
- Otherwise, age out only the *oldest* tail items and fold them — together with
  the prior rolling summary — into one new summary, hard-capped at
  `summary_max_tokens`. The most recent `raw_tail_size` descriptions stay
  byte-for-byte verbatim. Oversized fold inputs are reduced oldest-first in
  bounded rounds so nothing is silently truncated away.

### Guarantees

- **Bounded size**: exactly one capped summary segment + at most `raw_tail_size`
  verbatim items; the field cannot grow past the token ceiling regardless of how
  many documents mention the entity.
- **Recent wording preserved verbatim**: only genuinely old mentions are
  compressed, and the LLM only ever rewrites the summary zone.
- **Labeled, single-lineage compression**: the marker means raw text is never
  mistaken for a summary and re-cooked blindly. Summary-of-summary still occurs
  for ancient history — that is the honest tradeoff: bounded, *labeled* lossy
  compression, because perfect provenance under a hard token cap is impossible.
- **Backward compatible**: legacy marker-less fields are treated as all-tail and
  migrate forward on the first qualifying merge. Degraded mode
  (`llm_model_func=None`) never calls the LLM and preserves the
  `summary_required` signal; `pipeline_status` summary events still report
  reason + description count.

## The marker contract: storage keeps it, every exit strips it

`<<SUM>>` is internal bookkeeping. It must persist inside `GRAPH_IS_HERE/`
(including the portable `graph_graph.graphml`) so the next merge can find the
summary segment — but it must never appear on any outward surface. Initial
implementation stripped it at two exits; review found four more, now all closed:

| Exit surface | Where stripped |
|---|---|
| Embedding content (entity + relationship) | `core/merge.py` |
| Relationship VDB `description` payload | `core/merge.py` (stripped at write, so the marker never enters the derived store) |
| LLM query context | `core/query.py::_apply_token_truncation` |
| User-facing query results | `core/utils.py::convert_to_user_format` |
| Neo4j export properties | `core/export_adapters.py::_node_properties` / `_edge_properties` |
| Qdrant export payloads | `core/export_adapters.py::_vector_payload` (deep-strips every value, belt-and-suspenders against future upstream slips) |

The canonical helper is `core.utils.strip_summary_marker` (it lives in `utils`
rather than `summary` to avoid a circular import), and the full contract is
documented in one place: the comment block above `SUMMARY_MARKER` in `config.py`.

## Config additions

- `raw_tail_size` (default 4), env `GRAPHRAG_RAW_TAIL_SIZE`.
- `GRAPHRAG_FORCE_LLM_SUMMARY_ON_MERGE` is honored as a legacy alias for the tail
  size; the old "collapse everything at N descriptions" policy is gone.
- `SUMMARY_MARKER = "<<SUM>>"` — deliberately **not** env-configurable, since
  changing it would orphan markers already persisted in existing graphs.

## Validation (no pytest suite in this repo; manual per CONTRIBUTING.md)

- `python3 -m compileall core ingest mcp` — clean.
- `python3 test/summary_merge_manual.py` — 26 checks over the merge logic:
  verbatim tail across N merges, single marker segment, bounded tokens, no LLM
  below thresholds, idempotent re-ingest, legacy migration, degraded no-LLM mode,
  giant single description, `raw_tail_size` 0/1, defensive multi-marker folding.
  Deterministic LLM/tokenizer stubs; no Ollama/network needed.
- `python3 test/marker_leak_manual.py` — guards the *invariant*, not just the
  merge: drives the real `_merge_nodes_then_upsert` / `_merge_edges_then_upsert`
  write path until a summary is forced, then asserts the marker IS in stored
  graph data but ABSENT from every VDB payload, every `convert_to_user_format`
  field, the Neo4j property dicts, and the Qdrant payload builder (including a
  hostile marker-bearing record). Any future exit surface should be added here.
