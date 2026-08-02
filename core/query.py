from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from functools import partial
from typing import Any

from config import (
    DEFAULT_KG_EVIDENCE_MIN_SIMILARITY,
    DEFAULT_KG_EVIDENCE_TOP_K,
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_YOY_SIGNAL_PHRASES,
    PROMPTS,
    GRAPH_FIELD_SEP,
)
from core.storage.base import (
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
    QueryContextResult,
    QueryParam,
    QueryResult,
)
from core.utils import (
    compute_args_hash,
    compute_mdhash_id,
    convert_to_user_format,
    cosine_similarity,
    generate_reference_list_from_chunks,
    handle_cache,
    logger,
    process_chunks_unified,
    remove_think_tags,
    save_to_cache,
    split_string_by_multi_markers,
    strip_summary_marker,
    truncate_list_by_token_size,
    use_llm_func_with_cache,
    CacheData,
)


class EvidenceVectorIntegrityError(RuntimeError):
    """Raised when stored evidence cannot be resolved to a chunk vector."""


async def select_evidence_chunks_by_vector(
    query: str,
    candidates: list[dict],
    chunks_vdb: BaseVectorStorage,
    top_k: int,
    min_similarity: float,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    """Strictly rank a merged evidence set using the original query vector."""
    if not candidates:
        return []
    if chunks_vdb is None:
        raise EvidenceVectorIntegrityError("chunk vector storage is required for evidence retrieval")
    if top_k <= 0:
        raise ValueError("kg_evidence_top_k must be greater than zero")
    if not -1.0 <= min_similarity <= 1.0:
        raise ValueError("kg_evidence_min_similarity must be between -1.0 and 1.0")

    unique_candidates: list[dict] = []
    seen_chunk_ids: set[str] = set()
    for candidate in candidates:
        chunk_id = candidate.get("chunk_id") or candidate.get("id")
        if not chunk_id or chunk_id in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_id)
        normalized = candidate.copy()
        normalized["chunk_id"] = chunk_id
        unique_candidates.append(normalized)

    vector_id_by_chunk_id = {
        candidate["chunk_id"]: compute_mdhash_id(candidate["chunk_id"], prefix="vchunk-")
        for candidate in unique_candidates
    }
    vectors = await chunks_vdb.get_vectors_by_ids(list(vector_id_by_chunk_id.values()))
    missing_chunk_ids = [
        chunk_id
        for chunk_id, vector_id in vector_id_by_chunk_id.items()
        if vector_id not in vectors
    ]
    if missing_chunk_ids:
        preview = ", ".join(missing_chunk_ids[:5])
        suffix = "" if len(missing_chunk_ids) <= 5 else f" (+{len(missing_chunk_ids) - 5} more)"
        raise EvidenceVectorIntegrityError(
            f"missing chunk vector(s) for evidence: {preview}{suffix}; reingest or rebuild the chunk vector index"
        )

    if query_embedding is None:
        embedding_func = getattr(chunks_vdb, "embedding_func", None)
        if embedding_func is None:
            raise EvidenceVectorIntegrityError("chunk vector storage has no query embedding function")
        query_embedding = (await embedding_func([query], context="query", _priority=5))[0]

    scored_candidates: list[tuple[float, int, dict]] = []
    for index, candidate in enumerate(unique_candidates):
        vector_id = vector_id_by_chunk_id[candidate["chunk_id"]]
        similarity = cosine_similarity(query_embedding, vectors[vector_id])
        if similarity < min_similarity:
            continue
        ranked = candidate.copy()
        ranked["similarity_score"] = similarity
        scored_candidates.append((similarity, index, ranked))
    scored_candidates.sort(key=lambda item: (-item[0], item[1]))
    return [candidate for _score, _index, candidate in scored_candidates[:top_k]]


def _detect_comparison_query(query: str, signal_phrases: list[str]) -> bool:
    """
    Returns True if query is asking for a comparison between two time periods.

    The phrase list is domain configuration (config.py: yoy_signal_phrases /
    GRAPHRAG_YOY_SIGNAL_PHRASES), not core logic.
    """
    query_lower = query.lower()
    return any(phrase in query_lower for phrase in signal_phrases)


async def extract_keywords_only(
    text: str,
    param: QueryParam,
    global_config: dict[str, Any],
    hashing_kv: BaseKVStorage | None = None,
) -> tuple[list[str], list[str]]:
    if param.ll_keywords or param.hl_keywords:
        return param.hl_keywords, param.ll_keywords
    model_func = param.model_func or global_config.get("llm_model_func")
    if not model_func:
        return [text], [text]
    prompt = (
        "Extract high_level_keywords and low_level_keywords as compact JSON from:\n"
        f"{text}"
    )
    result, _ = await use_llm_func_with_cache(
        prompt,
        partial(model_func, _priority=5),
        llm_response_cache=hashing_kv,
        cache_type="keywords",
    )
    try:
        data = json.loads(remove_think_tags(result))
        return data.get("high_level_keywords", []) or [text], data.get(
            "low_level_keywords", []
        ) or [text]
    except Exception:
        return [text], [text]


async def get_keywords_from_query(
    query: str,
    query_param: QueryParam,
    global_config: dict[str, Any],
    hashing_kv: BaseKVStorage | None = None,
) -> tuple[list[str], list[str]]:
    if query_param.hl_keywords or query_param.ll_keywords:
        return query_param.hl_keywords, query_param.ll_keywords
    return await extract_keywords_only(query, query_param, global_config, hashing_kv)


async def _get_vector_context(
    query: str,
    chunks_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    try:
        search_top_k = query_param.chunk_top_k or query_param.top_k
        results = await chunks_vdb.query(query, top_k=search_top_k, query_embedding=query_embedding)
        valid_chunks = []
        for result in results:
            if "content" in result:
                valid_chunks.append(
                    {
                        "content": result["content"],
                        "created_at": result.get("created_at"),
                        "file_path": result.get("file_path", "unknown_source"),
                        "source_type": "vector",
                        "chunk_id": result.get("chunk_id") or result.get("id"),
                    }
                )
        return valid_chunks
    except Exception as exc:
        logger.error("Error in _get_vector_context: %s", exc)
        return []


async def kg_query(
    query: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    global_config: dict[str, Any],
    hashing_kv: BaseKVStorage | None = None,
    system_prompt: str | None = None,
    chunks_vdb: BaseVectorStorage = None,
) -> QueryResult | None:
    if not query:
        return QueryResult(content=PROMPTS["fail_response"])
    if query_param.mode == "hybrid" and _detect_comparison_query(
        query, global_config.get("yoy_signal_phrases", DEFAULT_YOY_SIGNAL_PHRASES)
    ):
        query_param = replace(query_param, mode="global")
        logger.info("Query auto-upgraded to global mode: comparison intent detected")
    use_model_func = query_param.model_func or global_config.get("llm_model_func")
    if use_model_func is not None:
        use_model_func = partial(use_model_func, _priority=5)
    hl_keywords, ll_keywords = await get_keywords_from_query(
        query, query_param, global_config, hashing_kv
    )
    if not hl_keywords and not ll_keywords:
        ll_keywords = [query]
    ll_keywords_str = ", ".join(ll_keywords) if ll_keywords else ""
    hl_keywords_str = ", ".join(hl_keywords) if hl_keywords else ""
    context_result = await _build_query_context(
        query,
        ll_keywords_str,
        hl_keywords_str,
        knowledge_graph_inst,
        entities_vdb,
        relationships_vdb,
        text_chunks_db,
        query_param,
        chunks_vdb,
    )
    if context_result is None:
        empty_raw_data = convert_to_user_format([], [], [], [], query_param.mode)
        empty_raw_data["status"] = "failure"
        empty_raw_data["message"] = PROMPTS["fail_response"]
        return QueryResult(content=PROMPTS["fail_response"], raw_data=empty_raw_data)
    if query_param.only_need_context and not query_param.only_need_prompt:
        return QueryResult(content=context_result.context, raw_data=context_result.raw_data)
    user_prompt = f"\n\n{query_param.user_prompt}" if query_param.user_prompt else "n/a"
    response_type = query_param.response_type or "Multiple Paragraphs"
    sys_prompt = (system_prompt or PROMPTS["rag_response"]).format(
        response_type=response_type,
        user_prompt=user_prompt,
        context_data=context_result.context,
    )
    if query_param.only_need_prompt:
        prompt_content = "\n\n".join([sys_prompt, "---User Query---", query])
        return QueryResult(content=prompt_content, raw_data=context_result.raw_data)
    if use_model_func is None:
        return QueryResult(content=context_result.context, raw_data=context_result.raw_data)
    args_hash = compute_args_hash(
        query_param.mode,
        query,
        query_param.response_type,
        query_param.top_k,
        query_param.chunk_top_k,
        query_param.max_entity_tokens,
        query_param.max_relation_tokens,
        query_param.max_total_tokens,
        hl_keywords_str,
        ll_keywords_str,
        query_param.user_prompt or "",
        query_param.enable_rerank,
        global_config.get("kg_evidence_top_k", DEFAULT_KG_EVIDENCE_TOP_K),
        global_config.get(
            "kg_evidence_min_similarity", DEFAULT_KG_EVIDENCE_MIN_SIMILARITY
        ),
        # The response depends on the fully rendered prompt, which includes
        # both the selected graph context and any custom system-prompt template.
        # History is a separate model input and must therefore also be keyed.
        sys_prompt,
        json.dumps(query_param.conversation_history, ensure_ascii=False, sort_keys=True),
    )
    cached_result = await handle_cache(
        hashing_kv, args_hash, query, query_param.mode, cache_type="query"
    )
    if cached_result is not None:
        response = cached_result[0]
    else:
        response = await use_model_func(
            query,
            system_prompt=sys_prompt,
            history_messages=query_param.conversation_history,
            enable_cot=True,
            stream=query_param.stream,
        )
        if hashing_kv and hashing_kv.global_config.get("enable_llm_cache"):
            await save_to_cache(
                hashing_kv,
                CacheData(
                    args_hash=args_hash,
                    content=response,
                    prompt=query,
                    mode=query_param.mode,
                    cache_type="query",
                    queryparam={
                        "mode": query_param.mode,
                        "response_type": query_param.response_type,
                    },
                ),
            )
    if isinstance(response, str):
        if len(response) > len(sys_prompt):
            response = (
                response.replace(sys_prompt, "")
                .replace("user", "")
                .replace("model", "")
                .replace(query, "")
                .replace("<system>", "")
                .replace("</system>", "")
                .strip()
            )
        return QueryResult(content=response, raw_data=context_result.raw_data)
    return QueryResult(
        response_iterator=response,
        raw_data=context_result.raw_data,
        is_streaming=True,
    )


async def _perform_kg_search(
    query: str,
    ll_keywords: str,
    hl_keywords: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    chunks_vdb: BaseVectorStorage = None,
) -> dict[str, Any]:
    local_entities = []
    local_relations = []
    global_entities = []
    global_relations = []
    vector_chunks = []
    chunk_tracking: dict[str, dict[str, Any]] = {}
    actual_embedding_func = getattr(chunks_vdb, "embedding_func", None) or getattr(
        entities_vdb, "embedding_func", None
    )
    query_embedding = None
    ll_embedding = None
    hl_embedding = None
    mode = query_param.mode
    need_ll = mode in ("local", "hybrid", "mix") and bool(ll_keywords)
    need_hl = mode in ("global", "hybrid", "mix") and bool(hl_keywords)
    if actual_embedding_func:
        texts_to_embed = []
        text_purposes = []
        if query and chunks_vdb:
            texts_to_embed.append(query)
            text_purposes.append("query")
        if need_ll:
            texts_to_embed.append(ll_keywords)
            text_purposes.append("ll")
        if need_hl:
            texts_to_embed.append(hl_keywords)
            text_purposes.append("hl")
        if texts_to_embed:
            try:
                all_embeddings = await actual_embedding_func(
                    texts_to_embed, context="query", _priority=5
                )
                for i, purpose in enumerate(text_purposes):
                    if purpose == "query":
                        query_embedding = all_embeddings[i]
                    elif purpose == "ll":
                        ll_embedding = all_embeddings[i]
                    elif purpose == "hl":
                        hl_embedding = all_embeddings[i]
            except Exception as exc:
                logger.warning("Failed to batch pre-compute embeddings: %s", exc)
    if query_param.mode == "local" and len(ll_keywords) > 0:
        local_entities, local_relations = await _get_node_data(
            ll_keywords, knowledge_graph_inst, entities_vdb, query_param, ll_embedding
        )
    elif query_param.mode == "global" and len(hl_keywords) > 0:
        global_relations, global_entities = await _get_edge_data(
            hl_keywords, knowledge_graph_inst, relationships_vdb, query_param, hl_embedding
        )
    else:
        if len(ll_keywords) > 0:
            local_entities, local_relations = await _get_node_data(
                ll_keywords, knowledge_graph_inst, entities_vdb, query_param, ll_embedding
            )
        if len(hl_keywords) > 0:
            global_relations, global_entities = await _get_edge_data(
                hl_keywords, knowledge_graph_inst, relationships_vdb, query_param, hl_embedding
            )
        if query_param.mode == "mix" and chunks_vdb:
            vector_chunks = await _get_vector_context(query, chunks_vdb, query_param, query_embedding)
            for i, chunk in enumerate(vector_chunks):
                chunk_id = chunk.get("chunk_id") or chunk.get("id")
                if chunk_id:
                    chunk_tracking[chunk_id] = {"source": "C", "frequency": 1, "order": i + 1}
    final_entities = []
    seen_entities = set()
    for i in range(max(len(local_entities), len(global_entities))):
        if i < len(local_entities):
            entity = local_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)
        if i < len(global_entities):
            entity = global_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)
    final_relations = []
    seen_relations = set()
    for i in range(max(len(local_relations), len(global_relations))):
        if i < len(local_relations):
            relation = local_relations[i]
            rel_key = (
                tuple(sorted(relation["src_tgt"]))
                if "src_tgt" in relation
                else tuple(sorted([relation.get("src_id"), relation.get("tgt_id")]))
            )
            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)
        if i < len(global_relations):
            relation = global_relations[i]
            rel_key = (
                tuple(sorted(relation["src_tgt"]))
                if "src_tgt" in relation
                else tuple(sorted([relation.get("src_id"), relation.get("tgt_id")]))
            )
            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)
    return {
        "final_entities": final_entities,
        "final_relations": final_relations,
        "vector_chunks": vector_chunks,
        "chunk_tracking": chunk_tracking,
        "query_embedding": query_embedding,
    }


async def _apply_token_truncation(
    search_result: dict[str, Any],
    query_param: QueryParam,
    global_config: dict[str, Any],
) -> dict[str, Any]:
    tokenizer = global_config.get("tokenizer")
    if not tokenizer:
        return {
            "entities_context": [],
            "relations_context": [],
            "filtered_entities": search_result["final_entities"],
            "filtered_relations": search_result["final_relations"],
            "entity_id_to_original": {},
            "relation_id_to_original": {},
        }
    max_entity_tokens = getattr(
        query_param, "max_entity_tokens", global_config.get("max_entity_tokens", DEFAULT_MAX_ENTITY_TOKENS)
    )
    max_relation_tokens = getattr(
        query_param, "max_relation_tokens", global_config.get("max_relation_tokens", DEFAULT_MAX_RELATION_TOKENS)
    )
    final_entities = search_result["final_entities"]
    final_relations = search_result["final_relations"]
    entity_id_to_original = {}
    relation_id_to_original = {}
    entities_context = []
    for entity in final_entities:
        entity_name = entity["entity_name"]
        entity_id_to_original[entity_name] = entity
        created_at = entity.get("created_at", "UNKNOWN")
        if isinstance(created_at, (int, float)):
            created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        entities_context.append(
            {
                "entity": entity_name,
                "type": entity.get("entity_type", "UNKNOWN"),
                # marker is internal bookkeeping — keep it out of the LLM context
                "description": strip_summary_marker(entity.get("description", "UNKNOWN")),
                "created_at": created_at,
                "file_path": entity.get("file_path", "unknown_source"),
            }
        )
    relations_context = []
    for relation in final_relations:
        created_at = relation.get("created_at", "UNKNOWN")
        if isinstance(created_at, (int, float)):
            created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        if "src_tgt" in relation:
            entity1, entity2 = relation["src_tgt"]
        else:
            entity1, entity2 = relation.get("src_id"), relation.get("tgt_id")
        relation_id_to_original[(entity1, entity2)] = relation
        relations_context.append(
            {
                "entity1": entity1,
                "entity2": entity2,
                "description": strip_summary_marker(relation.get("description", "UNKNOWN")),
                "created_at": created_at,
                "file_path": relation.get("file_path", "unknown_source"),
            }
        )
    if entities_context:
        entities_context = truncate_list_by_token_size(
            [{k: v for k, v in entity.items() if k not in {"file_path", "created_at"}} for entity in entities_context],
            key=lambda x: json.dumps(x, ensure_ascii=False),
            max_token_size=max_entity_tokens,
            tokenizer=tokenizer,
        )
    if relations_context:
        relations_context = truncate_list_by_token_size(
            [{k: v for k, v in relation.items() if k not in {"file_path", "created_at"}} for relation in relations_context],
            key=lambda x: json.dumps(x, ensure_ascii=False),
            max_token_size=max_relation_tokens,
            tokenizer=tokenizer,
        )
    filtered_entities = []
    filtered_entity_id_to_original = {}
    final_entity_names = {item["entity"] for item in entities_context}
    for entity in final_entities:
        name = entity.get("entity_name")
        if name in final_entity_names and name not in filtered_entity_id_to_original:
            filtered_entities.append(entity)
            filtered_entity_id_to_original[name] = entity
    filtered_relations = []
    filtered_relation_id_to_original = {}
    final_relation_pairs = {(item["entity1"], item["entity2"]) for item in relations_context}
    for relation in final_relations:
        src, tgt = relation.get("src_id"), relation.get("tgt_id")
        if src is None or tgt is None:
            src, tgt = relation.get("src_tgt", (None, None))
        pair = (src, tgt)
        if pair in final_relation_pairs and pair not in filtered_relation_id_to_original:
            filtered_relations.append(relation)
            filtered_relation_id_to_original[pair] = relation
    return {
        "entities_context": entities_context,
        "relations_context": relations_context,
        "filtered_entities": filtered_entities,
        "filtered_relations": filtered_relations,
        "entity_id_to_original": filtered_entity_id_to_original,
        "relation_id_to_original": filtered_relation_id_to_original,
    }


async def _merge_all_chunks(
    filtered_entities: list[dict],
    filtered_relations: list[dict],
    vector_chunks: list[dict],
    query: str = "",
    knowledge_graph_inst: BaseGraphStorage = None,
    text_chunks_db: BaseKVStorage = None,
    query_param: QueryParam = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding: list[float] = None,
) -> list[dict]:
    chunk_tracking = chunk_tracking or {}
    entity_chunks = []
    if filtered_entities and text_chunks_db:
        entity_chunks = await _find_related_text_unit_from_entities(
            filtered_entities,
            query_param,
            text_chunks_db,
            knowledge_graph_inst,
            query,
            chunks_vdb,
            chunk_tracking,
            query_embedding,
        )
    relation_chunks = []
    if filtered_relations and text_chunks_db:
        relation_chunks = await _find_related_text_unit_from_relations(
            filtered_relations,
            query_param,
            text_chunks_db,
            entity_chunks,
            query,
            chunks_vdb,
            chunk_tracking,
            query_embedding,
        )
    merged_chunks = []
    seen_chunk_ids = set()
    max_len = max(len(vector_chunks), len(entity_chunks), len(relation_chunks))
    for i in range(max_len):
        for chunk_list in (vector_chunks, entity_chunks, relation_chunks):
            if i >= len(chunk_list):
                continue
            chunk = chunk_list[i]
            chunk_id = chunk.get("chunk_id") or chunk.get("id")
            if chunk_id and chunk_id not in seen_chunk_ids:
                seen_chunk_ids.add(chunk_id)
                merged_chunks.append(
                    {
                        "content": chunk["content"],
                        "file_path": chunk.get("file_path", "unknown_source"),
                        "chunk_id": chunk_id,
                    }
                )
    global_config = text_chunks_db.global_config
    return await select_evidence_chunks_by_vector(
        query=query,
        candidates=merged_chunks,
        chunks_vdb=chunks_vdb,
        top_k=global_config.get("kg_evidence_top_k", DEFAULT_KG_EVIDENCE_TOP_K),
        min_similarity=global_config.get(
            "kg_evidence_min_similarity", DEFAULT_KG_EVIDENCE_MIN_SIMILARITY
        ),
        query_embedding=query_embedding,
    )


async def _build_context_str(
    entities_context: list[dict],
    relations_context: list[dict],
    merged_chunks: list[dict],
    query: str,
    query_param: QueryParam,
    global_config: dict[str, Any],
    chunk_tracking: dict = None,
    entity_id_to_original: dict = None,
    relation_id_to_original: dict = None,
) -> tuple[str, dict[str, Any]]:
    tokenizer = global_config.get("tokenizer")
    if not tokenizer:
        empty_raw_data = convert_to_user_format([], [], [], [], query_param.mode)
        empty_raw_data["status"] = "failure"
        empty_raw_data["message"] = "Missing tokenizer, cannot build LLM context."
        return "", empty_raw_data
    max_total_tokens = getattr(
        query_param, "max_total_tokens", global_config.get("max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS)
    )
    sys_prompt_template = global_config.get("system_prompt_template", PROMPTS["rag_response"])
    kg_context_template = PROMPTS["kg_query_context"]
    user_prompt = query_param.user_prompt or ""
    response_type = query_param.response_type or "Multiple Paragraphs"
    entities_str = "\n".join(json.dumps(entity, ensure_ascii=False) for entity in entities_context)
    relations_str = "\n".join(json.dumps(relation, ensure_ascii=False) for relation in relations_context)
    pre_kg_context = kg_context_template.format(
        entities_str=entities_str,
        relations_str=relations_str,
        text_chunks_str="",
        reference_list_str="",
    )
    kg_context_tokens = len(tokenizer.encode(pre_kg_context))
    pre_sys_prompt = sys_prompt_template.format(context_data="", response_type=response_type, user_prompt=user_prompt)
    sys_prompt_tokens = len(tokenizer.encode(pre_sys_prompt))
    query_tokens = len(tokenizer.encode(query))
    buffer_tokens = 200
    available_chunk_tokens = max_total_tokens - (sys_prompt_tokens + kg_context_tokens + query_tokens + buffer_tokens)
    truncated_chunks = await process_chunks_unified(
        query=query,
        unique_chunks=merged_chunks,
        query_param=query_param,
        global_config=global_config,
        source_type=query_param.mode,
        chunk_token_limit=available_chunk_tokens,
    )
    reference_list, truncated_chunks = generate_reference_list_from_chunks(truncated_chunks)
    chunks_context = [{"reference_id": chunk["reference_id"], "content": chunk["content"]} for chunk in truncated_chunks]
    text_units_str = "\n".join(json.dumps(item, ensure_ascii=False) for item in chunks_context)
    reference_list_str = "\n".join(
        f"[{ref['reference_id']}] {ref['file_path']}" for ref in reference_list if ref["reference_id"]
    )
    if not entities_context and not relations_context and not chunks_context:
        empty_raw_data = convert_to_user_format([], [], [], [], query_param.mode)
        empty_raw_data["status"] = "failure"
        empty_raw_data["message"] = "Query returned empty dataset."
        return "", empty_raw_data
    result = kg_context_template.format(
        entities_str=entities_str,
        relations_str=relations_str,
        text_chunks_str=text_units_str,
        reference_list_str=reference_list_str,
    )
    final_data = convert_to_user_format(
        entities_context,
        relations_context,
        truncated_chunks,
        reference_list,
        query_param.mode,
        entity_id_to_original,
        relation_id_to_original,
    )
    return result, final_data


async def _build_query_context(
    query: str,
    ll_keywords: str,
    hl_keywords: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    chunks_vdb: BaseVectorStorage = None,
) -> QueryContextResult | None:
    if not query:
        return None
    search_result = await _perform_kg_search(
        query,
        ll_keywords,
        hl_keywords,
        knowledge_graph_inst,
        entities_vdb,
        relationships_vdb,
        text_chunks_db,
        query_param,
        chunks_vdb,
    )
    if not search_result["final_entities"] and not search_result["final_relations"]:
        if query_param.mode != "mix" or not search_result["chunk_tracking"]:
            return None
    truncation_result = await _apply_token_truncation(
        search_result, query_param, text_chunks_db.global_config
    )
    merged_chunks = await _merge_all_chunks(
        filtered_entities=truncation_result["filtered_entities"],
        filtered_relations=truncation_result["filtered_relations"],
        vector_chunks=search_result["vector_chunks"],
        query=query,
        knowledge_graph_inst=knowledge_graph_inst,
        text_chunks_db=text_chunks_db,
        query_param=query_param,
        chunks_vdb=chunks_vdb,
        chunk_tracking=search_result["chunk_tracking"],
        query_embedding=search_result["query_embedding"],
    )
    if (
        not merged_chunks
        and not truncation_result["entities_context"]
        and not truncation_result["relations_context"]
    ):
        return None
    context, raw_data = await _build_context_str(
        entities_context=truncation_result["entities_context"],
        relations_context=truncation_result["relations_context"],
        merged_chunks=merged_chunks,
        query=query,
        query_param=query_param,
        global_config=text_chunks_db.global_config,
        chunk_tracking=search_result["chunk_tracking"],
        entity_id_to_original=truncation_result["entity_id_to_original"],
        relation_id_to_original=truncation_result["relation_id_to_original"],
    )
    hl_keywords_list = hl_keywords.split(", ") if hl_keywords else []
    ll_keywords_list = ll_keywords.split(", ") if ll_keywords else []
    raw_data.setdefault("metadata", {})
    raw_data["metadata"]["keywords"] = {
        "high_level": hl_keywords_list,
        "low_level": ll_keywords_list,
    }
    raw_data["metadata"]["processing_info"] = {
        "total_entities_found": len(search_result.get("final_entities", [])),
        "total_relations_found": len(search_result.get("final_relations", [])),
        "entities_after_truncation": len(truncation_result.get("filtered_entities", [])),
        "relations_after_truncation": len(truncation_result.get("filtered_relations", [])),
        "merged_chunks_count": len(merged_chunks),
        "final_chunks_count": len(raw_data.get("data", {}).get("chunks", [])),
    }
    return QueryContextResult(context=context, raw_data=raw_data)


async def _get_node_data(
    query: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding=None,
):
    results = await entities_vdb.query(query, top_k=query_param.top_k, query_embedding=query_embedding)
    if not results:
        return [], []
    node_ids = [result["entity_name"] for result in results]
    nodes_dict, degrees_dict = await asyncio.gather(
        knowledge_graph_inst.get_nodes_batch(node_ids),
        knowledge_graph_inst.node_degrees_batch(node_ids),
    )
    node_datas = [
        {
            **node,
            "entity_name": result["entity_name"],
            "rank": degrees_dict.get(result["entity_name"], 0),
            "created_at": result.get("created_at"),
        }
        for result in results
        if (node := nodes_dict.get(result["entity_name"])) is not None
    ]
    use_relations = await _find_most_related_edges_from_entities(
        node_datas, query_param, knowledge_graph_inst
    )
    return node_datas, use_relations


async def _find_most_related_edges_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    knowledge_graph_inst: BaseGraphStorage,
):
    node_names = [dp["entity_name"] for dp in node_datas]
    batch_edges_dict = await knowledge_graph_inst.get_nodes_edges_batch(node_names)
    all_edges = []
    seen = set()
    for node_name in node_names:
        for edge in batch_edges_dict.get(node_name, []):
            sorted_edge = tuple(sorted(edge))
            if sorted_edge not in seen:
                seen.add(sorted_edge)
                all_edges.append(sorted_edge)
    edge_pairs_dicts = [{"src": edge[0], "tgt": edge[1]} for edge in all_edges]
    edge_data_dict, edge_degrees_dict = await asyncio.gather(
        knowledge_graph_inst.get_edges_batch(edge_pairs_dicts),
        knowledge_graph_inst.edge_degrees_batch(list(all_edges)),
    )
    all_edges_data = []
    for pair in all_edges:
        edge_props = edge_data_dict.get(pair)
        if edge_props is not None:
            edge_props.setdefault("weight", 1.0)
            all_edges_data.append(
                {"src_tgt": pair, "rank": edge_degrees_dict.get(pair, 0), **edge_props}
            )
    return sorted(all_edges_data, key=lambda x: (x["rank"], x["weight"]), reverse=True)


async def _find_related_text_unit_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    text_chunks_db: BaseKVStorage,
    knowledge_graph_inst: BaseGraphStorage,
    query: str = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding=None,
):
    if not node_datas:
        return []
    entities_with_chunks = []
    for entity in node_datas:
        if entity.get("source_id"):
            chunks = split_string_by_multi_markers(entity["source_id"], [GRAPH_FIELD_SEP])
            if chunks:
                entities_with_chunks.append(
                    {
                        "entity_name": entity["entity_name"],
                        "chunks": chunks,
                        "entity_data": entity,
                    }
                )
    if not entities_with_chunks:
        return []
    chunk_occurrence_count = {}
    for entity_info in entities_with_chunks:
        deduplicated_chunks = []
        for chunk_id in entity_info["chunks"]:
            chunk_occurrence_count[chunk_id] = chunk_occurrence_count.get(chunk_id, 0) + 1
            if chunk_occurrence_count[chunk_id] == 1:
                deduplicated_chunks.append(chunk_id)
        entity_info["chunks"] = deduplicated_chunks
        entity_info["sorted_chunks"] = sorted(
            deduplicated_chunks,
            key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
            reverse=True,
        )
    selected_chunk_ids = list(chunk_occurrence_count)
    unique_chunk_ids = list(dict.fromkeys(selected_chunk_ids))
    chunk_data_list = await text_chunks_db.get_by_ids(unique_chunk_ids)
    result_chunks = []
    for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
        if chunk_data is not None and "content" in chunk_data:
            chunk_copy = chunk_data.copy()
            chunk_copy["source_type"] = "entity"
            chunk_copy["chunk_id"] = chunk_id
            result_chunks.append(chunk_copy)
            if chunk_tracking is not None:
                chunk_tracking[chunk_id] = {
                    "source": "E",
                    "frequency": chunk_occurrence_count.get(chunk_id, 1),
                    "order": i + 1,
                }
        else:
            logger.warning("Unresolvable entity evidence chunk `%s` was skipped during query", chunk_id)
    return result_chunks


async def _get_edge_data(
    keywords,
    knowledge_graph_inst: BaseGraphStorage,
    relationships_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding=None,
):
    results = await relationships_vdb.query(
        keywords, top_k=query_param.top_k, query_embedding=query_embedding
    )
    if not results:
        return [], []
    edge_data_dict = await knowledge_graph_inst.get_edges_batch(
        [{"src": result["src_id"], "tgt": result["tgt_id"]} for result in results]
    )
    edge_datas = []
    for result in results:
        pair = (result["src_id"], result["tgt_id"])
        edge_props = edge_data_dict.get(pair)
        if edge_props is not None:
            edge_props.setdefault("weight", 1.0)
            edge_datas.append(
                {
                    "src_id": result["src_id"],
                    "tgt_id": result["tgt_id"],
                    "created_at": result.get("created_at"),
                    **edge_props,
                }
            )
    use_entities = await _find_most_related_entities_from_relationships(
        edge_datas, query_param, knowledge_graph_inst
    )
    return edge_datas, use_entities


async def _find_most_related_entities_from_relationships(
    edge_datas: list[dict],
    query_param: QueryParam,
    knowledge_graph_inst: BaseGraphStorage,
):
    entity_names = []
    seen = set()
    for edge in edge_datas:
        for node_id in (edge["src_id"], edge["tgt_id"]):
            if node_id not in seen:
                entity_names.append(node_id)
                seen.add(node_id)
    nodes_dict = await knowledge_graph_inst.get_nodes_batch(entity_names)
    return [
        {**node, "entity_name": entity_name}
        for entity_name in entity_names
        if (node := nodes_dict.get(entity_name)) is not None
    ]


async def _find_related_text_unit_from_relations(
    edge_datas: list[dict],
    query_param: QueryParam,
    text_chunks_db: BaseKVStorage,
    entity_chunks: list[dict] = None,
    query: str = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding=None,
):
    if not edge_datas:
        return []
    relations_with_chunks = []
    for relation in edge_datas:
        if relation.get("source_id"):
            chunks = split_string_by_multi_markers(relation["source_id"], [GRAPH_FIELD_SEP])
            if chunks:
                rel_key = (
                    tuple(sorted(relation["src_tgt"]))
                    if "src_tgt" in relation
                    else tuple(sorted([relation.get("src_id"), relation.get("tgt_id")]))
                )
                relations_with_chunks.append(
                    {"relation_key": rel_key, "chunks": chunks, "relation_data": relation}
                )
    if not relations_with_chunks:
        return []
    entity_chunk_ids = {chunk.get("chunk_id") for chunk in (entity_chunks or []) if chunk.get("chunk_id")}
    chunk_occurrence_count = {}
    for relation_info in relations_with_chunks:
        deduplicated_chunks = []
        for chunk_id in relation_info["chunks"]:
            if chunk_id in entity_chunk_ids:
                continue
            chunk_occurrence_count[chunk_id] = chunk_occurrence_count.get(chunk_id, 0) + 1
            if chunk_occurrence_count[chunk_id] == 1:
                deduplicated_chunks.append(chunk_id)
        relation_info["chunks"] = deduplicated_chunks
    relations_with_chunks = [relation for relation in relations_with_chunks if relation["chunks"]]
    if not relations_with_chunks:
        return []
    for relation_info in relations_with_chunks:
        relation_info["sorted_chunks"] = sorted(
            relation_info["chunks"],
            key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
            reverse=True,
        )
    selected_chunk_ids = list(chunk_occurrence_count)
    unique_chunk_ids = list(dict.fromkeys(selected_chunk_ids))
    chunk_data_list = await text_chunks_db.get_by_ids(unique_chunk_ids)
    result_chunks = []
    for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
        if chunk_data is not None and "content" in chunk_data:
            chunk_copy = chunk_data.copy()
            chunk_copy["source_type"] = "relationship"
            chunk_copy["chunk_id"] = chunk_id
            result_chunks.append(chunk_copy)
            if chunk_tracking is not None:
                chunk_tracking[chunk_id] = {
                    "source": "R",
                    "frequency": chunk_occurrence_count.get(chunk_id, 1),
                    "order": i + 1,
                }
        else:
            logger.warning("Unresolvable relationship evidence chunk `%s` was skipped during query", chunk_id)
    return result_chunks
