"""core/summary.py two-zone description merge — pytest port of
test/summary_merge_manual.py (which stays as the runnable manual script).

Preciso never compresses descriptions itself: when the field grows past bounds
it stays fully verbatim and is flagged PENDING_SUMMARY_REASON for the
MCP-driving agent (preciso_mcp/tools/pending_summaries_tool.py) to compress.
These tests cover the zone-bookkeeping in core/summary.py only — the agent
compression path (list_pending_summaries / submit_summary, writing an actual
SUMMARY_MARKER segment) is covered by tests/test_agent_summary_mode.py."""

from __future__ import annotations

import pytest

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.summary import PENDING_SUMMARY_REASON, _handle_entity_relation_summary
from tests._stubs import make_merge_config, segments_of

RAW_TAIL_SIZE = 4
CONTEXT_SIZE = 200
SUMMARY_MAX = 50


def make_config(**overrides):
    return make_merge_config(
        raw_tail_size=overrides.pop("raw_tail_size", RAW_TAIL_SIZE),
        context=CONTEXT_SIZE,
        summary_max=SUMMARY_MAX,
        **overrides,
    )


def merge_once(state: str, new_descs: list[str], config):
    """Mimic merge.py: split existing description by SEP, append incoming."""
    existing = state.split(GRAPH_FIELD_SEP) if state.strip() else []
    return _handle_entity_relation_summary(existing + new_descs, GRAPH_FIELD_SEP, config)


def test_stays_verbatim_below_threshold():
    config = make_config()
    state = ""
    descs = ["Fact one about entity.", "Fact two about entity.", "Fact three about entity."]
    for d in descs:
        state, reason = merge_once(state, [d], config)
    assert state == GRAPH_FIELD_SEP.join(descs)
    assert reason is None


def test_over_threshold_flags_pending_and_stays_verbatim():
    config = make_config()
    n = 12
    descs = [
        f"Description number {i} states a distinct verifiable fact about the entity."
        for i in range(n)
    ]
    state = ""
    for i in range(n):
        state, reason = merge_once(state, [descs[i]], config)
    assert state == GRAPH_FIELD_SEP.join(descs)  # never compressed by Preciso itself
    assert reason == PENDING_SUMMARY_REASON
    marked, tail = segments_of(state)
    assert marked == []  # no marker until an agent submits one via submit_summary
    assert tail == descs


def test_idempotent_reingest():
    config = make_config()
    state = ""
    for i in range(8):
        state, _ = merge_once(state, [f"Unique fact {i} about the entity."], config)
    restate, reason = merge_once(state, ["Unique fact 7 about the entity."], config)
    assert restate == state


def test_legacy_marker_less_field_stays_readable():
    config = make_config()
    legacy_descs = [f"Legacy stored description {i} from an old graph." for i in range(8)]
    legacy = GRAPH_FIELD_SEP.join(legacy_descs)
    state, reason = merge_once(legacy, ["A brand new incoming description."], config)
    assert state == GRAPH_FIELD_SEP.join(legacy_descs + ["A brand new incoming description."])
    assert reason == PENDING_SUMMARY_REASON  # 9 raw descriptions > raw_tail_size


def test_giant_single_description_flagged_pending():
    config = make_config()
    giant = " ".join(f"word{i}" for i in range(CONTEXT_SIZE * 3))
    state, reason = merge_once("", [giant], config)
    assert state == giant  # never truncated by Preciso itself
    assert reason == PENDING_SUMMARY_REASON


@pytest.mark.parametrize("size", [1, 0])
def test_small_tail_sizes_still_flag_pending_over_count(size):
    config = make_config(raw_tail_size=size)
    state = ""
    for i in range(6):
        state, reason = merge_once(state, [f"Tail size {size} fact {i}."], config)
    assert reason == PENDING_SUMMARY_REASON


def test_existing_marker_segment_is_preserved_and_reread():
    """An existing SUMMARY_MARKER segment (written earlier by submit_summary) is
    correctly recognized as the prior_summary zone and doesn't get duplicated or
    dropped when new raw descriptions merge in."""
    config = make_config()
    state = GRAPH_FIELD_SEP.join([f"{SUMMARY_MARKER} an existing agent summary", "A verbatim fact."])
    state, reason = merge_once(state, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    assert marked == [f"{SUMMARY_MARKER} an existing agent summary"]
    assert tail == ["A verbatim fact.", "Another verbatim fact."]


def test_multiple_marker_segments_fold_into_one_on_read():
    config = make_config()
    corrupted = GRAPH_FIELD_SEP.join(
        [
            f"{SUMMARY_MARKER} first old summary",
            f"{SUMMARY_MARKER} second old summary",
            "A verbatim fact.",
        ]
    )
    state, reason = merge_once(corrupted, ["Another verbatim fact."], config)
    marked, tail = segments_of(state)
    assert len(marked) == 1
    assert "first old summary" in marked[0] and "second old summary" in marked[0]
    assert tail == ["A verbatim fact.", "Another verbatim fact."]


def test_empty_and_single_description_passthrough():
    config = make_config()
    state, reason = _handle_entity_relation_summary([], GRAPH_FIELD_SEP, config)
    assert state == "" and reason is None
    state, reason = _handle_entity_relation_summary(["Only fact."], GRAPH_FIELD_SEP, config)
    assert state == "Only fact." and reason is None
