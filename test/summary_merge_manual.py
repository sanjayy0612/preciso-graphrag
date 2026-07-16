#!/usr/bin/env python
"""
Manual test for the two-zone description merge (core/summary.py).

Verifies that repeated merges of the same entity keep the most recent
raw_tail_size descriptions byte-for-byte verbatim, never compress anything
inside Preciso itself (compression is always deferred to the MCP-driving
agent — see preciso_mcp/tools/pending_summaries_tool.py), correctly flag
PENDING_SUMMARY_REASON once bounds are exceeded, and migrate legacy
(marker-less) descriptions without error.

Uses a deterministic tokenizer — no Ollama/network required.

Usage:
    python test/summary_merge_manual.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.summary import PENDING_SUMMARY_REASON, _handle_entity_relation_summary

RAW_TAIL_SIZE = 4
CONTEXT_SIZE = 200  # tokens (stub tokenizer: 1 token per whitespace word)
SUMMARY_MAX = 50

FAILURES: list[str] = []


class StubTokenizer:
    """Deterministic tokenizer: one token per whitespace-separated word."""

    def encode(self, text):
        return (text or "").split()

    def decode(self, tokens):
        return " ".join(tokens)


def make_config(raw_tail_size=RAW_TAIL_SIZE, context=CONTEXT_SIZE, summary_max=SUMMARY_MAX):
    return {
        "llm_model_func": None,  # summary compression never uses an LLM
        "tokenizer": StubTokenizer(),
        "summary_context_size": context,
        "summary_max_tokens": summary_max,
        "raw_tail_size": raw_tail_size,
    }


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def segments_of(description: str) -> tuple[list[str], list[str]]:
    """Split a stored description into (marked summary segments, verbatim tail)."""
    segs = description.split(GRAPH_FIELD_SEP) if description else []
    marked = [s for s in segs if s.lstrip().startswith(SUMMARY_MARKER)]
    tail = [s for s in segs if s and not s.lstrip().startswith(SUMMARY_MARKER)]
    return marked, tail


def merge_once(state: str, new_descs: list[str], config) -> tuple[str, str | None]:
    """Mimic merge.py: split existing description by SEP, append incoming."""
    existing = state.split(GRAPH_FIELD_SEP) if state.strip() else []
    return _handle_entity_relation_summary(existing + new_descs, GRAPH_FIELD_SEP, config)


def test_stays_verbatim_and_flags_pending_over_threshold():
    print("\n[1] N merges: stays fully verbatim, flags pending, bounded by count")
    config = make_config()
    n = 12
    descs = [f"Description number {i} states a distinct verifiable fact about the entity." for i in range(n)]
    state = ""
    reason = None
    for i in range(n):
        state, reason = merge_once(state, [descs[i]], config)
    check(state == GRAPH_FIELD_SEP.join(descs), "Preciso never compresses descriptions itself")
    check(reason == PENDING_SUMMARY_REASON, "flagged pending once raw_tail_size is exceeded")
    marked, tail = segments_of(state)
    check(marked == [], "no marker written until an agent calls submit_summary")
    check(tail == descs, "all descriptions remain in the verbatim tail")


def test_no_pending_below_threshold():
    print("\n[2] Small merges below tail size never get flagged pending")
    config = make_config()
    state = ""
    descs = ["Fact one about entity.", "Fact two about entity.", "Fact three about entity."]
    reason = None
    for d in descs:
        state, reason = merge_once(state, [d], config)
    check(state == GRAPH_FIELD_SEP.join(descs), "all descriptions stored verbatim, no marker")
    check(reason is None, "no pending flag below raw_tail_size")


def test_idempotent_reingest():
    print("\n[3] Re-ingesting duplicate descriptions is a no-op")
    config = make_config()
    state = ""
    for i in range(8):
        state, _ = merge_once(state, [f"Unique fact {i} about the entity."], config)
    restate, _reason = merge_once(state, ["Unique fact 7 about the entity."], config)
    check(restate == state, "output identical after duplicate re-ingest")


def test_legacy_migration():
    print("\n[4] Legacy marker-less description merges without error")
    config = make_config()
    legacy_descs = [f"Legacy stored description {i} from an old graph." for i in range(8)]
    legacy = GRAPH_FIELD_SEP.join(legacy_descs)
    state, reason = merge_once(legacy, ["A brand new incoming description."], config)
    check(
        state == GRAPH_FIELD_SEP.join(legacy_descs + ["A brand new incoming description."]),
        "legacy field stays verbatim and readable",
    )
    check(reason == PENDING_SUMMARY_REASON, "9 raw descriptions > raw_tail_size flags pending")


def test_giant_single_description():
    print("\n[5] Single description larger than summary_context_size")
    config = make_config()
    giant = " ".join(f"word{i}" for i in range(CONTEXT_SIZE * 3))
    state, reason = merge_once("", [giant], config)
    check(state == giant, "giant description never truncated by Preciso itself")
    check(reason == PENDING_SUMMARY_REASON, "flagged pending on token overflow")


def test_small_tail_sizes():
    print("\n[6] raw_tail_size = 1 and 0")
    for size in (1, 0):
        config = make_config(raw_tail_size=size)
        state = ""
        reason = None
        for i in range(6):
            state, reason = merge_once(state, [f"Tail size {size} fact {i}."], config)
        check(reason == PENDING_SUMMARY_REASON, f"raw_tail_size={size}: flagged pending")


def test_existing_marker_segment_preserved():
    print("\n[7] Existing marker segment (written earlier by submit_summary) is preserved")
    config = make_config()
    state = GRAPH_FIELD_SEP.join([f"{SUMMARY_MARKER} an existing agent summary", "A verbatim fact."])
    state, _reason = merge_once(state, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    check(marked == [f"{SUMMARY_MARKER} an existing agent summary"], "existing summary segment untouched")
    check(tail == ["A verbatim fact.", "Another verbatim fact."], "tail correctly appended")


def test_defensive_multiple_markers():
    print("\n[8] Multiple marker segments fold into one on read")
    config = make_config()
    corrupted = GRAPH_FIELD_SEP.join(
        [f"{SUMMARY_MARKER} first old summary", f"{SUMMARY_MARKER} second old summary", "A verbatim fact."]
    )
    state, _reason = merge_once(corrupted, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    check(len(marked) == 1, "duplicate markers collapsed to one segment")
    check(tail == ["A verbatim fact.", "Another verbatim fact."], "tail untouched")


async def main() -> None:
    test_stays_verbatim_and_flags_pending_over_threshold()
    test_no_pending_below_threshold()
    test_idempotent_reingest()
    test_legacy_migration()
    test_giant_single_description()
    test_small_tail_sizes()
    test_existing_marker_segment_preserved()
    test_defensive_multiple_markers()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
