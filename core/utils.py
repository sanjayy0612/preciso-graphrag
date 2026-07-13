from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import math
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass
from hashlib import md5
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from config import (
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_SOURCE_IDS_LIMIT_METHOD,
    GRAPH_FIELD_SEP,
    SOURCE_IDS_LIMIT_METHOD_FIFO,
    SUMMARY_MARKER,
    VALID_SOURCE_IDS_LIMIT_METHODS,
)

logger = logging.getLogger("graphrag_mcp")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

_SUMMARY_MARKER_RE = re.compile(re.escape(SUMMARY_MARKER) + r"\s*")


def strip_summary_marker(text: str) -> str:
    """Remove the internal SUMMARY_MARKER bookkeeping prefix.

    The marker is storage-only metadata; it must never reach embedding content,
    LLM query context, user-facing query results, or exports.
    """
    if not text or SUMMARY_MARKER not in text:
        return text
    return _SUMMARY_MARKER_RE.sub("", text)


class BasicTokenizer:
    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name
        self._encoding = None
        if os.getenv("GRAPHRAG_DISABLE_TIKTOKEN", "0") == "1":
            return
        try:
            import tiktoken

            self._encoding = tiktoken.encoding_for_model(model_name)
        except Exception:
            self._encoding = None

    def encode(self, text: str) -> list[int]:
        text = text or ""
        if self._encoding is not None:
            return self._encoding.encode(text)
        return text.encode("utf-8", errors="ignore").split()

    def decode(self, tokens: Sequence[int]) -> str:
        if self._encoding is not None:
            return self._encoding.decode(list(tokens))
        if isinstance(tokens, bytes):
            return tokens.decode("utf-8", errors="ignore")
        return ""


def load_json(file_name: str) -> dict[str, Any] | None:
    path = Path(file_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to load json %s: %s", file_name, exc)
        return None


def write_json(data: dict[str, Any], file_name: str) -> bool:
    path = Path(file_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def compute_args_hash(*args: Any) -> str:
    args_str = "".join(str(arg) for arg in args)
    try:
        return md5(args_str.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return md5(args_str.encode("utf-8", errors="replace")).hexdigest()


def compute_mdhash_id(content: str, prefix: str = "") -> str:
    return prefix + compute_args_hash(content)


def generate_cache_key(mode: str, cache_type: str, hash_value: str) -> str:
    return f"{mode}:{cache_type}:{hash_value}"


def split_string_by_multi_markers(content: str, markers: list[str]) -> list[str]:
    if not markers:
        return [content]
    content = content if content is not None else ""
    results = re.split("|".join(re.escape(marker) for marker in markers), content)
    return [item.strip() for item in results if item.strip()]


def truncate_list_by_token_size(
    list_data: list[Any],
    key: Callable[[Any], str],
    max_token_size: int,
    tokenizer: BasicTokenizer,
) -> list[Any]:
    if max_token_size <= 0:
        return []
    tokens = 0
    for i, data in enumerate(list_data):
        tokens += len(tokenizer.encode(key(data)))
        if tokens > max_token_size:
            return list_data[:i]
    return list_data


async def safe_vdb_operation_with_exception(
    operation: Callable,
    operation_name: str,
    entity_name: str = "",
    max_retries: int = 3,
    retry_delay: float = 0.2,
    logger_func: Optional[Callable] = None,
) -> None:
    log_func = logger_func or logger.warning
    for attempt in range(max_retries):
        try:
            await operation()
            return
        except Exception as exc:
            if attempt >= max_retries - 1:
                error_msg = (
                    f"VDB {operation_name} failed for {entity_name} "
                    f"after {max_retries} attempts: {exc}"
                )
                log_func(error_msg)
                raise Exception(error_msg) from exc
            log_func(
                f"VDB {operation_name} attempt {attempt + 1} failed for {entity_name}: {exc}, retrying..."
            )
            if retry_delay > 0:
                await asyncio.sleep(retry_delay)


async def handle_cache(
    hashing_kv,
    args_hash,
    prompt,
    mode="default",
    cache_type="unknown",
) -> tuple[str, int] | None:
    if hashing_kv is None:
        return None
    if mode != "default":
        if not hashing_kv.global_config.get("enable_llm_cache"):
            return None
    else:
        if not hashing_kv.global_config.get("enable_llm_cache_for_entity_extract"):
            return None
    flattened_key = generate_cache_key(mode, cache_type, args_hash)
    cache_entry = await hashing_kv.get_by_id(flattened_key)
    if cache_entry:
        return cache_entry["return"], cache_entry.get("create_time", 0)
    return None


@dataclass
class CacheData:
    args_hash: str
    content: str
    prompt: str
    mode: str = "default"
    cache_type: str = "query"
    chunk_id: str | None = None
    queryparam: dict | None = None


async def save_to_cache(hashing_kv, cache_data: CacheData):
    if hashing_kv is None or not cache_data.content:
        return
    if hasattr(cache_data.content, "__aiter__"):
        return
    flattened_key = generate_cache_key(
        cache_data.mode, cache_data.cache_type, cache_data.args_hash
    )
    cache_entry = {
        "return": cache_data.content,
        "cache_type": cache_data.cache_type,
        "chunk_id": cache_data.chunk_id,
        "original_prompt": cache_data.prompt,
        "queryparam": cache_data.queryparam,
    }
    await hashing_kv.upsert({flattened_key: cache_entry})


def sanitize_text_for_encoding(text: str, replacement_char: str = "") -> str:
    if not text:
        return text
    text = html.unescape(text.strip())
    text = re.sub(r"[\ud800-\udfff]", replacement_char, text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", replacement_char, text)
    return text.strip()


def remove_think_tags(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


async def use_llm_func_with_cache(
    user_prompt: str,
    use_llm_func: callable,
    llm_response_cache=None,
    system_prompt: str | None = None,
    max_tokens: int = None,
    history_messages: list[dict[str, str]] | None = None,
    cache_type: str = "extract",
    chunk_id: str | None = None,
    cache_keys_collector: list | None = None,
) -> tuple[str, int]:
    safe_user_prompt = sanitize_text_for_encoding(user_prompt)
    safe_system_prompt = (
        sanitize_text_for_encoding(system_prompt) if system_prompt else None
    )
    safe_history = None
    if history_messages:
        safe_history = []
        for msg in history_messages:
            copied = dict(msg)
            if "content" in copied:
                copied["content"] = sanitize_text_for_encoding(copied["content"])
            safe_history.append(copied)
    history = json.dumps(safe_history, ensure_ascii=False) if safe_history else None
    if llm_response_cache:
        prompt_parts = [part for part in [safe_user_prompt, safe_system_prompt, history] if part]
        full_prompt = "\n".join(prompt_parts)
        arg_hash = compute_args_hash(full_prompt)
        cache_key = generate_cache_key("default", cache_type, arg_hash)
        cached_result = await handle_cache(
            llm_response_cache, arg_hash, full_prompt, "default", cache_type
        )
        if cached_result:
            if cache_keys_collector is not None:
                cache_keys_collector.append(cache_key)
            return cached_result
        kwargs: dict[str, Any] = {}
        if safe_history:
            kwargs["history_messages"] = safe_history
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = await use_llm_func(
            safe_user_prompt, system_prompt=safe_system_prompt, **kwargs
        )
        response = remove_think_tags(response)
        timestamp = int(time.time())
        await save_to_cache(
            llm_response_cache,
            CacheData(
                args_hash=arg_hash,
                content=response,
                prompt=full_prompt,
                cache_type=cache_type,
                chunk_id=chunk_id,
            ),
        )
        if cache_keys_collector is not None:
            cache_keys_collector.append(cache_key)
        return response, timestamp
    kwargs = {}
    if safe_history:
        kwargs["history_messages"] = safe_history
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    response = await use_llm_func(
        safe_user_prompt, system_prompt=safe_system_prompt, **kwargs
    )
    return remove_think_tags(response), int(time.time())


def pick_by_weighted_polling(
    entities_or_relations: list[dict],
    max_related_chunks: int,
    min_related_chunks: int = 1,
) -> list[str]:
    if not entities_or_relations:
        return []
    n = len(entities_or_relations)
    if n == 1:
        return entities_or_relations[0].get("sorted_chunks", [])[:max_related_chunks]
    expected_counts = []
    for i in range(n):
        ratio = i / (n - 1) if n > 1 else 0
        expected = max_related_chunks - ratio * (
            max_related_chunks - min_related_chunks
        )
        expected_counts.append(int(round(expected)))
    selected_chunks: list[str] = []
    used_counts: list[int] = []
    remaining = 0
    for i, item in enumerate(entities_or_relations):
        chunks = item.get("sorted_chunks", [])
        actual = min(expected_counts[i], len(chunks))
        selected_chunks.extend(chunks[:actual])
        used_counts.append(actual)
        rem = expected_counts[i] - actual
        if rem > 0:
            remaining += rem
    for _ in range(remaining):
        allocated = False
        for i, item in enumerate(entities_or_relations):
            chunks = item.get("sorted_chunks", [])
            if used_counts[i] < len(chunks):
                selected_chunks.append(chunks[used_counts[i]])
                used_counts[i] += 1
                allocated = True
                break
        if not allocated:
            break
    return selected_chunks


def cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    v1_arr = np.array(v1)
    v2_arr = np.array(v2)
    denom = np.linalg.norm(v1_arr) * np.linalg.norm(v2_arr)
    if denom == 0:
        return 0.0
    return float(np.dot(v1_arr, v2_arr) / denom)


async def pick_by_vector_similarity(
    query: str,
    text_chunks_storage,
    chunks_vdb,
    num_of_chunks: int,
    entity_info: list[dict[str, Any]],
    embedding_func: callable,
    query_embedding=None,
) -> list[str]:
    chunk_ids: list[str] = []
    for item in entity_info:
        chunk_ids.extend(item.get("sorted_chunks", item.get("chunks", [])))
    chunk_ids = list(dict.fromkeys(chunk_ids))
    if not chunk_ids:
        return []
    chunk_vectors = await chunks_vdb.get_vectors_by_ids(chunk_ids)
    if query_embedding is None:
        query_embedding = (await embedding_func([query], context="query", _priority=5))[0]
    scored: list[tuple[str, float]] = []
    for chunk_id in chunk_ids:
        vector = chunk_vectors.get(chunk_id)
        if vector is None:
            continue
        scored.append((chunk_id, cosine_similarity(query_embedding, vector)))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [chunk_id for chunk_id, _ in scored[: max(0, num_of_chunks)]]


async def apply_rerank_if_enabled(
    query: str,
    retrieved_docs: list[dict[str, Any]],
    global_config: dict[str, Any],
    enable_rerank: bool,
    top_n: int,
) -> list[dict[str, Any]]:
    rerank_func = global_config.get("rerank_model_func")
    if not enable_rerank or not rerank_func:
        return retrieved_docs
    try:
        reranked = await rerank_func(query=query, documents=retrieved_docs, top_n=top_n)
        return reranked or retrieved_docs
    except Exception as exc:
        logger.error("Rerank failed: %s", exc)
        return retrieved_docs


async def process_chunks_unified(
    query: str,
    unique_chunks: list[dict],
    query_param,
    global_config: dict,
    source_type: str = "mixed",
    chunk_token_limit: int = None,
) -> list[dict]:
    if not unique_chunks:
        return []
    origin_count = len(unique_chunks)
    if query_param.enable_rerank and query and unique_chunks:
        rerank_top_k = query_param.chunk_top_k or len(unique_chunks)
        unique_chunks = await apply_rerank_if_enabled(
            query=query,
            retrieved_docs=unique_chunks,
            global_config=global_config,
            enable_rerank=query_param.enable_rerank,
            top_n=rerank_top_k,
        )
    if query_param.enable_rerank and unique_chunks:
        min_rerank_score = global_config.get("min_rerank_score", 0.5)
        unique_chunks = [
            chunk
            for chunk in unique_chunks
            if chunk.get("rerank_score", 1.0) >= min_rerank_score
        ]
        if not unique_chunks:
            return []
    if query_param.chunk_top_k is not None and query_param.chunk_top_k > 0:
        unique_chunks = unique_chunks[: query_param.chunk_top_k]
    tokenizer = global_config.get("tokenizer")
    if tokenizer and unique_chunks:
        if chunk_token_limit is None:
            chunk_token_limit = getattr(
                query_param,
                "max_total_tokens",
                global_config.get("MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS),
            )
        unique_chunks = truncate_list_by_token_size(
            unique_chunks,
            key=lambda x: "\n".join(
                json.dumps(item, ensure_ascii=False) for item in [x]
            ),
            max_token_size=chunk_token_limit,
            tokenizer=tokenizer,
        )
        logger.debug(
            "Token truncation: %s chunks from %s (limit=%s source=%s)",
            len(unique_chunks),
            origin_count,
            chunk_token_limit,
            source_type,
        )
    final_chunks = []
    for i, chunk in enumerate(unique_chunks):
        copied = chunk.copy()
        copied["id"] = f"DC{i + 1}"
        final_chunks.append(copied)
    return final_chunks


def normalize_source_ids_limit_method(method: str | None) -> str:
    if not method:
        return DEFAULT_SOURCE_IDS_LIMIT_METHOD
    normalized = method.upper()
    if normalized not in VALID_SOURCE_IDS_LIMIT_METHODS:
        return DEFAULT_SOURCE_IDS_LIMIT_METHOD
    return normalized


def merge_source_ids(
    existing_ids: Iterable[str] | None, new_ids: Iterable[str] | None
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for sequence in (existing_ids, new_ids):
        if not sequence:
            continue
        for source_id in sequence:
            if source_id and source_id not in seen:
                seen.add(source_id)
                merged.append(source_id)
    return merged


def apply_source_ids_limit(
    source_ids: Sequence[str],
    limit: int,
    method: str,
    *,
    identifier: str | None = None,
) -> list[str]:
    if limit <= 0:
        return []
    source_ids_list = list(source_ids)
    if len(source_ids_list) <= limit:
        return source_ids_list
    normalized_method = normalize_source_ids_limit_method(method)
    truncated = (
        source_ids_list[-limit:]
        if normalized_method == SOURCE_IDS_LIMIT_METHOD_FIFO
        else source_ids_list[:limit]
    )
    if identifier:
        logger.debug(
            "Source_id truncated: %s | %s keeping %s of %s entries",
            identifier,
            normalized_method,
            len(truncated),
            len(source_ids_list),
        )
    return truncated


def make_relation_chunk_key(src: str, tgt: str) -> str:
    return GRAPH_FIELD_SEP.join(sorted((src, tgt)))


def convert_to_user_format(
    entities_context: list[dict],
    relations_context: list[dict],
    chunks: list[dict],
    references: list[dict],
    query_mode: str,
    entity_id_to_original: dict | None = None,
    relation_id_to_original: dict | None = None,
) -> dict[str, Any]:
    formatted_entities = []
    for entity in entities_context:
        entity_name = entity.get("entity", "")
        original_entity = (
            entity_id_to_original.get(entity_name)
            if entity_id_to_original and entity_name in entity_id_to_original
            else None
        )
        if original_entity:
            formatted_entities.append(
                {
                    "entity_name": original_entity.get("entity_name", entity_name),
                    "entity_type": original_entity.get("entity_type", "UNKNOWN"),
                    "description": strip_summary_marker(original_entity.get("description", "")),
                    "source_id": original_entity.get("source_id", ""),
                    "file_path": original_entity.get("file_path", "unknown_source"),
                    "created_at": original_entity.get("created_at", ""),
                }
            )
        else:
            formatted_entities.append(
                {
                    "entity_name": entity_name,
                    "entity_type": entity.get("type", "UNKNOWN"),
                    "description": strip_summary_marker(entity.get("description", "")),
                    "source_id": entity.get("source_id", ""),
                    "file_path": entity.get("file_path", "unknown_source"),
                    "created_at": entity.get("created_at", ""),
                }
            )
    formatted_relationships = []
    for relation in relations_context:
        entity1 = relation.get("entity1", "")
        entity2 = relation.get("entity2", "")
        relation_key = (entity1, entity2)
        original_relation = (
            relation_id_to_original.get(relation_key)
            if relation_id_to_original and relation_key in relation_id_to_original
            else None
        )
        if original_relation:
            formatted_relationships.append(
                {
                    "src_id": original_relation.get("src_id", entity1),
                    "tgt_id": original_relation.get("tgt_id", entity2),
                    "description": strip_summary_marker(original_relation.get("description", "")),
                    "keywords": original_relation.get("keywords", ""),
                    "weight": original_relation.get("weight", 1.0),
                    "source_id": original_relation.get("source_id", ""),
                    "file_path": original_relation.get("file_path", "unknown_source"),
                    "created_at": original_relation.get("created_at", ""),
                }
            )
        else:
            formatted_relationships.append(
                {
                    "src_id": entity1,
                    "tgt_id": entity2,
                    "description": strip_summary_marker(relation.get("description", "")),
                    "keywords": relation.get("keywords", ""),
                    "weight": relation.get("weight", 1.0),
                    "source_id": relation.get("source_id", ""),
                    "file_path": relation.get("file_path", "unknown_source"),
                    "created_at": relation.get("created_at", ""),
                }
            )
    formatted_chunks = [
        {
            "reference_id": chunk.get("reference_id", ""),
            "content": chunk.get("content", ""),
            "file_path": chunk.get("file_path", "unknown_source"),
            "chunk_id": chunk.get("chunk_id", ""),
        }
        for chunk in chunks
    ]
    return {
        "status": "success",
        "message": "Query processed successfully",
        "data": {
            "entities": formatted_entities,
            "relationships": formatted_relationships,
            "chunks": formatted_chunks,
            "references": references,
        },
        "metadata": {
            "query_mode": query_mode,
            "keywords": {"high_level": [], "low_level": []},
        },
    }


def generate_reference_list_from_chunks(
    chunks: list[dict],
) -> tuple[list[dict], list[dict]]:
    if not chunks:
        return [], []
    file_path_counts: dict[str, int] = {}
    for chunk in chunks:
        file_path = chunk.get("file_path", "")
        if file_path and file_path != "unknown_source":
            file_path_counts[file_path] = file_path_counts.get(file_path, 0) + 1
    file_path_with_indices = []
    seen_paths = set()
    for i, chunk in enumerate(chunks):
        file_path = chunk.get("file_path", "")
        if file_path and file_path != "unknown_source" and file_path not in seen_paths:
            file_path_with_indices.append((file_path, file_path_counts[file_path], i))
            seen_paths.add(file_path)
    sorted_file_paths = sorted(file_path_with_indices, key=lambda x: (-x[1], x[2]))
    unique_file_paths = [item[0] for item in sorted_file_paths]
    file_path_to_ref_id = {
        file_path: str(i + 1) for i, file_path in enumerate(unique_file_paths)
    }
    updated_chunks = []
    for chunk in chunks:
        chunk_copy = chunk.copy()
        file_path = chunk_copy.get("file_path", "")
        chunk_copy["reference_id"] = (
            file_path_to_ref_id[file_path]
            if file_path and file_path != "unknown_source"
            else ""
        )
        updated_chunks.append(chunk_copy)
    reference_list = [
        {"reference_id": str(i + 1), "file_path": file_path}
        for i, file_path in enumerate(unique_file_paths)
    ]
    return reference_list, updated_chunks


async def _cooperative_yield(counter: int, every: int = 128) -> None:
    if counter % every == 0:
        await asyncio.sleep(0)


def performance_timing_log(message: str, *args: Any) -> None:
    logger.debug(message, *args)


def pack_user_ass_to_openai_messages(*args: str):
    roles = ["user", "assistant"]
    return [{"role": roles[i % 2], "content": content} for i, content in enumerate(args)]
