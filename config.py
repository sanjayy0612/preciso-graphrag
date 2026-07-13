from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import Any

# Same logger core/utils.py configures handlers on; config.py must not import
# core.utils (core.utils imports config).
logger = logging.getLogger("graphrag_mcp")

# ==========================================================================
# TOKEN LIMIT DEFAULTS (used by summary.py, merge.py, query.py)
# Prevents LLM calls from exceeding API token limits
# ==========================================================================
DEFAULT_MAX_ENTITY_TOKENS = int(os.getenv("GRAPHRAG_MAX_ENTITY_TOKENS", "4000"))
DEFAULT_MAX_RELATION_TOKENS = int(os.getenv("GRAPHRAG_MAX_RELATION_TOKENS", "4000"))
DEFAULT_MAX_TOTAL_TOKENS = int(os.getenv("GRAPHRAG_MAX_TOTAL_TOKENS", "16000"))
DEFAULT_RELATED_CHUNK_NUMBER = int(os.getenv("GRAPHRAG_RELATED_CHUNK_NUMBER", "8"))

# ============================================================================
# KNOWLEDGE GRAPH DEFAULTS (used by merge.py, query.py)
# Controls entity/relationship merging and retrieval behavior
# ============================================================================
DEFAULT_KG_CHUNK_PICK_METHOD = "WEIGHT"        # Method to select important chunks: "WEIGHT" or other
DEFAULT_MAX_FILE_PATHS = 8                     # Max source file paths to store per entity/relation
DEFAULT_FILE_PATH_MORE_PLACEHOLDER = "more_paths"  # Placeholder when truncating file paths
DEFAULT_ENTITY_NAME_MAX_LENGTH = 255           # Max entity name length
DEFAULT_TOP_K = 5                              # Top K results to return in queries
DEFAULT_CHUNK_TOP_K = 20                       # Top K chunks to retrieve
GRAPH_FIELD_SEP = "<SEP>"                      # Internal separator for multi-value graph fields

# ============================================================================
# MERGING & SOURCE TRACKING DEFAULTS (used by merge.py)
# Controls how entities/relationships are merged when re-ingested
# ============================================================================
DEFAULT_SOURCE_IDS_LIMIT_METHOD = "KEEP"       # How to limit stored source references: "KEEP" (keep old) or "FIFO" (newest)
SOURCE_IDS_LIMIT_METHOD_KEEP = "KEEP"          # Keep older source IDs when limit reached
SOURCE_IDS_LIMIT_METHOD_FIFO = "FIFO"          # Use FIFO (first-in-first-out) when limit reached
VALID_SOURCE_IDS_LIMIT_METHODS = {
    SOURCE_IDS_LIMIT_METHOD_KEEP,
    SOURCE_IDS_LIMIT_METHOD_FIFO,
}

# ==========================================================================
# QUERY DEFAULTS (used by query.py)
# ==========================================================================
DEFAULT_HISTORY_TURNS = int(os.getenv("GRAPHRAG_HISTORY_TURNS", "3"))
DEFAULT_SUMMARY_LANGUAGE = os.getenv("GRAPHRAG_SUMMARY_LANGUAGE", "English")

# ==========================================================================
# SUMMARY DEFAULTS (used by summary.py, merge.py)
# ==========================================================================
DEFAULT_SUMMARY_CONTEXT_TOKENS = int(os.getenv("GRAPHRAG_SUMMARY_CONTEXT_TOKENS", "8000"))
DEFAULT_SUMMARY_MAX_TOKENS = int(os.getenv("GRAPHRAG_SUMMARY_MAX_TOKENS", "1024"))
DEFAULT_SUMMARY_LENGTH_RECOMMENDED = int(
    os.getenv("GRAPHRAG_SUMMARY_LENGTH_RECOMMENDED", "256")
)
# Two-zone description merge (core/summary.py): descriptions are stored as ONE
# marker-tagged rolling summary of old mentions + a verbatim tail of the most recent
# raw_tail_size descriptions. Compression fires only when the tail exceeds this count
# or the field exceeds summary_context_size — never on a bare description count.
# GRAPHRAG_FORCE_LLM_SUMMARY_ON_MERGE is honored as a legacy alias for the tail size.
DEFAULT_RAW_TAIL_SIZE = int(
    os.getenv(
        "GRAPHRAG_RAW_TAIL_SIZE",
        os.getenv("GRAPHRAG_FORCE_LLM_SUMMARY_ON_MERGE", "4"),
    )
)
# Sentinel prefix identifying the single rolling-summary segment inside a SEP-joined
# description field. Deliberately NOT env-configurable: changing it would orphan
# markers already persisted in GRAPH_IS_HERE/. ASCII on purpose (tokenizer/grep-safe).
#
# CONTRACT — "storage keeps it, every exit strips it":
# The marker exists ONLY inside GRAPH_IS_HERE/ (graph nodes/edges, incl. the
# graphml artifact) so that the next merge can find the rolling-summary segment;
# copying GRAPH_IS_HERE/ to another machine must preserve it. Every surface that
# leaves storage must pass through core.utils.strip_summary_marker:
#   - embedding content            core/merge.py (entity_content / rel_content)
#   - relationship VDB payload     core/merge.py ("description" field)
#   - LLM query context            core/query.py (_apply_token_truncation)
#   - user-facing query results    core/utils.py (convert_to_user_format)
#   - Neo4j export properties      core/export_adapters.py (_node/_edge_properties)
#   - Qdrant export payloads       core/export_adapters.py (_vector_payload)
# Adding a new export/output surface? Strip there too, and extend the guard test
# in test/marker_leak_manual.py.
SUMMARY_MARKER = "<<SUM>>"

# ============================================================================
# EMBEDDING DEFAULTS
# ============================================================================
DEFAULT_EMBEDDING_PROVIDER = os.getenv("GRAPHRAG_EMBEDDING_PROVIDER", "ollama")
DEFAULT_EMBEDDING_MODEL = os.getenv("GRAPHRAG_EMBEDDING_MODEL", "mxbai-embed-large")
DEFAULT_EMBEDDING_DIM = int(os.getenv("GRAPHRAG_EMBEDDING_DIM", "768"))
DEFAULT_EMBEDDING_MAX_TOKENS = int(os.getenv("GRAPHRAG_EMBEDDING_MAX_TOKENS", "8192"))
DEFAULT_OPENAI_EMBEDDING_MODEL = os.getenv("GRAPHRAG_OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_COHERE_EMBEDDING_MODEL = os.getenv("GRAPHRAG_COHERE_EMBEDDING_MODEL", "embed-english-v3.0")

# ============================================================================
# DOWNSTREAM EXPORT DEFAULTS
# Optional settings for Neo4j and Qdrant exports.
# These are read from environment variables so exports stay config-driven.
# ============================================================================
DEFAULT_NEO4J_URI = os.getenv("GRAPHRAG_NEO4J_URI", "")
DEFAULT_NEO4J_USERNAME = os.getenv("GRAPHRAG_NEO4J_USERNAME", "neo4j")
DEFAULT_NEO4J_PASSWORD = os.getenv("GRAPHRAG_NEO4J_PASSWORD", "")
DEFAULT_NEO4J_DATABASE = os.getenv("GRAPHRAG_NEO4J_DATABASE", "neo4j")

DEFAULT_QDRANT_URL = os.getenv("GRAPHRAG_QDRANT_URL", "")
DEFAULT_QDRANT_API_KEY = os.getenv("GRAPHRAG_QDRANT_API_KEY", "")
DEFAULT_QDRANT_COLLECTION_PREFIX = os.getenv("GRAPHRAG_QDRANT_COLLECTION_PREFIX", "preciso")

DEFAULT_EXPORT_WORKSPACE = os.getenv("GRAPHRAG_EXPORT_WORKSPACE", "default")
DEFAULT_EXPORT_CLEAR_EXISTING = os.getenv("GRAPHRAG_EXPORT_CLEAR_EXISTING", "false").lower() == "true"

# ============================================================================
# LLM PROMPTS (used by summary.py, query.py)
# Templates sent to LLM functions for summarization and response generation
# ============================================================================
PROMPTS = {
    "summarize_entity_descriptions": (
        # Used by summary.py → _summarize_descriptions()
        # Combines multiple entity/relationship descriptions into one concise summary
        "Summarize the following {description_type} information for "
        "{description_name} in {language}. Keep the most important facts and "
        "target about {summary_length} tokens.\n{description_list}"
    ),
    "rag_response": (
        # Used by query.py → kg_query()
        # Final prompt to generate response from knowledge graph context
        "You are a financial graph retrieval assistant.\n"
        "Use the provided context to answer in {response_type}.\n"
        "Additional user guidance: {user_prompt}\n\n"
        "{context_data}"
    ),
    "kg_query_context": (
        # Used by query.py → to format context before sending to LLM
        "-----Entities-----\n{entities_str}\n\n"
        "-----Relationships-----\n{relations_str}\n\n"
        "-----Text Chunks-----\n{text_chunks_str}\n\n"
        "-----References-----\n{reference_list_str}"
    ),
    "fail_response": "No relevant graph context could be constructed for the query.",
}


async def _fallback_embed(texts, **kwargs):
    return [[0.0] * 8 for _ in texts]


async def _ollama_embed(texts: list[str], **kwargs) -> list[list[float]]:
    ollama = importlib.import_module("ollama")
    model = kwargs.get("model") or kwargs.get("model_name") or DEFAULT_EMBEDDING_MODEL
    if not isinstance(texts, list):
        texts = [texts]
    try:
        response = ollama.embeddings(model=model, input=texts)
        return response["embeddings"]
    except TypeError:
        # Older ollama client expects prompt=... per request.
        embeddings: list[list[float]] = []
        for text in texts:
            response = ollama.embeddings(model=model, prompt=text)
            if "embedding" in response:
                embeddings.append(response["embedding"])
            else:
                embeddings.append(response.get("embeddings", [])[0])
        return embeddings


async def _openai_embed(texts: list[str], **kwargs) -> list[list[float]]:
    try:
        from openai import OpenAI
    except Exception as exc:
        raise ImportError("OpenAI SDK not installed. Run: pip install openai") from exc
    api_key = os.getenv("GRAPHRAG_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OpenAI API key missing. Set GRAPHRAG_OPENAI_API_KEY or OPENAI_API_KEY.")
    client = OpenAI(api_key=api_key)
    model = kwargs.get("model") or kwargs.get("model_name") or DEFAULT_OPENAI_EMBEDDING_MODEL
    if not isinstance(texts, list):
        texts = [texts]
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


async def _cohere_embed(texts: list[str], **kwargs) -> list[list[float]]:
    try:
        import cohere
    except Exception as exc:
        raise ImportError("Cohere SDK not installed. Run: pip install cohere") from exc
    api_key = os.getenv("GRAPHRAG_COHERE_API_KEY") or os.getenv("COHERE_API_KEY")
    if not api_key:
        raise ValueError("Cohere API key missing. Set GRAPHRAG_COHERE_API_KEY or COHERE_API_KEY.")
    client = cohere.Client(api_key)
    model = kwargs.get("model") or kwargs.get("model_name") or DEFAULT_COHERE_EMBEDDING_MODEL
    if not isinstance(texts, list):
        texts = [texts]
    response = client.embed(texts=texts, model=model, input_type="search_document")
    embeddings = getattr(response, "embeddings", None) or response.get("embeddings")
    return list(embeddings or [])


def build_default_embedding_func() -> Any:
    """Build the EmbeddingFunc for the configured provider.

    For the ollama provider this performs a NETWORK round-trip to probe the
    model's embedding dimension — call it from server startup (or a script's
    main path), never at module import.
    """
    from core.storage.base import EmbeddingFunc

    provider = DEFAULT_EMBEDDING_PROVIDER.lower()
    if provider == "ollama":
        # Try to auto-detect embedding dimension from the Ollama model.
        # If detection fails, fall back to the configured default.
        detected_dim = DEFAULT_EMBEDDING_DIM
        try:
            import asyncio

            # Call the async embedder to get a real embedding shape
            embeddings = asyncio.run(_ollama_embed(["test"], model=DEFAULT_EMBEDDING_MODEL))
            if embeddings and isinstance(embeddings, list) and len(embeddings) > 0:
                first = embeddings[0]
                detected_dim = len(first)
            else:
                logger.warning(
                    "Ollama embedding-dimension probe returned no embeddings for model %s; "
                    "using default embedding_dim=%d — if the model's real dimension differs, "
                    "vector indexes will be built wrong and reset on the next mismatch",
                    DEFAULT_EMBEDDING_MODEL,
                    DEFAULT_EMBEDDING_DIM,
                )
        except Exception:
            logger.exception(
                "Ollama embedding-dimension probe failed for model %s (is Ollama running?); "
                "using default embedding_dim=%d — if the model's real dimension differs, "
                "vector indexes will be built wrong and reset on the next mismatch",
                DEFAULT_EMBEDDING_MODEL,
                DEFAULT_EMBEDDING_DIM,
            )
            detected_dim = DEFAULT_EMBEDDING_DIM

        return EmbeddingFunc(
            embedding_dim=detected_dim,
            max_token_size=DEFAULT_EMBEDDING_MAX_TOKENS,
            func=_ollama_embed,
            model_name=DEFAULT_EMBEDDING_MODEL,
        )
    if provider == "openai":
        return EmbeddingFunc(
            embedding_dim=DEFAULT_EMBEDDING_DIM,
            max_token_size=DEFAULT_EMBEDDING_MAX_TOKENS,
            func=_openai_embed,
            model_name=DEFAULT_OPENAI_EMBEDDING_MODEL,
        )
    if provider == "cohere":
        return EmbeddingFunc(
            embedding_dim=DEFAULT_EMBEDDING_DIM,
            max_token_size=DEFAULT_EMBEDDING_MAX_TOKENS,
            func=_cohere_embed,
            model_name=DEFAULT_COHERE_EMBEDDING_MODEL,
        )
    if provider == "anthropic":
        raise ValueError("Anthropic does not provide embeddings. Use openai, cohere, or ollama.")
    if provider == "fallback":
        return EmbeddingFunc(
            embedding_dim=8,
            max_token_size=DEFAULT_EMBEDDING_MAX_TOKENS,
            func=_fallback_embed,
            model_name="fallback",
        )
    raise ValueError(f"Unsupported embedding provider: {DEFAULT_EMBEDDING_PROVIDER}")



def build_global_config(
    *,
    working_dir: str | None = None,
    llm_model_func: Any = None,              # LLM function to call: can be OpenAI, Claude, local model, or None (no LLM)
    embedding_func: Any = None,              # Embedding function: converts text to vectors for similarity search
    tokenizer: Any = None,                   # Token counter: counts tokens in text (uses tiktoken or fallback)
    extra: dict[str, Any] | None = None,     # Additional custom config values
) -> dict[str, Any]:
    """
    Build a global configuration dictionary used throughout the system.
    
    This function creates a single config object passed to ALL functions that need settings.
    Instead of hardcoding values in each file, all functions read from this global_config dict.
    """
    base_dir = Path(working_dir or os.getenv("GRAPHRAG_MCP_WORKDIR", "./graphrag_mcp_data"))
    config: dict[str, Any] = {
        # ====================================================================
        # CORE PLUGGABLE FUNCTIONS (can be None or custom implementations)
        # ====================================================================
        "working_dir": str(base_dir),                    # Where to store graph/vector DB data
        "llm_model_func": llm_model_func,                # LLM to use (defaults to None = no LLM)
        "embedding_func": embedding_func,                # Embedding model (converts text→vectors)
        "tokenizer": tokenizer,                          # Token counter (GPT-4o-mini or fallback)
        
        # ====================================================================
        # VECTOR EMBEDDING SETTINGS (used by storage/ and query.py)
        # Controls how text is converted to vectors for similarity search
        # ====================================================================
        "embedding_batch_num": 8,                        # Batch size for embedding operations
        "vector_db_storage_cls_kwargs": {
            "cosine_better_than_threshold": 0.6,         # Min similarity threshold for results
        },
        
        # ====================================================================
        # LLM CACHING SETTINGS (used by utils.py and summary.py)
        # Caches LLM responses to avoid redundant API calls
        # ====================================================================
        "enable_llm_cache": True,                        # Cache query/summarization results
        "enable_llm_cache_for_entity_extract": True,     # Cache extraction-time LLM calls
        
        # ====================================================================
        # SUMMARY & DESCRIPTION SETTINGS (used by summary.py, merge.py)
        # Controls how entity/relationship descriptions are merged
        # ====================================================================
        "summary_context_size": DEFAULT_SUMMARY_CONTEXT_TOKENS,     # Max tokens to show LLM for summarization
        "summary_max_tokens": DEFAULT_SUMMARY_MAX_TOKENS,           # Max tokens in final summary
        "summary_length_recommended": DEFAULT_SUMMARY_LENGTH_RECOMMENDED,  # Target summary length
        "raw_tail_size": DEFAULT_RAW_TAIL_SIZE,                     # Verbatim recent descriptions kept outside the rolling summary
        "force_llm_summary_on_merge": DEFAULT_RAW_TAIL_SIZE,        # Legacy alias of raw_tail_size (old count-based collapse policy removed)
        
        # ====================================================================
        # TOKEN LIMITS (used by utils.py, merge.py, query.py)
        # Prevents text from exceeding API token limits
        # ====================================================================
        "embedding_token_limit": int(os.getenv("GRAPHRAG_EMBEDDING_TOKEN_LIMIT", "0")) or None,
        "max_entity_tokens": DEFAULT_MAX_ENTITY_TOKENS,  # Max tokens per entity description
        "max_relation_tokens": DEFAULT_MAX_RELATION_TOKENS,  # Max tokens per relation description
        "max_total_tokens": DEFAULT_MAX_TOTAL_TOKENS,    # Total token budget per query
        
        # ====================================================================
        # KNOWLEDGE GRAPH SETTINGS (used by merge.py, query.py)
        # Controls entity/relation retrieval and filtering
        # ====================================================================
        "related_chunk_number": DEFAULT_RELATED_CHUNK_NUMBER,  # Chunks to retrieve per query
        "kg_chunk_pick_method": DEFAULT_KG_CHUNK_PICK_METHOD,  # How to select chunks: "WEIGHT" or others
        
        # ====================================================================
        # SOURCE ID TRACKING (used by merge.py)
        # Controls how many source references (chunk IDs) to keep per entity/relation
        # When re-ingesting, older sources may be dropped if limit is reached
        # ====================================================================
        "max_source_ids_per_entity": 64,                 # Max source chunks to track per entity
        "max_source_ids_per_relation": 64,               # Max source chunks to track per relation
        "source_ids_limit_method": DEFAULT_SOURCE_IDS_LIMIT_METHOD,  # "KEEP" old or "FIFO" newest
        
        # ====================================================================
        # FILE PATH TRACKING (used by merge.py)
        # Limits how many file paths are stored per entity/relation
        # ====================================================================
        "max_file_paths": DEFAULT_MAX_FILE_PATHS,        # Max file paths to store
        "file_path_more_placeholder": DEFAULT_FILE_PATH_MORE_PLACEHOLDER,  # Placeholder for truncated paths
        
        # ====================================================================
        # GRAPH FILTERING (used by query.py)
        # Controls graph search and result filtering
        # ====================================================================
        "max_graph_nodes": 1000,                         # Max nodes to consider in graph traversal
        "min_rerank_score": 0.0,                         # Min score to include in results
        
        # ====================================================================
        # LANGUAGE & PROMPTS (used by summary.py, query.py)
        # ====================================================================
        "addon_params": {"language": DEFAULT_SUMMARY_LANGUAGE},  # Language for LLM summaries
        "system_prompt_template": PROMPTS["rag_response"],       # Default system prompt for queries
    }
    if extra:
        config.update(extra)
    return config
