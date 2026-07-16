from __future__ import annotations

from config import SUMMARY_MARKER


# Reason surfaced on the merge return tuple (and mirrored into the pending-summary
# queue record) when compression is needed. Preciso never compresses descriptions
# itself — see preciso_mcp/tools/pending_summaries_tool.py, which is the only path
# that ever writes a SUMMARY_MARKER segment.
PENDING_SUMMARY_REASON = "summary_pending"


def _split_summary_zones(description_list: list[str]) -> tuple[str | None, list[str]]:
    """Partition SEP-split segments into (rolling summary or None, verbatim raw tail).

    Legacy fields carry no marker, so everything lands in the raw tail and the first
    qualifying merge migrates them forward. Multiple marked segments should never
    occur but are defensively folded into one. The tail is deduplicated (first
    occurrence kept, byte-for-byte) so re-ingesting the same document is idempotent
    instead of growing the tail and re-triggering compression.
    """
    summaries: list[str] = []
    raw_tail: list[str] = []
    seen: set[str] = set()
    for seg in description_list:
        if not seg or not seg.strip():
            continue
        stripped = seg.lstrip()
        if stripped.startswith(SUMMARY_MARKER):
            summary_text = stripped[len(SUMMARY_MARKER):].strip()
            if summary_text:
                summaries.append(summary_text)
        elif seg not in seen:
            seen.add(seg)
            raw_tail.append(seg)
    prior_summary = " ".join(summaries) if summaries else None
    return prior_summary, raw_tail


def _assemble_description(prior_summary: str | None, raw_tail: list[str], separator: str) -> str:
    segments = []
    if prior_summary:
        segments.append(f"{SUMMARY_MARKER} {prior_summary}")
    segments.extend(raw_tail)
    return separator.join(segments)


def _enforce_summary_budget(summary: str, tokenizer, summary_max_tokens: int) -> str:
    """Hard cap on the rolling summary so the field cannot ratchet upward even if
    the agent submits something far longer than requested."""
    tokens = tokenizer.encode(summary)
    if len(tokens) <= summary_max_tokens:
        return summary
    truncated = tokenizer.decode(tokens[:summary_max_tokens])
    if truncated:
        return truncated
    # decode unavailable (fallback tokenizer): approximate by character ratio
    ratio = summary_max_tokens / len(tokens)
    return summary[: max(1, int(len(summary) * ratio))]


def resolve_raw_tail_size(global_config: dict) -> int:
    return max(0, int(global_config.get("raw_tail_size", 4)))


def _handle_entity_relation_summary(
    description_list: list[str],
    separator: str,
    global_config: dict,
) -> tuple[str, str | None]:
    """Two-zone description merge: at most ONE marker-tagged rolling summary of old
    mentions, followed by up to raw_tail_size verbatim recent descriptions.

    Preciso never calls an LLM to compress descriptions. When the field grows past
    bounds, it stays fully verbatim and the entity/relation is flagged
    PENDING_SUMMARY_REASON so the MCP-driving agent can compress it via
    list_pending_summaries / submit_summary (preciso_mcp/tools/pending_summaries_tool.py)
    — that tool is the only place a SUMMARY_MARKER segment is ever written.
    """
    if not description_list:
        return "", None
    tokenizer = global_config["tokenizer"]
    summary_context_size = global_config["summary_context_size"]
    raw_tail_size = resolve_raw_tail_size(global_config)

    prior_summary, raw_tail = _split_summary_zones(description_list)
    if prior_summary is None and not raw_tail:
        return "", None

    def zone_tokens(summary: str | None, tail: list[str]) -> int:
        parts = ([summary] if summary else []) + tail
        return sum(len(tokenizer.encode(part)) for part in parts)

    # Within bounds: store verbatim. This is also the idempotent fast path —
    # re-ingesting the same file dedupes into the existing tail and returns the
    # identical string.
    if (
        len(raw_tail) <= raw_tail_size
        and zone_tokens(prior_summary, raw_tail) <= summary_context_size
    ):
        return _assemble_description(prior_summary, raw_tail, separator), None

    return _assemble_description(prior_summary, raw_tail, separator), PENDING_SUMMARY_REASON
