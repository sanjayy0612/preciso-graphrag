from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from typing import Any, final

import networkx as nx

from core.storage.base import BaseGraphStorage
from core.storage.shared_storage import (
    get_namespace_lock,
    get_update_flag,
    set_all_update_flags,
)
from core.utils import logger


@final
@dataclass
class NetworkXStorage(BaseGraphStorage):
    @staticmethod
    def load_nx_graph(file_name) -> nx.Graph | None:
        if os.path.exists(file_name):
            return nx.read_graphml(file_name)
        return None

    @staticmethod
    def write_nx_graph(graph: nx.Graph, file_name: str, workspace: str = "_"):
        logger.info(
            "[%s] Writing graph with %s nodes, %s edges",
            workspace,
            graph.number_of_nodes(),
            graph.number_of_edges(),
        )
        nx.write_graphml(graph, file_name)

    def __post_init__(self):
        working_dir = self.global_config["working_dir"]
        self._working_dir = working_dir
        workspace_dir = os.path.join(working_dir, self.workspace) if self.workspace else working_dir
        self.workspace = self.workspace or ""
        os.makedirs(workspace_dir, exist_ok=True)
        self._graphml_xml_file = os.path.join(workspace_dir, f"graph_{self.namespace}.graphml")
        self._storage_lock = None
        self.storage_updated = None
        self._graph = self.load_nx_graph(self._graphml_xml_file) or nx.Graph()

    async def initialize(self):
        self.storage_updated = await get_update_flag(
            self.namespace, workspace=self.workspace, working_dir=self._working_dir
        )
        self._storage_lock = get_namespace_lock(
            self.namespace, workspace=self.workspace, working_dir=self._working_dir
        )

    async def _get_graph(self):
        async with self._storage_lock:
            if self.storage_updated.value:
                self._graph = self.load_nx_graph(self._graphml_xml_file) or nx.Graph()
                self.storage_updated.value = False
            return self._graph

    async def has_node(self, node_id: str) -> bool:
        return (await self._get_graph()).has_node(node_id)

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        return (await self._get_graph()).has_edge(source_node_id, target_node_id)

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        return (await self._get_graph()).nodes.get(node_id)

    async def node_degree(self, node_id: str) -> int:
        graph = await self._get_graph()
        return graph.degree(node_id) if graph.has_node(node_id) else 0

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        graph = await self._get_graph()
        return (
            (graph.degree(src_id) if graph.has_node(src_id) else 0)
            + (graph.degree(tgt_id) if graph.has_node(tgt_id) else 0)
        )

    async def get_edge(self, source_node_id: str, target_node_id: str) -> dict[str, Any] | None:
        return (await self._get_graph()).edges.get((source_node_id, target_node_id))

    async def get_node_edges(self, source_node_id: str) -> list[tuple[str, str]] | None:
        graph = await self._get_graph()
        if graph.has_node(source_node_id):
            return list(graph.edges(source_node_id))
        return None

    async def upsert_node(self, node_id: str, node_data: dict[str, Any]) -> None:
        (await self._get_graph()).add_node(node_id, **node_data)

    async def upsert_edge(self, source_node_id: str, target_node_id: str, edge_data: dict[str, Any]) -> None:
        (await self._get_graph()).add_edge(source_node_id, target_node_id, **edge_data)

    async def upsert_nodes_batch(self, nodes: list[tuple[str, dict[str, Any]]]) -> None:
        graph = await self._get_graph()
        for node_id, node_data in nodes:
            graph.add_node(node_id, **node_data)

    async def has_nodes_batch(self, node_ids: list[str]) -> set[str]:
        graph = await self._get_graph()
        return {node_id for node_id in node_ids if graph.has_node(node_id)}

    async def upsert_edges_batch(self, edges: list[tuple[str, str, dict[str, Any]]]) -> None:
        graph = await self._get_graph()
        for src, tgt, edge_data in edges:
            graph.add_edge(src, tgt, **edge_data)

    async def delete_node(self, node_id: str) -> None:
        graph = await self._get_graph()
        if graph.has_node(node_id):
            graph.remove_node(node_id)

    async def remove_nodes(self, nodes: list[str]):
        graph = await self._get_graph()
        for node in nodes:
            if graph.has_node(node):
                graph.remove_node(node)

    async def remove_edges(self, edges: list[tuple[str, str]]):
        graph = await self._get_graph()
        for source, target in edges:
            if graph.has_edge(source, target):
                graph.remove_edge(source, target)

    async def get_all_labels(self) -> list[str]:
        graph = await self._get_graph()
        return sorted(str(node) for node in graph.nodes())

    async def get_popular_labels(self, limit: int = 300) -> list[str]:
        graph = await self._get_graph()
        degrees = dict(graph.degree())
        return [str(node) for node, _ in sorted(degrees.items(), key=lambda item: item[1], reverse=True)[:limit]]

    async def search_labels(self, query: str, limit: int = 50) -> list[str]:
        graph = await self._get_graph()
        query_lower = query.lower().strip()
        if not query_lower:
            return []
        matches = []
        for node in graph.nodes():
            node_str = str(node)
            node_lower = node_str.lower()
            if query_lower not in node_lower:
                continue
            if node_lower == query_lower:
                score = 1000
            elif node_lower.startswith(query_lower):
                score = 500
            else:
                score = 100 - len(node_str)
                if f" {query_lower}" in node_lower or f"_{query_lower}" in node_lower:
                    score += 50
            matches.append((node_str, score))
        matches.sort(key=lambda item: (-item[1], item[0]))
        return [item[0] for item in matches[:limit]]

    async def get_knowledge_graph(self, node_label: str, max_depth: int = 3, max_nodes: int = 1000) -> dict[str, Any]:
        graph = await self._get_graph()
        max_nodes = min(max_nodes, self.global_config.get("max_graph_nodes", 1000))
        if node_label == "*":
            selected_nodes = list(graph.nodes())[:max_nodes]
        else:
            selected_nodes = [node for node in graph.nodes() if node_label.lower() in str(node).lower()]
            if not selected_nodes:
                return {"nodes": [], "edges": [], "is_truncated": False}
            bfs_nodes = []
            seen = set()
            queue = deque((node, 0) for node in selected_nodes[:1])
            while queue and len(bfs_nodes) < max_nodes:
                node, depth = queue.popleft()
                if node in seen or depth > max_depth:
                    continue
                seen.add(node)
                bfs_nodes.append(node)
                for neighbor in graph.neighbors(node):
                    queue.append((neighbor, depth + 1))
            selected_nodes = bfs_nodes
        node_set = set(selected_nodes)
        edges = []
        for src, tgt, data in graph.edges(data=True):
            if src in node_set and tgt in node_set:
                edges.append({"source": src, "target": tgt, **dict(data)})
        return {
            "nodes": [{"id": node, **dict(graph.nodes[node])} for node in selected_nodes],
            "edges": edges,
            "is_truncated": len(selected_nodes) >= max_nodes,
        }

    async def get_all_nodes(self) -> list[dict]:
        graph = await self._get_graph()
        return [{"id": node, **dict(data)} for node, data in graph.nodes(data=True)]

    async def get_all_edges(self) -> list[dict]:
        graph = await self._get_graph()
        return [{"source": src, "target": tgt, **dict(data)} for src, tgt, data in graph.edges(data=True)]

    async def index_done_callback(self) -> None:
        async with self._storage_lock:
            self.write_nx_graph(self._graph, self._graphml_xml_file, self.workspace)
            await set_all_update_flags(
                self.namespace, workspace=self.workspace, working_dir=self._working_dir
            )
            self.storage_updated.value = False

    async def drop(self) -> dict[str, str]:
        try:
            graph = await self._get_graph()
            graph.clear()
            await self.index_done_callback()
            return {"status": "success", "message": "data dropped"}
        except Exception as exc:
            logger.exception("NetworkXStorage.drop failed (namespace=%s)", self.namespace)
            return {"status": "error", "message": str(exc)}
