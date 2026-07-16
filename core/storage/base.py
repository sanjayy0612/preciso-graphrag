from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Literal, Optional, TypedDict

from config import (
    DEFAULT_CHUNK_TOP_K,
    DEFAULT_HISTORY_TURNS,
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_TOP_K,
)


class TextChunkSchema(TypedDict):
    tokens: int
    content: str
    full_doc_id: str
    chunk_order_index: int


@dataclass
class QueryParam:
    mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"] = "mix"
    only_need_context: bool = False
    only_need_prompt: bool = False
    response_type: str = "Multiple Paragraphs"
    stream: bool = False
    top_k: int = int(os.getenv("TOP_K", str(DEFAULT_TOP_K)))
    chunk_top_k: int = int(os.getenv("CHUNK_TOP_K", str(DEFAULT_CHUNK_TOP_K)))
    max_entity_tokens: int = int(
        os.getenv("MAX_ENTITY_TOKENS", str(DEFAULT_MAX_ENTITY_TOKENS))
    )
    max_relation_tokens: int = int(
        os.getenv("MAX_RELATION_TOKENS", str(DEFAULT_MAX_RELATION_TOKENS))
    )
    max_total_tokens: int = int(
        os.getenv("MAX_TOTAL_TOKENS", str(DEFAULT_MAX_TOTAL_TOKENS))
    )
    hl_keywords: list[str] = field(default_factory=list)
    ll_keywords: list[str] = field(default_factory=list)
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    history_turns: int = int(os.getenv("HISTORY_TURNS", str(DEFAULT_HISTORY_TURNS)))
    model_func: Callable[..., object] | None = None
    user_prompt: str | None = None
    enable_rerank: bool = False
    include_references: bool = False


@dataclass
class QueryResult:
    content: Optional[str] = None
    response_iterator: Optional[AsyncIterator[str]] = None
    raw_data: Optional[Dict[str, Any]] = None
    is_streaming: bool = False

    @property
    def reference_list(self) -> List[Dict[str, str]]:
        if self.raw_data:
            return self.raw_data.get("data", {}).get("references", [])
        return []

    @property
    def metadata(self) -> Dict[str, Any]:
        if self.raw_data:
            return self.raw_data.get("metadata", {})
        return {}


@dataclass
class QueryContextResult:
    context: str
    raw_data: Dict[str, Any]


@dataclass
class EmbeddingFunc:
    embedding_dim: int
    max_token_size: int
    func: Callable[..., Any]
    model_name: str | None = None
    supports_asymmetric: bool = False

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self.func(*args, **kwargs)


@dataclass
class StorageNameSpace(ABC):
    namespace: str
    workspace: str
    global_config: dict[str, Any]

    async def initialize(self):
        pass

    async def finalize(self):
        pass

    @abstractmethod
    async def index_done_callback(self) -> None:
        ...

    @abstractmethod
    async def drop(self) -> dict[str, str]:
        ...


@dataclass
class BaseVectorStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc
    cosine_better_than_threshold: float = field(default=0.2)
    meta_fields: set[str] = field(default_factory=set)

    def _validate_embedding_func(self):
        if self.embedding_func is None:
            raise ValueError("embedding_func is required for vector storage")

    @abstractmethod
    async def query(
        self, query: str, top_k: int, query_embedding: list[float] | None = None
    ) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        ...

    @abstractmethod
    async def delete_entity(self, entity_name: str) -> None:
        ...

    @abstractmethod
    async def delete_entity_relation(self, entity_name: str) -> None:
        ...

    @abstractmethod
    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        ...

    @abstractmethod
    async def delete(self, ids: list[str]):
        ...

    @abstractmethod
    async def get_vectors_by_ids(self, ids: list[str]) -> dict[str, list[float]]:
        ...


@dataclass
class BaseKVStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc | None

    @abstractmethod
    async def get_by_id(self, id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def get_by_ids(self, ids: list[str]) -> list[dict[str, Any] | None]:
        ...

    @abstractmethod
    async def filter_keys(self, keys: set[str]) -> set[str]:
        ...

    @abstractmethod
    async def upsert(self, data: dict[str, dict[str, Any]]) -> None:
        ...

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        ...

    @abstractmethod
    async def is_empty(self) -> bool:
        ...

    @abstractmethod
    async def get_all_items(self) -> dict[str, dict[str, Any]]:
        """Return a snapshot of every stored item, keyed by id. Lets callers
        enumerate/count a KV store's contents without reaching into a specific
        backend's private attributes."""
        ...


@dataclass
class BaseGraphStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc | None

    @abstractmethod
    async def has_node(self, node_id: str) -> bool:
        ...

    @abstractmethod
    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        ...

    @abstractmethod
    async def node_degree(self, node_id: str) -> int:
        ...

    @abstractmethod
    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        ...

    @abstractmethod
    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def get_edge(
        self, source_node_id: str, target_node_id: str
    ) -> dict[str, Any] | None:
        ...

    @abstractmethod
    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        ...

    async def get_nodes_batch(self, node_ids: list[str]) -> dict[str, dict]:
        result = {}
        for node_id in node_ids:
            node = await self.get_node(node_id)
            if node is not None:
                result[node_id] = node
        return result

    async def node_degrees_batch(self, node_ids: list[str]) -> dict[str, int]:
        result = {}
        for node_id in node_ids:
            result[node_id] = await self.node_degree(node_id)
        return result

    async def edge_degrees_batch(
        self, edge_pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        result = {}
        for src_id, tgt_id in edge_pairs:
            result[(src_id, tgt_id)] = await self.edge_degree(src_id, tgt_id)
        return result

    async def get_edges_batch(
        self, pairs: list[dict[str, str]]
    ) -> dict[tuple[str, str], dict]:
        result = {}
        for pair in pairs:
            edge = await self.get_edge(pair["src"], pair["tgt"])
            if edge is not None:
                result[(pair["src"], pair["tgt"])] = edge
        return result

    async def get_nodes_edges_batch(
        self, node_ids: list[str]
    ) -> dict[str, list[tuple[str, str]]]:
        result = {}
        for node_id in node_ids:
            result[node_id] = await self.get_node_edges(node_id) or []
        return result

    @abstractmethod
    async def upsert_node(self, node_id: str, node_data: dict[str, Any]) -> None:
        ...

    async def upsert_nodes_batch(self, nodes: list[tuple[str, dict[str, Any]]]) -> None:
        for node_id, node_data in nodes:
            await self.upsert_node(node_id, node_data)

    async def has_nodes_batch(self, node_ids: list[str]) -> set[str]:
        found = set()
        for node_id in node_ids:
            if await self.has_node(node_id):
                found.add(node_id)
        return found

    async def upsert_edges_batch(
        self, edges: list[tuple[str, str, dict[str, Any]]]
    ) -> None:
        for src, tgt, edge_data in edges:
            await self.upsert_edge(src, tgt, edge_data)

    @abstractmethod
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, Any]
    ) -> None:
        ...

    @abstractmethod
    async def delete_node(self, node_id: str) -> None:
        ...

    @abstractmethod
    async def remove_nodes(self, nodes: list[str]):
        ...

    @abstractmethod
    async def remove_edges(self, edges: list[tuple[str, str]]):
        ...

    @abstractmethod
    async def get_all_labels(self) -> list[str]:
        ...

    @abstractmethod
    async def get_knowledge_graph(
        self, node_label: str, max_depth: int = 3, max_nodes: int = 1000
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    async def get_all_nodes(self) -> list[dict]:
        ...

    @abstractmethod
    async def get_all_edges(self) -> list[dict]:
        ...

    @abstractmethod
    async def get_popular_labels(self, limit: int = 300) -> list[str]:
        ...

    @abstractmethod
    async def search_labels(self, query: str, limit: int = 50) -> list[str]:
        ...
