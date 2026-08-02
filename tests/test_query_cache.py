"""Query-cache keys must represent every input sent to the response model."""

from __future__ import annotations

import pytest

import core.query as query_module
from core.storage.base import QueryContextResult, QueryParam


class MemoryCache:
    def __init__(self):
        self.global_config = {"enable_llm_cache": True}
        self.data = {}

    async def get_by_id(self, key):
        return self.data.get(key)

    async def upsert(self, data):
        self.data.update(data)


@pytest.mark.asyncio
async def test_query_cache_varies_with_rendered_prompt_and_history(monkeypatch):
    async def fake_context(*_args, **_kwargs):
        return QueryContextResult(context="graph context", raw_data={})

    calls = []

    async def model(query, **kwargs):
        calls.append((query, kwargs["system_prompt"], kwargs["history_messages"]))
        return f"response-{len(calls)}"

    monkeypatch.setattr(query_module, "_build_query_context", fake_context)
    cache = MemoryCache()
    config = {"enable_llm_cache": True}

    async def run(system_prompt, history):
        return await query_module.kg_query(
            query="What changed?",
            knowledge_graph_inst=None,
            entities_vdb=None,
            relationships_vdb=None,
            text_chunks_db=None,
            chunks_vdb=None,
            query_param=QueryParam(
                mode="local",
                ll_keywords=["changed"],
                conversation_history=history,
                model_func=model,
            ),
            global_config=config,
            hashing_kv=cache,
            system_prompt=system_prompt,
        )

    await run("Answer precisely using: {context_data}", [])
    await run("Answer cautiously using: {context_data}", [])
    await run(
        "Answer cautiously using: {context_data}",
        [{"role": "user", "content": "Earlier question"}],
    )

    assert len(calls) == 3
