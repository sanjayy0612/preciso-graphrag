"""summary_mode="agent" — the engine never calls an LLM to summarize; instead
it defers (verbatim + a pending record) and exposes list_pending_summaries /
submit_summary for the MCP-driving agent to close the loop.

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
    global_config["summary_mode"] = "agent"
    global_config["raw_tail_size"] = RAW_TAIL_SIZE
    assert global_config["llm_model_func"] is None  # no LLM configured at all

    await ingest_n_docs(storage_instances, global_config)

    node = await storage_instances["graph"].get_node("ACME_CORP")
    assert SUMMARY_MARKER not in node["description"]  # stayed fully verbatim
    segments = node["description"].split(GRAPH_FIELD_SEP)
    assert len(segments) == N_DOCS  # nothing collapsed

    pending = storage_instances["pending_summaries"]
    record = await pending.get_by_id("ACME_CORP")
    assert record is not None
    assert record["kind"] == "entity"
    assert record["reason"] in ("merge_policy", "token_limit")

    edge_key = make_relation_chunk_key("ACME_CORP", "TIM_APPLE")
    edge_record = await pending.get_by_id(edge_key)
    assert edge_record is not None
    assert edge_record["kind"] == "relation"
    assert edge_record["src"] == "ACME_CORP" and edge_record["tgt"] == "TIM_APPLE"


async def test_list_pending_summaries_old_vs_keep_tail_split(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["summary_mode"] = "agent"
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)

    result = await list_pending_summaries(storage_instances, global_config, limit=50)
    assert result["status"] == "success"
    entity_item = next(item for item in result["items"] if item["name"] == "ACME_CORP")
    content = entity_item["content_to_summarize"]
    expected_all = [f"Fact {i} about ACME from a source document." for i in range(N_DOCS)]
    assert content["keep_tail"] == expected_all[-RAW_TAIL_SIZE:]
    assert content["old_descriptions"] == expected_all[:-RAW_TAIL_SIZE]
    assert content["prior_summary"] is None  # nothing summarized yet in agent mode


async def test_submit_summary_writes_marker_reembeds_and_clears_pending(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["summary_mode"] = "agent"
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    await ingest_n_docs(storage_instances, global_config)

    result = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="entity",
        summary_text="ACME is a company with several verified facts on record.",
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

    pending = storage_instances["pending_summaries"]
    assert await pending.get_by_id("ACME_CORP") is None


async def test_no_marker_leaks_from_list_and_submit_outputs(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["summary_mode"] = "agent"
    global_config["raw_tail_size"] = RAW_TAIL_SIZE
    await ingest_n_docs(storage_instances, global_config)

    # Submit once so a marker genuinely exists in storage, then re-list.
    await submit_summary(
        storage_instances, global_config, name="ACME_CORP", kind="entity", summary_text="A rolling summary."
    )
    list_result = await list_pending_summaries(storage_instances, global_config, limit=50)
    assert not contains_marker(list_result)

    submit_result = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP",
        kind="relation",
        summary_text="ignored",  # kind mismatch on purpose: exercises the error path output too
    )
    assert not contains_marker(submit_result)

    real_relation_submit = await submit_summary(
        storage_instances,
        global_config,
        name="ACME_CORP~TIM_APPLE",
        kind="relation",
        summary_text="ACME and Tim Apple have a long employment history.",
        src="ACME_CORP",
        tgt="TIM_APPLE",
    )
    assert not contains_marker(real_relation_submit)
    assert real_relation_submit["status"] == "success"


async def test_llm_and_verbatim_modes_unchanged_no_pending_queue(storage_stack):
    """Pre-existing behavior (no llm_model_func configured) is untouched by this
    feature: compression beyond raw_tail_size still surfaces "summary_required"
    (test_summary_merge.py::test_degraded_no_llm_keeps_verbatim_and_signals), and
    neither mode ever writes to the new pending_summaries queue."""
    storage_instances, global_config, _ = storage_stack
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    for mode in ("llm", "verbatim"):
        global_config["summary_mode"] = mode
        entity_name = f"WIDGET_CO_{mode}"
        result = None
        for i in range(N_DOCS):
            payload = {
                "document_id": f"doc_{mode}_{i}",
                "file_path": f"doc_{mode}_{i}.md",
                "timestamp": 1_700_000_000 + i,
                "chunks": [{"chunk_id": "chunk_001", "content": f"Unique content {mode} {i}."}],
                "entities": [
                    {
                        "entity_name": entity_name,
                        "entity_type": "ORG",
                        "description": f"Fact {i} about {entity_name}.",
                        "source_id": "chunk_001",
                    }
                ],
                "relationships": [],
            }
            result = await ingest_extracted_json(payload, storage_instances, global_config)
            assert result["status"] in ("success", "summary_required"), result

        # By N_DOCS with raw_tail_size=2 and no LLM, compression was needed and
        # deferred to the (unchanged) "summary_required" degraded signal.
        assert result["status"] == "summary_required"
        node = await storage_instances["graph"].get_node(entity_name)
        # No LLM configured in either mode -> both stay fully verbatim, no marker.
        assert SUMMARY_MARKER not in node["description"]
        pending = storage_instances["pending_summaries"]
        assert await pending.get_by_id(entity_name) is None


async def test_idempotent_reingest_no_duplicate_pending_and_noop_resubmit(storage_stack):
    storage_instances, global_config, _ = storage_stack
    global_config["summary_mode"] = "agent"
    global_config["raw_tail_size"] = RAW_TAIL_SIZE

    payload = make_payload(0)
    await ingest_extracted_json(payload, storage_instances, global_config)
    await ingest_extracted_json(copy.deepcopy(payload), storage_instances, global_config)  # identical re-ingest

    pending = storage_instances["pending_summaries"]
    # Below raw_tail_size after dedup -> no compression needed yet, nothing pending.
    assert await pending.get_by_id("ACME_CORP") is None

    await ingest_n_docs(storage_instances, global_config)  # now force a pending record
    assert await pending.get_by_id("ACME_CORP") is not None

    first = await submit_summary(
        storage_instances, global_config, name="ACME_CORP", kind="entity", summary_text="Stable summary text."
    )
    assert first["status"] == "success"
    node_after_first = dict(await storage_instances["graph"].get_node("ACME_CORP"))

    # Re-submitting once more (no new merges in between) is a no-op success:
    # same summary_text -> same resulting description.
    second = await submit_summary(
        storage_instances, global_config, name="ACME_CORP", kind="entity", summary_text="Stable summary text."
    )
    assert second["status"] == "success"
    node_after_second = dict(await storage_instances["graph"].get_node("ACME_CORP"))
    assert node_after_second["description"] == node_after_first["description"]
    assert await pending.get_by_id("ACME_CORP") is None
