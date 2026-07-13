"""Deterministic offline stubs shared by the pytest suite.

Mirrors the stubs used by test/summary_merge_manual.py and
test/marker_leak_manual.py so no Ollama/network is ever required.
"""

from __future__ import annotations

import json

from config import GRAPH_FIELD_SEP, SOURCE_IDS_LIMIT_METHOD_KEEP, SUMMARY_MARKER


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


class StubGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _key(src, tgt):
        return tuple(sorted((src, tgt)))

    async def get_node(self, name):
        return self.nodes.get(name)

    async def upsert_node(self, name, node_data):
        self.nodes[name] = dict(node_data)

    async def has_edge(self, src, tgt):
        return self._key(src, tgt) in self.edges

    async def get_edge(self, src, tgt):
        return self.edges.get(self._key(src, tgt))

    async def upsert_edge(self, src, tgt, edge_data):
        self.edges[self._key(src, tgt)] = dict(edge_data)


class StubVDB:
    """Captures every payload the merge path would embed/export."""

    def __init__(self):
        self.records: dict[str, dict] = {}

    async def upsert(self, payload):
        self.records.update({k: dict(v) for k, v in payload.items()})

    async def delete(self, ids):
        for record_id in ids:
            self.records.pop(record_id, None)


def make_merge_config(
    llm=None,
    *,
    raw_tail_size: int = 4,
    context: int = 200,
    summary_max: int = 50,
    source_ids_limit_method: str = SOURCE_IDS_LIMIT_METHOD_KEEP,
    max_source_ids: int = 100,
) -> dict:
    return {
        "llm_model_func": llm,
        "tokenizer": StubTokenizer(),
        "summary_context_size": context,
        "summary_max_tokens": summary_max,
        "summary_length_recommended": 30,
        "raw_tail_size": raw_tail_size,
        "force_llm_summary_on_merge": raw_tail_size,  # legacy alias
        "addon_params": {},
        "source_ids_limit_method": source_ids_limit_method,
        "max_source_ids_per_entity": max_source_ids,
        "max_source_ids_per_relation": max_source_ids,
        "max_file_paths": 10,
    }


def segments_of(description: str) -> tuple[list[str], list[str]]:
    """Split a stored description into (marked summary segments, verbatim tail)."""
    segs = description.split(GRAPH_FIELD_SEP) if description else []
    marked = [s for s in segs if s.lstrip().startswith(SUMMARY_MARKER)]
    tail = [s for s in segs if s and not s.lstrip().startswith(SUMMARY_MARKER)]
    return marked, tail


def contains_marker(value) -> bool:
    return SUMMARY_MARKER in json.dumps(value, ensure_ascii=False, default=str)
