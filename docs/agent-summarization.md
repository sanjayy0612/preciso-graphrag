# Agent-handshake summarization (`GRAPHRAG_SUMMARY_MODE=agent`)

## Why this exists

Entity and relationship descriptions are stored as a **two-zone field**
(`core/summary.py`): at most one `<<SUM>>`-tagged rolling summary of older
mentions, plus a verbatim tail of the most recent `raw_tail_size`
descriptions. When the tail grows past `raw_tail_size` or the whole field
exceeds `summary_context_size`, the field needs compressing.

By default (`summary_mode="llm"`) the engine calls `llm_model_func` to fold
the old material into a new rolling summary. If you don't want the engine
calling an LLM at all — no API key, no local model, or you simply want the
summarizing intelligence to be the same agent driving the MCP session — set:

```bash
export GRAPHRAG_SUMMARY_MODE=agent
```

In this mode the engine **never** calls an LLM for summarization, even if
`llm_model_func` happens to be configured. When compression is needed it
instead:

1. Keeps the description **fully verbatim** (nothing is dropped or rewritten).
2. Writes a **pending record** (`GRAPH_IS_HERE/kv_store_pending_summaries.json`)
   flagging that entity or relationship as needing a summary.
3. Leaves the rest of the pipeline — ingestion, querying, exports — working
   exactly as before; `get_server_status`'s `pending_summaries` count is the
   only visible signal that work is waiting.

## The agent loop

After ingesting documents, close the loop yourself:

```
1. ingest_from_file(...)  /  ingest_graph_tool(...)   # as usual
2. list_pending_summaries(limit=50)
     -> for each item: { key, kind, name, src, tgt, reason,
                          content_to_summarize: {
                            prior_summary,     # existing rolling summary, or null
                            old_descriptions,  # verbatim facts aged out of the tail, oldest first
                            keep_tail,         # most recent facts — stay verbatim regardless
                          } }
3. For each item: read content_to_summarize, write a concise summary that
   preserves prior_summary + old_descriptions (do NOT re-summarize keep_tail —
   it stays verbatim in the field either way).
4. submit_summary(name=item.name, kind=item.kind, summary_text=..., src=item.src, tgt=item.tgt)
     -> stores "<<SUM>> {summary_text}" + keep_tail, re-embeds (marker stripped),
        clears the pending record.
5. Repeat list_pending_summaries until it returns no items.
```

Notes:

- `submit_summary` re-reads the **current** description before writing —
  if new merges arrived between step 2 and step 4, your summary still lands
  correctly on top of whatever `keep_tail` is current at submit time.
- Submitting the same `summary_text` again when the field already reflects
  it is a no-op success (idempotent).
- Re-ingesting the same document doesn't create duplicate pending records —
  the raw tail dedupes byte-for-byte, so nothing new crosses the threshold.
- `kind="relation"` requires both `src` and `tgt` (the sorted pair is the
  storage key internally); `kind="entity"` requires `name`.

## Other modes, for context

| `summary_mode` | LLM ever called for summaries? | Compression needed but can't compress |
|---|---|---|
| `"llm"` (default) | Yes, if `llm_model_func` is configured | Falls back to verbatim + `"summary_required"` |
| `"verbatim"` | Never | Verbatim + `"summary_required"` (no pending queue) |
| `"agent"` | Never | Verbatim + pending record, resolved via the tools above |

`"llm"` is unchanged from before this mode existed — all existing behavior
and tests are untouched. `"verbatim"` is the same no-LLM fallback `"llm"`
mode already had when `llm_model_func` is `None`, just explicit and
independent of whether an LLM happens to be configured.

## The `<<SUM>>` marker contract still applies

The marker is internal, storage-only bookkeeping (see `SUMMARY_MARKER` in
`config.py`). `content_to_summarize` from `list_pending_summaries` is built
from `old_descriptions`/`keep_tail`, which are never marker-tagged by
construction, and `submit_summary`'s tool response never echoes the stored
description back. `submit_summary` re-embeds through the same
marker-stripping path (`core.utils.strip_summary_marker`) every other exit
surface uses.
