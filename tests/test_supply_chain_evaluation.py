"""Stage-4 evaluation protocol and reproducible demonstration checks."""

from __future__ import annotations

import json

import pytest

from evals.supply_chain_eval import (
    FIXTURE_ROOT,
    PROTOCOL_PATH,
    load_json,
    prepare_extractor_bundle,
    run_curated,
)


def test_protocol_is_frozen_and_keeps_gold_out_of_extractor_inputs(tmp_path):
    protocol = load_json(PROTOCOL_PATH)
    assert protocol["protocol_version"] == "1.0"
    assert [item["id"] for item in protocol["dependency_questions"]] == [
        "northbridge",
        "lakeside",
        "harbor",
    ]

    bundle = tmp_path / "extractor-input"
    prepare_extractor_bundle(bundle)
    assert sorted(path.name for path in (bundle / "sources").glob("*.md")) == [
        "facility_register.md",
        "identity_notice.md",
        "product_bom.md",
    ]
    assert not (bundle / "expected_extraction.json").exists()
    assert not (bundle / "ground_truth.json").exists()
    assert not (bundle / "supply_chain_protocol.json").exists()


@pytest.mark.asyncio
async def test_curated_evaluation_confirms_complete_northbridge_demo(tmp_path):
    output = tmp_path / "results.json"
    result = await run_curated(output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    northbridge = result["traversal_correctness_curated_extraction"]["northbridge_complete_result"]

    assert saved == result
    assert northbridge["status"] == "success"
    assert northbridge["path"] == {
        "expected": 2,
        "actual": 2,
        "correct": 2,
        "precision": 1.0,
        "recall": 1.0,
    }
    assert northbridge["products"]["recall"] == 1.0
    assert northbridge["snapshot"]["effective_dates"] == ["2026-01-15"]
    assert northbridge["completeness"] == {"is_truncated": False, "max_paths": 100}
    assert result["actual_extraction_end_to_end"]["status"] == "blocked"
    assert result["retrieval_comparison"]["questions_with_all_path_evidence"] == "1/3"


def test_external_validation_uses_the_registry_and_strict_profile():
    payload = load_json(FIXTURE_ROOT / "invalid" / "unsupported_dependency.json")
    from evals.supply_chain_eval import validate_external_extraction

    validation = validate_external_extraction(payload)
    assert not validation["accepted"]
    assert any("requires `USED_IN` to connect COMPONENT -> PRODUCT" in error for error in validation["errors"])
