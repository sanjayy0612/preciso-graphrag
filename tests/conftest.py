from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.storage import shared_storage
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer


@pytest.fixture(autouse=True)
def isolate_shared_storage():
    """Give every test a genuinely empty storage namespace.

    core/storage/shared_storage.py holds JsonKVStorage data in process-global dicts
    keyed by namespace (plus workspace) but *not* by working_dir, and
    try_initialize_namespace() only loads from disk the first time a namespace is seen
    in a process. Two tests using different tmp_path dirs therefore share the same
    in-memory KV data, which leaks through core/merge.py (it reads prior chunk_ids from
    entity_chunks_storage) into the next test's merged source_id values.
    """
    for state in (
        shared_storage._namespace_data,
        shared_storage._namespace_locks,
        shared_storage._namespace_update_flags,
        shared_storage._namespace_init_flags,
        shared_storage._keyed_locks,
    ):
        state.clear()
    yield


@pytest.fixture
async def storage_stack(tmp_path):
    """Full offline storage stack in a temporary working dir.

    Uses the 8-dim fallback embedder — no Ollama/network involved.
    Returns (storage_instances, global_config, working_dir).
    """
    global_config = build_global_config(
        working_dir=str(tmp_path),
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
    return storage_instances, global_config, tmp_path
