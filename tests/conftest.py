from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from config import _fallback_embed, build_global_config
from core.bootstrap import build_storage_instances, initialize_storage_instances
from core.storage.base import EmbeddingFunc
from core.utils import BasicTokenizer


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
