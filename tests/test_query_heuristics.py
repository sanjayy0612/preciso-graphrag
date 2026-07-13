"""Domain heuristics moved from core/query.py into config.py (Tier 4):
comparison-query detection reads its phrase list from global_config, and the
rag_response persona is configurable with a domain-neutral default."""

from __future__ import annotations

from config import DEFAULT_YOY_SIGNAL_PHRASES, PROMPTS, build_global_config
from core.query import _detect_comparison_query


def test_default_phrases_detect_comparison_queries():
    phrases = DEFAULT_YOY_SIGNAL_PHRASES
    assert _detect_comparison_query("How did revenue change from 2022 to 2023?", phrases)
    assert _detect_comparison_query("Walmart YoY sales growth", phrases)
    assert _detect_comparison_query("Apple vs Microsoft market cap", phrases)
    assert not _detect_comparison_query("What is Tim Cook's role?", phrases)


def test_custom_phrase_list_is_respected():
    assert _detect_comparison_query("delta between quarters", ["delta between"])
    assert not _detect_comparison_query("How did revenue change from 2022?", ["delta between"])


def test_yoy_phrases_flow_through_global_config():
    cfg = build_global_config(working_dir="/tmp/x")
    assert cfg["yoy_signal_phrases"] == DEFAULT_YOY_SIGNAL_PHRASES


def test_rag_response_default_persona_is_domain_neutral():
    first_line = PROMPTS["rag_response"].splitlines()[0]
    assert first_line == "You are a knowledge graph retrieval assistant."
    # template placeholders still intact for kg_query's .format()
    assert "{response_type}" in PROMPTS["rag_response"]
    assert "{context_data}" in PROMPTS["rag_response"]
