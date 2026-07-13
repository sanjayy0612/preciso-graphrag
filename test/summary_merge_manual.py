#!/usr/bin/env python
"""
Manual test for the two-zone description merge (core/summary.py).

Verifies that repeated merges of the same entity keep the most recent
raw_tail_size descriptions byte-for-byte verbatim, maintain exactly one
marker-tagged rolling summary, stay under the token ceiling regardless of N,
and migrate legacy (marker-less) descriptions without error.

Uses a deterministic LLM stub and tokenizer — no Ollama/network required.

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
from core.summary import _handle_entity_relation_summary, strip_summary_marker

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


class StubLLM:
    """Deterministic summarizer stub; counts calls."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, prompt, system_prompt=None, **kwargs):
        self.calls += 1
        return f"stub rolling summary number {self.calls}"


def make_config(llm, raw_tail_size=RAW_TAIL_SIZE, context=CONTEXT_SIZE, summary_max=SUMMARY_MAX):
    return {
        "llm_model_func": llm,
        "tokenizer": StubTokenizer(),
        "summary_context_size": context,
        "summary_max_tokens": summary_max,
        "summary_length_recommended": 30,
        "raw_tail_size": raw_tail_size,
        "force_llm_summary_on_merge": raw_tail_size,  # legacy alias
        "addon_params": {},
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


async def merge_once(state: str, new_descs: list[str], config) -> tuple[str, bool, str | None]:
    """Mimic merge.py: split existing description by SEP, append incoming."""
    existing = state.split(GRAPH_FIELD_SEP) if state.strip() else []
    return await _handle_entity_relation_summary(
        "Entity", "test_entity", existing + new_descs, GRAPH_FIELD_SEP, config
    )


async def test_verbatim_tail_and_single_marker():
    print("\n[1] N merges: verbatim tail, single marker, bounded tokens")
    llm = StubLLM()
    config = make_config(llm)
    tokenizer = config["tokenizer"]
    n = 12
    descs = [f"Description number {i} states a distinct verifiable fact about the entity." for i in range(n)]
    state = ""
    for i in range(n):
        state, _, _ = await merge_once(state, [descs[i]], config)
        tokens = len(tokenizer.encode(state))
        if tokens > CONTEXT_SIZE:
            check(False, f"token bound violated at merge {i}: {tokens} > {CONTEXT_SIZE}")
            return
    marked, tail = segments_of(state)
    check(len(marked) == 1, f"exactly one {SUMMARY_MARKER} segment (got {len(marked)})")
    check(tail == descs[-RAW_TAIL_SIZE:], f"last {RAW_TAIL_SIZE} descriptions byte-for-byte verbatim")
    check(len(tokenizer.encode(state)) <= CONTEXT_SIZE, "total tokens under summary_context_size")
    check(llm.calls > 0, "LLM summarized aged-out mentions")
    check(SUMMARY_MARKER not in strip_summary_marker(state), "strip_summary_marker removes marker")


async def test_no_llm_below_threshold():
    print("\n[2] Small merges below tail size never call the LLM (old defect fixed)")
    llm = StubLLM()
    config = make_config(llm)
    state = ""
    descs = ["Fact one about entity.", "Fact two about entity.", "Fact three about entity."]
    for d in descs:
        state, used, reason = await merge_once(state, [d], config)
    check(llm.calls == 0, "no LLM call at 3 tiny descriptions")
    check(state == GRAPH_FIELD_SEP.join(descs), "all descriptions stored verbatim, no marker")
    check(reason is None, "no summary_event emitted")


async def test_idempotent_reingest():
    print("\n[3] Re-ingesting duplicate descriptions is a no-op")
    llm = StubLLM()
    config = make_config(llm)
    state = ""
    for i in range(8):
        state, _, _ = await merge_once(state, [f"Unique fact {i} about the entity."], config)
    calls_before = llm.calls
    restate, used, reason = await merge_once(state, ["Unique fact 7 about the entity."], config)
    check(restate == state, "output identical after duplicate re-ingest")
    check(llm.calls == calls_before and not used, "no LLM call on duplicate re-ingest")


async def test_legacy_migration():
    print("\n[4] Legacy marker-less description migrates without error")
    llm = StubLLM()
    config = make_config(llm)
    legacy_descs = [f"Legacy stored description {i} from an old graph." for i in range(8)]
    legacy = GRAPH_FIELD_SEP.join(legacy_descs)
    state, used, _ = await merge_once(legacy, ["A brand new incoming description."], config)
    marked, tail = segments_of(state)
    check(len(marked) == 1, "legacy field gains exactly one summary segment")
    expected_tail = (legacy_descs + ["A brand new incoming description."])[-RAW_TAIL_SIZE:]
    check(tail == expected_tail, "most recent items kept verbatim during migration")
    check(used, "LLM folded the aged-out legacy items")


async def test_degraded_no_llm():
    print("\n[5] llm_model_func=None: no LLM, verbatim join, summary_required signal")
    config = make_config(None)
    descs = [f"Degraded mode description {i}." for i in range(8)]
    state, used, reason = await _handle_entity_relation_summary(
        "Entity", "test_entity", descs, GRAPH_FIELD_SEP, config
    )
    check(state == GRAPH_FIELD_SEP.join(descs), "everything kept verbatim without an LLM")
    check(not used, "LLM not used")
    check(reason == "summary_required", "degraded signal preserved")


async def test_giant_single_description():
    print("\n[6] Single description larger than summary_context_size")
    llm = StubLLM()
    config = make_config(llm)
    giant = " ".join(f"word{i}" for i in range(CONTEXT_SIZE * 3))
    state, used, reason = await merge_once("", [giant], config)
    tokens = len(config["tokenizer"].encode(state))
    check(tokens <= CONTEXT_SIZE, f"giant description compressed to {tokens} tokens")
    check(used and reason == "token_limit", "compression reported as token_limit")
    marked, _ = segments_of(state)
    check(len(marked) == 1, "giant description folded into the summary zone")


async def test_small_tail_sizes():
    print("\n[7] raw_tail_size = 1 and 0")
    for size in (1, 0):
        llm = StubLLM()
        config = make_config(llm, raw_tail_size=size)
        state = ""
        for i in range(6):
            state, _, _ = await merge_once(state, [f"Tail size {size} fact {i}."], config)
        marked, tail = segments_of(state)
        check(len(marked) == 1, f"raw_tail_size={size}: one summary segment")
        check(len(tail) == size, f"raw_tail_size={size}: tail holds {size} item(s)")
        if size:
            check(tail == [f"Tail size {size} fact 5."], "newest item verbatim")


async def test_defensive_multiple_markers():
    print("\n[8] Multiple marker segments fold into one")
    llm = StubLLM()
    config = make_config(llm)
    corrupted = GRAPH_FIELD_SEP.join(
        [f"{SUMMARY_MARKER} first old summary", f"{SUMMARY_MARKER} second old summary", "A verbatim fact."]
    )
    state, _, _ = await merge_once(corrupted, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    check(len(marked) == 1, "duplicate markers collapsed to one segment")
    check(tail == ["A verbatim fact.", "Another verbatim fact."], "tail untouched")


async def main() -> None:
    await test_verbatim_tail_and_single_marker()
    await test_no_llm_below_threshold()
    await test_idempotent_reingest()
    await test_legacy_migration()
    await test_degraded_no_llm()
    await test_giant_single_description()
    await test_small_tail_sizes()
    await test_defensive_multiple_markers()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
