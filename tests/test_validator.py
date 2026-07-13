"""ingest/validator.py — pure-function contracts the pipeline relies on:
every entity carries name/description/source_id/type, and every relationship's
endpoints reference defined entities."""

from __future__ import annotations

import pytest

from ingest.validator import validate_entity, validate_relationship

VALID_ENTITY = {
    "entity_name": "APPLE",
    "entity_type": "ORG",
    "description": "Apple is a company.",
    "source_id": "chunk-1",
}

KNOWN = {"APPLE", "TIM_COOK"}

VALID_REL = {
    "src_id": "TIM_COOK",
    "tgt_id": "APPLE",
    "description": "Tim Cook leads Apple.",
    "source_id": "chunk-1",
    "weight": 1.0,
}


def test_valid_entity_passes():
    ok, reason = validate_entity(VALID_ENTITY)
    assert ok and reason == ""


def test_entity_must_be_dict():
    ok, reason = validate_entity(["not", "a", "dict"])
    assert not ok and "object" in reason


@pytest.mark.parametrize("missing", ["entity_name", "description", "source_id", "entity_type"])
def test_entity_required_fields(missing):
    entity = {k: v for k, v in VALID_ENTITY.items() if k != missing}
    ok, reason = validate_entity(entity)
    assert not ok
    assert missing in reason or "entity_name" in reason


def test_entity_whitespace_fields_rejected():
    entity = {**VALID_ENTITY, "source_id": "   "}
    ok, _ = validate_entity(entity)
    assert not ok


def test_valid_relationship_passes():
    ok, reason = validate_relationship(VALID_REL, KNOWN)
    assert ok and reason == ""


def test_relationship_accepts_source_target_entity_aliases():
    rel = {
        "source_entity": "TIM_COOK",
        "target_entity": "APPLE",
        "description": "Tim Cook leads Apple.",
        "source_id": "chunk-1",
    }
    ok, reason = validate_relationship(rel, KNOWN)
    assert ok and reason == ""


def test_relationship_requires_both_endpoints():
    ok, reason = validate_relationship({**VALID_REL, "tgt_id": ""}, KNOWN)
    assert not ok and "requires src_id" in reason


def test_relationship_rejects_self_loop():
    ok, reason = validate_relationship({**VALID_REL, "tgt_id": "TIM_COOK"}, KNOWN)
    assert not ok and "self-loop" in reason


def test_relationship_endpoints_must_be_known_entities():
    ok, reason = validate_relationship({**VALID_REL, "src_id": "GHOST"}, KNOWN)
    assert not ok and "unknown entities" in reason


def test_relationship_requires_description_and_source_id():
    ok, _ = validate_relationship({**VALID_REL, "description": ""}, KNOWN)
    assert not ok
    ok, _ = validate_relationship({**VALID_REL, "source_id": ""}, KNOWN)
    assert not ok


def test_relationship_weight_must_be_numeric():
    ok, reason = validate_relationship({**VALID_REL, "weight": "heavy"}, KNOWN)
    assert not ok and "weight must be numeric" in reason
    # numeric strings are fine
    ok, _ = validate_relationship({**VALID_REL, "weight": "2.5"}, KNOWN)
    assert ok
