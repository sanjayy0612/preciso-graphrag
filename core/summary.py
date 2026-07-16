from __future__ import annotations

import json
from functools import partial

from config import DEFAULT_SUMMARY_LANGUAGE, PROMPTS, SUMMARY_MARKER
from core.storage.base import BaseKVStorage
from core.utils import (
    logger,
    strip_summary_marker,
    truncate_list_by_token_size,
    use_llm_func_with_cache,
)


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
    the LLM ignores the recommended length."""
    tokens = tokenizer.encode(summary)
    if len(tokens) <= summary_max_tokens:
        return summary
    truncated = tokenizer.decode(tokens[:summary_max_tokens])
    if truncated:
        return truncated
    # decode unavailable (fallback tokenizer): approximate by character ratio
    ratio = summary_max_tokens / len(tokens)
    return summary[: max(1, int(len(summary) * ratio))]


def _truncate_entity_identifier(
    identifier: str, limit: int, chunk_key: str, identifier_role: str
) -> str:
    if len(identifier) <= limit:
        return identifier
    preview = identifier[:20]
    logger.warning(
        "%s: %s len %d > %d chars (Name: '%s...')",
        chunk_key,
        identifier_role,
        len(identifier),
        limit,
        preview,
    )
    return identifier[:limit]


async def _summarize_descriptions(
    description_type: str,
    description_name: str,
    description_list: list[str],
    global_config: dict,
    llm_response_cache: BaseKVStorage | None = None,
) -> str:
    use_llm_func = global_config["llm_model_func"]
    if use_llm_func is None:
        return "\n".join(description_list)
    use_llm_func = partial(use_llm_func, _priority=8)
    language = global_config["addon_params"].get("language", DEFAULT_SUMMARY_LANGUAGE)
    summary_length_recommended = global_config["summary_length_recommended"]
    tokenizer = global_config["tokenizer"]
    summary_context_size = global_config["summary_context_size"]
    json_descriptions = [{"Description": desc} for desc in description_list]
    truncated_json_descriptions = truncate_list_by_token_size(
        json_descriptions,
        key=lambda x: json.dumps(x, ensure_ascii=False),
        max_token_size=summary_context_size,
        tokenizer=tokenizer,
    )
    joined_descriptions = "\n".join(
        json.dumps(desc, ensure_ascii=False) for desc in truncated_json_descriptions
    )
    use_prompt = PROMPTS["summarize_entity_descriptions"].format(
        description_type=description_type,
        description_name=description_name,
        description_list=joined_descriptions,
        summary_length=summary_length_recommended,
        language=language,
    )
    summary, _ = await use_llm_func_with_cache(
        use_prompt,
        use_llm_func,
        llm_response_cache=llm_response_cache,
        cache_type="summary",
    )
    return summary


async def _fold_into_summary(
    description_type: str,
    entity_or_relation_name: str,
    fold_list: list[str],
    global_config: dict,
    llm_response_cache: BaseKVStorage | None = None,
) -> str:
    """Reduce (prior rolling summary + aged-out verbatim descriptions) to ONE new
    rolling summary.

    If the fold input exceeds summary_context_size, reduce it in chronological
    order — summarize the largest oldest-first prefix that fits, then fold that
    intermediate summary with the remainder — so nothing is silently dropped by
    _summarize_descriptions' input truncation. This keeps compression
    single-lineage: old text only ever collapses forward in time.
    """
    tokenizer = global_config["tokenizer"]
    summary_context_size = global_config["summary_context_size"]
    current = [desc for desc in fold_list if desc and desc.strip()]
    if not current:
        return ""
    # Bounded rounds: each round shrinks the oldest prefix into one summary; the
    # cap only guards against an LLM that returns outputs as large as its input.
    for _ in range(len(current) + 2):
        total_tokens = sum(len(tokenizer.encode(desc)) for desc in current)
        if total_tokens <= summary_context_size or len(current) == 1:
            if len(current) == 1 and total_tokens > summary_context_size:
                logger.warning(
                    "Single description for %s exceeds summary_context_size (%d tokens); summarization input will be truncated",
                    entity_or_relation_name,
                    total_tokens,
                )
            return await _summarize_descriptions(
                description_type,
                entity_or_relation_name,
                current,
                global_config,
                llm_response_cache,
            )
        prefix: list[str] = []
        prefix_tokens = 0
        for desc in current:
            desc_tokens = len(tokenizer.encode(desc))
            if prefix and prefix_tokens + desc_tokens > summary_context_size:
                break
            prefix.append(desc)
            prefix_tokens += desc_tokens
        intermediate = await _summarize_descriptions(
            description_type,
            entity_or_relation_name,
            prefix,
            global_config,
            llm_response_cache,
        )
        current = [intermediate] + current[len(prefix):]
    return await _summarize_descriptions(
        description_type,
        entity_or_relation_name,
        current,
        global_config,
        llm_response_cache,
    )


async def _handle_entity_relation_summary(
    description_type: str,
    entity_or_relation_name: str,
    description_list: list[str],
    separator: str,
    global_config: dict,
    llm_response_cache: BaseKVStorage | None = None,
) -> tuple[str, bool, str | None]:
    """Two-zone description merge: at most ONE marker-tagged rolling summary of old
    mentions, followed by up to raw_tail_size verbatim recent descriptions. Only the
    summary zone is ever rewritten by the LLM; tail items stay byte-for-byte until
    they age out. See SUMMARY_MARKER in config.py.
    """
    if not description_list:
        return "", False, None
    tokenizer = global_config["tokenizer"]
    use_llm_func = global_config["llm_model_func"]
    summary_mode = global_config.get("summary_mode", "agent")
    summary_context_size = global_config["summary_context_size"]
    summary_max_tokens = global_config["summary_max_tokens"]
    # raw_tail_size supersedes the old force_llm_summary_on_merge count policy
    raw_tail_size = max(
        0,
        int(
            global_config.get(
                "raw_tail_size", global_config.get("force_llm_summary_on_merge", 4)
            )
        ),
    )

    prior_summary, raw_tail = _split_summary_zones(description_list)
    if prior_summary is None and not raw_tail:
        return "", False, None

    def zone_tokens(summary: str | None, tail: list[str]) -> int:
        parts = ([summary] if summary else []) + tail
        return sum(len(tokenizer.encode(part)) for part in parts)

    # Within bounds: store verbatim, no LLM. This is also the idempotent fast path —
    # re-ingesting the same file dedupes into the existing tail and returns the
    # identical string.
    if (
        len(raw_tail) <= raw_tail_size
        and zone_tokens(prior_summary, raw_tail) <= summary_context_size
    ):
        return _assemble_description(prior_summary, raw_tail, separator), False, None

    # "agent" mode: never call an LLM. Keep everything verbatim and defer
    # compression to the MCP-driving agent (see preciso_mcp/tools/pending_summaries_tool.py).
    # Checked before the use_llm_func is None branch so this holds even if an
    # LLM happens to be configured — the mode, not the func, decides here.
    if summary_mode == "agent":
        return (
            _assemble_description(prior_summary, raw_tail, separator),
            False,
            "summary_pending",
        )

    # "verbatim" mode, or compression needed but no LLM available: never call
    # the LLM, keep everything verbatim, and surface the degraded
    # "summary_required" signal (no pending queue involved).
    if summary_mode == "verbatim" or use_llm_func is None:
        return (
            _assemble_description(prior_summary, raw_tail, separator),
            False,
            "summary_required",
        )

    summary_reason = "merge_policy" if len(raw_tail) > raw_tail_size else "token_limit"

    # Age out the oldest tail items; the most recent raw_tail_size stay verbatim.
    keep_tail = raw_tail[len(raw_tail) - raw_tail_size:] if raw_tail_size else []
    overflow = raw_tail[: len(raw_tail) - len(keep_tail)]

    # Token overflow can persist after count-capping (few but huge descriptions):
    # keep shifting the oldest kept item into the fold until the future field
    # (summary budget + tail) fits. The newest description is evicted into the
    # summary only if it alone would blow the whole budget.
    summary_budget = min(summary_max_tokens, summary_context_size)
    while keep_tail and summary_budget + zone_tokens(None, keep_tail) > summary_context_size:
        overflow.append(keep_tail.pop(0))
        summary_reason = "token_limit"

    fold_list = ([prior_summary] if prior_summary else []) + overflow
    new_summary = await _fold_into_summary(
        description_type,
        entity_or_relation_name,
        fold_list,
        global_config,
        llm_response_cache,
    )
    new_summary = _enforce_summary_budget(new_summary, tokenizer, summary_max_tokens)
    return _assemble_description(new_summary, keep_tail, separator), True, summary_reason
