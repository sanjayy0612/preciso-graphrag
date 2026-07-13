"""core/summary.py two-zone description merge — pytest port of
test/summary_merge_manual.py (which stays as the runnable manual script).

Invariants: at most ONE marker-tagged rolling summary, the newest
raw_tail_size descriptions stay byte-for-byte verbatim, total tokens stay
bounded, and legacy marker-less fields migrate cleanly."""

from __future__ import annotations

import pytest

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.summary import _handle_entity_relation_summary, strip_summary_marker
from tests._stubs import StubLLM, make_merge_config, segments_of

RAW_TAIL_SIZE = 4
CONTEXT_SIZE = 200
SUMMARY_MAX = 50


def make_config(llm, **overrides):
    return make_merge_config(
        llm,
        raw_tail_size=overrides.pop("raw_tail_size", RAW_TAIL_SIZE),
        context=CONTEXT_SIZE,
        summary_max=SUMMARY_MAX,
        **overrides,
    )


async def merge_once(state: str, new_descs: list[str], config):
    """Mimic merge.py: split existing description by SEP, append incoming."""
    existing = state.split(GRAPH_FIELD_SEP) if state.strip() else []
    return await _handle_entity_relation_summary(
        "Entity", "test_entity", existing + new_descs, GRAPH_FIELD_SEP, config
    )


async def test_verbatim_tail_single_marker_bounded_tokens():
    llm = StubLLM()
    config = make_config(llm)
    tokenizer = config["tokenizer"]
    n = 12
    descs = [
        f"Description number {i} states a distinct verifiable fact about the entity."
        for i in range(n)
    ]
    state = ""
    for i in range(n):
        state, _, _ = await merge_once(state, [descs[i]], config)
        assert len(tokenizer.encode(state)) <= CONTEXT_SIZE, f"token bound violated at merge {i}"
    marked, tail = segments_of(state)
    assert len(marked) == 1
    assert tail == descs[-RAW_TAIL_SIZE:]
    assert llm.calls > 0
    assert SUMMARY_MARKER not in strip_summary_marker(state)


async def test_no_llm_below_threshold():
    llm = StubLLM()
    config = make_config(llm)
    state = ""
    descs = ["Fact one about entity.", "Fact two about entity.", "Fact three about entity."]
    for d in descs:
        state, used, reason = await merge_once(state, [d], config)
    assert llm.calls == 0
    assert state == GRAPH_FIELD_SEP.join(descs)
    assert reason is None


async def test_idempotent_reingest():
    llm = StubLLM()
    config = make_config(llm)
    state = ""
    for i in range(8):
        state, _, _ = await merge_once(state, [f"Unique fact {i} about the entity."], config)
    calls_before = llm.calls
    restate, used, _ = await merge_once(state, ["Unique fact 7 about the entity."], config)
    assert restate == state
    assert llm.calls == calls_before and not used


async def test_legacy_marker_less_field_migrates():
    llm = StubLLM()
    config = make_config(llm)
    legacy_descs = [f"Legacy stored description {i} from an old graph." for i in range(8)]
    legacy = GRAPH_FIELD_SEP.join(legacy_descs)
    state, used, _ = await merge_once(legacy, ["A brand new incoming description."], config)
    marked, tail = segments_of(state)
    assert len(marked) == 1
    assert tail == (legacy_descs + ["A brand new incoming description."])[-RAW_TAIL_SIZE:]
    assert used


async def test_degraded_no_llm_keeps_verbatim_and_signals():
    config = make_config(None)
    descs = [f"Degraded mode description {i}." for i in range(8)]
    state, used, reason = await _handle_entity_relation_summary(
        "Entity", "test_entity", descs, GRAPH_FIELD_SEP, config
    )
    assert state == GRAPH_FIELD_SEP.join(descs)
    assert not used
    assert reason == "summary_required"


async def test_giant_single_description_compressed():
    llm = StubLLM()
    config = make_config(llm)
    giant = " ".join(f"word{i}" for i in range(CONTEXT_SIZE * 3))
    state, used, reason = await merge_once("", [giant], config)
    assert len(config["tokenizer"].encode(state)) <= CONTEXT_SIZE
    assert used and reason == "token_limit"
    marked, _ = segments_of(state)
    assert len(marked) == 1


@pytest.mark.parametrize("size", [1, 0])
async def test_small_tail_sizes(size):
    llm = StubLLM()
    config = make_config(llm, raw_tail_size=size)
    state = ""
    for i in range(6):
        state, _, _ = await merge_once(state, [f"Tail size {size} fact {i}."], config)
    marked, tail = segments_of(state)
    assert len(marked) == 1
    assert len(tail) == size
    if size:
        assert tail == [f"Tail size {size} fact 5."]


async def test_multiple_marker_segments_fold_into_one():
    llm = StubLLM()
    config = make_config(llm)
    corrupted = GRAPH_FIELD_SEP.join(
        [
            f"{SUMMARY_MARKER} first old summary",
            f"{SUMMARY_MARKER} second old summary",
            "A verbatim fact.",
        ]
    )
    state, _, _ = await merge_once(corrupted, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    assert len(marked) == 1
    assert tail == ["A verbatim fact.", "Another verbatim fact."]


async def test_empty_and_single_description_passthrough():
    config = make_config(StubLLM())
    state, used, reason = await _handle_entity_relation_summary(
        "Entity", "test_entity", [], GRAPH_FIELD_SEP, config
    )
    assert state == "" and not used and reason is None
    state, used, _ = await _handle_entity_relation_summary(
        "Entity", "test_entity", ["Only fact."], GRAPH_FIELD_SEP, config
    )
    assert state == "Only fact." and not used
