# Agent-driven summarization

## Why this exists

Entity and relationship descriptions are stored as a **two-zone field**
(`core/summary.py`): at most one `<<SUM>>`-tagged rolling summary of older
mentions, plus a verbatim tail of the most recent `raw_tail_size`
descriptions. When the tail grows past `raw_tail_size` or the whole field
exceeds `summary_context_size`, the field needs compressing.

**Preciso never calls an LLM to do this.** There is no summarization LLM
config, key, or endpoint anywhere in the engine. When compression is needed,
the engine instead:

1. Keeps the description **fully verbatim** (nothing is dropped or rewritten).
2. Writes a **pending record** (`GRAPH_IS_HERE/kv_store_pending_summaries.json`)
   flagging that entity or relationship as needing a summary.
3. Leaves the rest of the pipeline — ingestion, querying, exports — working
   exactly as before; `get_server_status`'s `pending_summaries` count is the
   only visible signal that work is waiting.

Compressing the field is entirely the job of the MCP-driving agent (Claude
Code, Codex, etc.) via the two tools below — the same agent that already
reads documents and drives ingestion.

## The agent loop

After ingesting documents, close the loop yourself:

```
1. ingest_from_file(...)  /  ingest_graph_tool(...)   # as usual
2. list_pending_summaries(limit=50)
     -> for each item: { key, kind, name, src, tgt, description_count,
                          content_to_summarize: {
                            prior_summary,     # existing rolling summary, or null
                            old_descriptions,  # verbatim facts aged out of the tail, oldest first
                            keep_tail,         # most recent facts — stay verbatim regardless
                          } }
3. For each item: read content_to_summarize, write a concise summary that
   preserves prior_summary + old_descriptions (do NOT re-summarize keep_tail —
   it stays verbatim in the field either way).
4. submit_summary(name=item.name, kind=item.kind, summary_text=...,
                   expected_description_count=item.description_count,
                   src=item.src, tgt=item.tgt)
     -> stores "<<SUM>> {summary_text}" + keep_tail, re-embeds (marker stripped),
        clears the pending record.
5. Repeat list_pending_summaries until it returns no items.
```

Notes:

- `expected_description_count` must be the `description_count` you just read
  from `list_pending_summaries` for that item. It's an optimistic-concurrency
  guard: if a new merge landed new descriptions between step 2 and step 4, the
  live count won't match and `submit_summary` returns an error instead of
  silently dropping whatever aged into `old_descriptions` since you last read
  it — re-fetch via `list_pending_summaries` and retry.
- Submitting the same `summary_text` again when the field already reflects
  it is a no-op success (idempotent).
- Re-ingesting the same document doesn't create duplicate pending records —
  the raw tail dedupes byte-for-byte, so nothing new crosses the threshold.
- `kind="relation"` requires both `src` and `tgt` (the sorted pair is the
  storage key internally); `kind="entity"` requires `name`.

## The `<<SUM>>` marker contract still applies

The marker is internal, storage-only bookkeeping (see `SUMMARY_MARKER` in
`config.py`). `content_to_summarize` from `list_pending_summaries` is built
from `old_descriptions`/`keep_tail`, which are never marker-tagged by
construction, and `submit_summary`'s tool response never echoes the stored
description back. `submit_summary` re-embeds through the same
marker-stripping path (`core.utils.strip_summary_marker`) every other exit
surface uses.
