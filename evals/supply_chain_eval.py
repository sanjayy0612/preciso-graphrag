"""Frozen, reproducible evaluation for Preciso's synthetic supply-chain corpus.

This module deliberately separates traversal correctness with curated extraction
from external-agent extraction evaluation and from evidence retrieval. It does
not invoke an LLM or manufacture an extraction result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.profiles import SUPPLY_CHAIN_WORKSPACE, validate_profile_records
from core.storage.base import EmbeddingFunc
from core.supply_chain import query_facility_unavailable
from core.utils import BasicTokenizer
from ingest.pipeline import ingest_extracted_json
from ingest.validator import validate_extraction_structure


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "fixtures" / "supply_chain"
PROTOCOL_PATH = ROOT / "evals" / "supply_chain_protocol.json"
SKILL_PATH = ROOT / "skills" / "Supply-Chain-Graph-Extraction" / "SKILL.md"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def raw_source_ids(record: dict[str, Any]) -> set[str]:
    return {source_id for source_id in str(record.get("source_id", "")).split("<SEP>") if source_id}


def relation_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("src_id") or record.get("source_entity") or ""),
        str(record.get("keywords", "")).split(",", 1)[0].strip().upper(),
        str(record.get("tgt_id") or record.get("target_entity") or ""),
    )


def score_sets(expected: set[Any], actual: set[Any]) -> dict[str, float | int]:
    correct = len(expected & actual)
    return {
        "expected": len(expected),
        "actual": len(actual),
        "correct": correct,
        "precision": correct / len(actual) if actual else (1.0 if not expected else 0.0),
        "recall": correct / len(expected) if expected else 1.0,
    }


def evaluation_metadata(protocol: dict[str, Any]) -> dict[str, Any]:
    source_files = sorted((FIXTURE_ROOT / "sources").glob("*.md"))
    source_hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_files
    }
    return {
        "protocol_version": protocol["protocol_version"],
        "protocol_sha256": hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest(),
        "source_sha256": source_hashes,
        "embedding": "config._fallback_embed (8-dimensional deterministic lexical test embedding)",
        "model": None,
        "run_count": 0,
    }


async def make_storage(workdir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = build_global_config(
        working_dir=str(workdir),
        tokenizer=BasicTokenizer(),
        embedding_func=EmbeddingFunc(
            embedding_dim=8,
            max_token_size=8192,
            func=_fallback_embed,
            model_name="fallback",
        ),
        extra={"vector_db_storage_cls_kwargs": {"cosine_better_than_threshold": 0.0}},
    )
    storage = build_storage_instances(config, workspace=SUPPLY_CHAIN_WORKSPACE)
    await initialize_storage_instances(storage)
    return storage, config


async def curated_stack(workdir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    storage, config = await make_storage(workdir)
    payload = load_json(FIXTURE_ROOT / "expected_extraction.json")
    result = await ingest_extracted_json(payload, storage, config)
    if result["status"] != "success":
        raise RuntimeError(f"curated extraction did not ingest: {result}")
    return storage, config


def paths_from_result(result: dict[str, Any]) -> set[tuple[str, ...]]:
    return {
        tuple(path["nodes"])
        for product in result.get("potentially_exposed_products", [])
        for path in product.get("paths", [])
    }


async def score_curated_traversal(protocol: dict[str, Any], workdir: Path) -> dict[str, Any]:
    storage, config = await curated_stack(workdir)
    question_results = []
    expected_paths_all: set[tuple[str, ...]] = set()
    actual_paths_all: set[tuple[str, ...]] = set()
    expected_products_all: set[str] = set()
    actual_products_all: set[str] = set()
    evidence_expected = 0
    evidence_correct = 0

    for question in protocol["dependency_questions"]:
        result = await query_facility_unavailable(question["facility_id"], storage, config)
        expected_paths = {tuple(path) for path in question["expected_paths"]}
        actual_paths = paths_from_result(result)
        expected_products = {path[-1] for path in expected_paths}
        actual_products = {item["product_id"] for item in result.get("potentially_exposed_products", [])}
        expected_paths_all |= expected_paths
        actual_paths_all |= actual_paths
        expected_products_all |= expected_products
        actual_products_all |= actual_products

        expected_evidence = set(question["required_chunks"])
        cited_evidence = {
            edge_evidence["source_id"].rsplit("::", 1)[-1]
            for product in result.get("potentially_exposed_products", [])
            for path in product.get("paths", [])
            for edge in path.get("edges", [])
            for edge_evidence in edge.get("evidence", [])
        }
        evidence_expected += len(expected_evidence)
        evidence_correct += len(expected_evidence & cited_evidence)
        question_results.append(
            {
                "id": question["id"],
                "status": result["status"],
                "path": score_sets(expected_paths, actual_paths),
                "products": score_sets(expected_products, actual_products),
                "edge_evidence": score_sets(expected_evidence, cited_evidence),
                "excluded_products_returned": sorted(set(question["excluded_products"]) & actual_products),
                "snapshot": result.get("snapshot"),
                "completeness": result.get("completeness"),
            }
        )

    negative_results = []
    for negative in protocol["negative_cases"]:
        if negative.get("remove_chunk"):
            case_storage, case_config = await curated_stack(workdir / negative["id"])
            document_id = load_json(FIXTURE_ROOT / "expected_extraction.json")["document_id"]
            await case_storage["text_chunks"].delete([f"{document_id}::{negative['remove_chunk']}"])
            await case_storage["text_chunks"].index_done_callback()
        else:
            case_storage, case_config = storage, config
        result = await query_facility_unavailable(negative["facility_id"], case_storage, case_config)
        negative_results.append(
            {
                "id": negative["id"],
                "expected_status": negative["expected_status"],
                "actual_status": result["status"],
                "passed": result["status"] == negative["expected_status"],
            }
        )

    northbridge = next(item for item in question_results if item["id"] == "northbridge")
    return {
        "label": "Traversal correctness given curated extraction (not end-to-end accuracy)",
        "path": score_sets(expected_paths_all, actual_paths_all),
        "products": score_sets(expected_products_all, actual_products_all),
        "edge_evidence": {
            "expected": evidence_expected,
            "correct": evidence_correct,
            "recall": evidence_correct / evidence_expected if evidence_expected else 1.0,
        },
        "questions": question_results,
        "negative_cases": negative_results,
        "northbridge_complete_result": northbridge,
    }


async def score_retrieval(protocol: dict[str, Any], workdir: Path) -> dict[str, Any]:
    storage, _config = await curated_stack(workdir)
    results = []
    expected_count = actual_count = correct_count = 0
    complete_paths = 0
    for question in protocol["dependency_questions"]:
        retrieved = await storage["chunks_vdb"].query(question["question"], top_k=protocol["retrieval"]["top_k"])
        actual = {item.get("chunk_id", "").rsplit("::", 1)[-1] for item in retrieved}
        expected = set(question["required_chunks"])
        expected_count += len(expected)
        actual_count += len(actual)
        correct_count += len(expected & actual)
        complete = expected <= actual
        complete_paths += int(complete)
        results.append(
            {
                "id": question["id"],
                "retrieved_chunk_ids": sorted(actual),
                "required_chunk_ids": sorted(expected),
                "evidence": score_sets(expected, actual),
                "complete_path_evidence_retrieved": complete,
            }
        )
    return {
        "label": "Chunk retrieval of evidence required for complete paths; not path construction or answer generation",
        "method": protocol["retrieval"],
        "aggregate_evidence": {
            "expected": expected_count,
            "actual": actual_count,
            "correct": correct_count,
            "precision": correct_count / actual_count if actual_count else 0.0,
            "recall": correct_count / expected_count if expected_count else 1.0,
        },
        "questions_with_all_path_evidence": f"{complete_paths}/{len(results)}",
        "questions": results,
    }


def validate_external_extraction(payload: dict[str, Any]) -> dict[str, Any]:
    errors = validate_extraction_structure(payload)
    chunk_ids = {str(chunk.get("chunk_id", "")) for chunk in payload.get("chunks", []) if isinstance(chunk, dict)}
    profile = build_global_config()["workspace_profiles"][SUPPLY_CHAIN_WORKSPACE]
    from core.profiles import PROFILES

    errors.extend(
        validate_profile_records(
            PROFILES[profile], payload.get("entities", []), payload.get("relationships", []), resolvable_source_ids=chunk_ids
        )
    )
    registry = load_json(FIXTURE_ROOT / "canonical_id_registry.json")
    allowed_ids = {item["canonical_id"] for item in registry["entities"]}
    unexpected_ids = sorted(
        str(entity.get("entity_name", ""))
        for entity in payload.get("entities", [])
        if isinstance(entity, dict) and entity.get("entity_name") not in allowed_ids
    )
    if unexpected_ids:
        errors.append(f"canonical-ID registry rejects: {', '.join(unexpected_ids)}")
    return {"accepted": not errors, "errors": errors}


def score_external_extraction(payload: dict[str, Any]) -> dict[str, Any]:
    ground_truth = load_json(FIXTURE_ROOT / "ground_truth.json")
    expected_entities = set(ground_truth["entities"])
    actual_entities = {
        str(entity.get("entity_name", "")) for entity in payload.get("entities", []) if isinstance(entity, dict)
    }
    expected_relations = {
        (item["src_id"], item["type"], item["tgt_id"]) for item in ground_truth["relationships"]
    }
    actual_relations = {relation_key(item) for item in payload.get("relationships", []) if isinstance(item, dict)}
    expected_evidence = {
        (item["src_id"], item["type"], item["tgt_id"]): item["evidence"]
        for item in ground_truth["relationships"]
    }
    supported = 0
    cited = 0
    for relation in payload.get("relationships", []):
        if not isinstance(relation, dict):
            continue
        key = relation_key(relation)
        if key not in expected_evidence:
            continue
        cited += 1
        if expected_evidence[key] in raw_source_ids(relation):
            supported += 1
    registry = load_json(FIXTURE_ROOT / "canonical_id_registry.json")
    allowed_ids = {item["canonical_id"] for item in registry["entities"]}
    return {
        "entity": score_sets(expected_entities, actual_entities),
        "typed_relation": score_sets(expected_relations, actual_relations),
        "canonical_id": {
            "correct": len(actual_entities & allowed_ids),
            "actual": len(actual_entities),
            "precision": len(actual_entities & allowed_ids) / len(actual_entities) if actual_entities else 0.0,
        },
        "relationship_evidence_support": {
            "supported": supported,
            "claims_matching_gold": cited,
            "precision": supported / cited if cited else 0.0,
            "definition": "For this synthetic corpus, a predicted gold relationship is supported only when it cites that relationship's independently authored gold chunk ID.",
        },
    }


def prepare_extractor_bundle(output_dir: Path) -> None:
    """Create the only allowed files for an external extraction run."""
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite extractor bundle: {output_dir}")
    (output_dir / "sources").mkdir(parents=True)
    for source in sorted((FIXTURE_ROOT / "sources").glob("*.md")):
        shutil.copy2(source, output_dir / "sources" / source.name)
    shutil.copy2(FIXTURE_ROOT / "canonical_id_registry.json", output_dir / "canonical_id_registry.json")
    shutil.copy2(SKILL_PATH, output_dir / "SKILL.md")
    write_json(
        output_dir / "run_manifest.json",
        {
            "instruction": "Use only sources/, canonical_id_registry.json, and SKILL.md. Write untouched model output to extraction_output.json.",
            "forbidden": ["expected_extraction.json", "ground_truth.json", "supply_chain_protocol.json", "evaluation results"],
            "model": "record externally by the runner",
            "run_count": 1,
        },
    )


async def run_curated(output_path: Path) -> dict[str, Any]:
    protocol = load_json(PROTOCOL_PATH)
    with tempfile.TemporaryDirectory(prefix="preciso-supply-chain-eval-") as temp_dir:
        workdir = Path(temp_dir)
        result = {
            "metadata": evaluation_metadata(protocol),
            "traversal_correctness_curated_extraction": await score_curated_traversal(protocol, workdir / "traversal"),
            "actual_extraction_end_to_end": {
                "status": "blocked",
                "reason": "No repository-owned agent/model execution mechanism is configured. Use prepare-extractor then score-extraction with untouched external output; do not substitute the curated extraction.",
                "model": None,
                "run_count": 0,
            },
            "retrieval_comparison": await score_retrieval(protocol, workdir / "retrieval"),
        }
    write_json(output_path, result)
    return result


async def run_northbridge_demo(output_path: Path) -> dict[str, Any]:
    """Run the exact cited Northbridge scenario against curated extraction."""
    with tempfile.TemporaryDirectory(prefix="preciso-supply-chain-demo-") as temp_dir:
        storage, config = await curated_stack(Path(temp_dir))
        result = await query_facility_unavailable(
            "facility:arkon-components:northbridge", storage, config
        )
    write_json(output_path, result)
    return result


async def evaluate_external_extraction(
    payload: dict[str, Any], model: str | None, settings: str | None, run_count: int
) -> dict[str, Any]:
    """Score untouched agent output only after strict validation and ingestion."""
    validation = validate_external_extraction(payload)
    result: dict[str, Any] = {
        "validation": validation,
        "extraction_scoring": score_external_extraction(payload),
        "model": model,
        "settings": settings,
        "run_count": run_count,
    }
    if not validation["accepted"]:
        result["end_to_end"] = {"status": "not_ingested", "reason": "strict validation rejected payload"}
        return result
    protocol = load_json(PROTOCOL_PATH)
    with tempfile.TemporaryDirectory(prefix="preciso-supply-chain-extraction-eval-") as temp_dir:
        storage, config = await make_storage(Path(temp_dir))
        ingestion = await ingest_extracted_json(payload, storage, config)
        result["ingestion"] = ingestion
        if ingestion["status"] not in {"success", "partial_success"}:
            result["end_to_end"] = {"status": "not_queryable", "reason": "ingestion did not succeed"}
            return result
        queries = []
        for question in protocol["dependency_questions"]:
            answer = await query_facility_unavailable(question["facility_id"], storage, config)
            expected_paths = {tuple(path) for path in question["expected_paths"]}
            actual_paths = paths_from_result(answer)
            queries.append(
                {
                    "id": question["id"],
                    "status": answer["status"],
                    "path": score_sets(expected_paths, actual_paths),
                    "products": score_sets(
                        {path[-1] for path in expected_paths},
                        {item["product_id"] for item in answer.get("potentially_exposed_products", [])},
                    ),
                }
            )
        result["end_to_end"] = {"status": "evaluated", "queries": queries}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run-curated", help="run frozen curated traversal and retrieval evaluations")
    run_parser.add_argument("--output", type=Path, required=True)
    prepare_parser = subparsers.add_parser("prepare-extractor", help="create a sealed external-agent input bundle")
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    demo_parser = subparsers.add_parser("demo-northbridge", help="run the cited Northbridge demonstration")
    demo_parser.add_argument("--output", type=Path, required=True)
    score_parser = subparsers.add_parser("score-extraction", help="validate and score untouched external-agent output")
    score_parser.add_argument("--input", type=Path, required=True)
    score_parser.add_argument("--output", type=Path, required=True)
    score_parser.add_argument("--model", default=None)
    score_parser.add_argument("--settings", default=None)
    score_parser.add_argument("--run-count", type=int, default=1)
    args = parser.parse_args()

    if args.command == "run-curated":
        result = asyncio.run(run_curated(args.output))
    elif args.command == "prepare-extractor":
        prepare_extractor_bundle(args.output_dir)
        result = {"status": "prepared", "output_dir": str(args.output_dir)}
    elif args.command == "demo-northbridge":
        result = asyncio.run(run_northbridge_demo(args.output))
    else:
        payload = load_json(args.input)
        result = asyncio.run(
            evaluate_external_extraction(payload, args.model, args.settings, args.run_count)
        )
        result["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
        write_json(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
