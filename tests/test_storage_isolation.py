"""Storage globals must remain isolated between independent working directories."""

from __future__ import annotations

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer


async def _storage_stack(working_dir: str) -> dict:
    global_config = build_global_config(
        working_dir=working_dir,
        tokenizer=BasicTokenizer(),
        embedding_func=EmbeddingFunc(
            embedding_dim=8,
            max_token_size=8192,
            func=_fallback_embed,
            model_name="fallback",
        ),
    )
    storage_instances = build_storage_instances(global_config)
    await initialize_storage_instances(storage_instances)
    return storage_instances


async def test_json_kv_storage_isolated_by_working_dir(tmp_path):
    left = await _storage_stack(str(tmp_path / "left"))
    await left["text_chunks"].upsert({"same-key": {"content": "left-only"}})

    right = await _storage_stack(str(tmp_path / "right"))

    assert await right["text_chunks"].get_by_id("same-key") is None
