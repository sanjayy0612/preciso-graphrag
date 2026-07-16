"""Agent-driven summarization — Preciso never calls an LLM to summarize;
instead it defers (verbatim + a pending record) and exposes
list_pending_summaries / submit_summary for the MCP-driving agent to close the
loop.

Runs against the real storage stack (storage_stack fixture: NetworkX + JSON KV
+ the 8-dim fallback embedder) so ingest -> defer -> list -> submit is
exercised end-to-end, offline and deterministic."""

from __future__ import annotations

import copy

from config import GRAPH_FIELD_SEP, SUMMARY_MARKER
from core.utils import make_relation_chunk_key
from ingest.pipeline import ingest_extracted_json
from preciso_mcp.tools.pending_summaries_tool import list_pending_summaries, submit_summary
from tests._stubs import contains_marker

RAW_TAIL_SIZE = 2
N_DOCS = 6


def make_payload(i: int, description: str | None = None) -> dict:
    return {
        "document_id": f"doc_{i}",
        "file_path": f"doc_{i}.md",
        "timestamp": 1_700_000_000 + i,
        "chunks": [{"chunk_id": "chunk_001", "content": f"Unique source content number {i} about ACME."}],
        "entities": [
            {
                "entity_name": "ACME_CORP",
                "entity_type": "ORG",
                "description": description or f"Fact {i} about ACME from a source document.",
                "source_id": "chunk_001",
            },
            {
                "entity_name": "TIM_APPLE",
                "entity_type": "PERSON",
                "description": f"Tim Apple mention {i}.",
                "source_id": "chunk_001",
            },
        ],
        "relationships": [
            {
                "src_id": "ACME_CORP",
                "tgt_id": "TIM_APPLE",
                "description": f"Relation fact {i} between ACME and Tim Apple.",
                "keywords": "employment",
                "weight": 1.0,
                "source_id": "chunk_001",
            }
        ],
    }


async def ingest_n_docs(storage_instances, global_config, n=N_DOCS):
    for i in range(n):
        result = await ingest_extracted_json(make_payload(i), storage_instances, global_config)
        assert result["status"] == "success", result
    return result


async def test_agent_mode_defers_and_writes_pending_record(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)

    node = await storage_instances["graph"].get_node("ACME_CORP")
    assert SUMMARY_MARKER not in node["description"]  # stayed fully verbatim
    segments = node["description"].split(GRAPH_FIELD_SEP)
    assert len(segments) == N_DOCS  # nothing collapsed

    pending = storage_instances["pending_summaries"]
    record = await pending.get_by_id("ACME_CORP")
    assert record is not None
    assert record["kind"] == "entity"
    assert record["description_count"] == N_DOCS

    edge_key = make_relation_chunk_key("ACME_CORP", "TIM_APPLE")
    edge_record = await pending.get_by_id(edge_key)
    assert edge_record is not None
    assert edge_record["kind"] == "relation"
    assert edge_record["src"] == "ACME_CORP" and edge_record["tgt"] == "TIM_APPLE"


async def test_list_pending_summaries_old_vs_keep_tail_split(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)

    result = await list_pending_summaries(storage_instances, global_config, limit=50)
    assert result["status"] == "success"
    entity_item = next(item for item in result["items"] if item["name"] == "ACME_CORP")
    assert entity_item["description_count"] == N_DOCS
    content = entity_item["content_to_summarize"]
    expected_all = [f"Fact {i} about ACME from a source document." for i in range(N_DOCS)]
    assert content["keep_tail"] == expected_all[-RAW_TAIL_SIZE:]
    assert content["old_descriptions"] == expected_all[:-RAW_TAIL_SIZE]
    assert content["prior_summary"] is None  # nothing summarized yet


async def test_submit_summary_writes_marker_reembeds_and_clears_pending(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)

    pending = storage_instances["pending_summaries"]
    record = await pending.get_by_id("ACME_CORP")

    result = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="ACME is a company with several verified facts on record.",
        expected_description_count=record["description_count"],
    )
    assert result["status"] == "success", result

    node = await storage_instances["graph"].get_node("ACME_CORP")
    marked_segments = [s for s in node["description"].split(GRAPH_FIELD_SEP) if s.startswith(SUMMARY_MARKER)]
    assert len(marked_segments) == 1
    assert "ACME is a company with several verified facts on record." in marked_segments[0]
    tail_segments = [s for s in node["description"].split(GRAPH_FIELD_SEP) if not s.startswith(SUMMARY_MARKER)]
    expected_tail = [f"Fact {i} about ACME from a source document." for i in range(N_DOCS)][-RAW_TAIL_SIZE:]
    assert tail_segments == expected_tail

    # re-embedded, marker stripped (storage itself legitimately keeps the marker —
    # only the outward-facing vdb content must be clean)
    entities_vdb = storage_instances["entities_vdb"]
    from core.utils import compute_mdhash_id

    vdb_id = compute_mdhash_id("ACME_CORP", prefix="ent-")
    stored = await entities_vdb.get_by_id(vdb_id)
    assert stored is not None
    assert SUMMARY_MARKER not in stored["content"]

    assert await pending.get_by_id("ACME_CORP") is None


async def test_submit_summary_rejects_stale_expected_description_count(storage_stack):
    """A concurrent merge landing between list_pending_summaries and submit_summary
    must not silently drop the new content — submit_summary rejects the stale
    submission instead."""
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)
    pending = storage_instances["pending_summaries"]
    stale_record = await pending.get_by_id("ACME_CORP")
    stale_count = stale_record["description_count"]

    # A new merge lands after the agent "read" the pending content.
    await ingest_extracted_json(make_payload(N_DOCS), storage_instances, global_config)

    result = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="A summary based on stale content.",
        expected_description_count=stale_count,
    )
    assert result["status"] == "error"

    # Nothing was dropped: the field is still fully verbatim with all N_DOCS+1
    # descriptions, and the pending record is still live (untouched).
    node = await storage_instances["graph"].get_node("ACME_CORP")
    assert SUMMARY_MARKER not in node["description"]
    assert len(node["description"].split(GRAPH_FIELD_SEP)) == N_DOCS + 1
    assert await pending.get_by_id("ACME_CORP") is not None


async def test_no_marker_leaks_from_list_and_submit_outputs(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE
    await ingest_n_docs(storage_instances, global_config)

    pending = storage_instances["pending_summaries"]

    # Submit once so a marker genuinely exists in storage, then re-list.
    entity_record = await pending.get_by_id("ACME_CORP")
    await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="A rolling summary.",
        expected_description_count=entity_record["description_count"],
    )
    list_result = await list_pending_summaries(storage_instances, global_config, limit=50)
    assert not contains_marker(list_result)

    submit_result = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="relation",
        summary_text="ignored",  # kind mismatch on purpose: exercises the error path output too
        expected_description_count=0,
    )
    assert not contains_marker(submit_result)

    edge_key = make_relation_chunk_key("ACME_CORP", "TIM_APPLE")
    edge_record = await pending.get_by_id(edge_key)
    real_relation_submit = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP~TIM_APPLE",
        kind="relation",
        summary_text="ACME and Tim Apple have a long employment history.",
        expected_description_count=edge_record["description_count"],
        src="ACME_CORP",
        tgt="TIM_APPLE",
    )
    assert not contains_marker(real_relation_submit)
    assert real_relation_submit["status"] == "success"


async def test_idempotent_reingest_no_duplicate_pending_and_noop_resubmit(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    payload = make_payload(0)
    await ingest_extracted_json(payload, storage_instances, global_config)
    await ingest_extracted_json(copy.deepcopy(payload), storage_instances, global_config)  # identical re-ingest

    pending = storage_instances["pending_summaries"]
    # Below raw_tail_size after dedup -> no compression needed yet, nothing pending.
    assert await pending.get_by_id("ACME_CORP") is None

    await ingest_n_docs(storage_instances, global_config)  # now force a pending record
    record = await pending.get_by_id("ACME_CORP")
    assert record is not None

    first = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="Stable summary text.",
        expected_description_count=record["description_count"],
    )
    assert first["status"] == "success"
    node_after_first = dict(await storage_instances["graph"].get_node("ACME_CORP"))

    # Re-submitting once more (no new merges in between) is a no-op success:
    # same summary_text -> same resulting description. The pending record is
    # already cleared, so the expected count no longer matters for the guard,
    # but the resulting tail is still whatever it was after the first submit.
    tail_count = len(
        [
            s
            for s in node_after_first["description"].split(GRAPH_FIELD_SEP)
            if not s.startswith(SUMMARY_MARKER)
        ]
    )
    second = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="Stable summary text.",
        expected_description_count=1 + tail_count,  # 1 marker segment + tail
    )
    assert second["status"] == "success"
    node_after_second = dict(await storage_instances["graph"].get_node("ACME_CORP"))
    assert node_after_second["description"] == node_after_first["description"]
    assert await pending.get_by_id("ACME_CORP") is None
