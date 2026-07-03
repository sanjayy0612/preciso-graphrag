export type EntityType =
  | 'COMPANY' | 'PERSON' | 'FINANCIAL_METRIC' | 'SEGMENT' | 'PRODUCT'
  | 'RISK_FACTOR' | 'GEOGRAPHIC_REGION' | 'REGULATORY_BODY' | 'EVENT'
  | 'CONCEPT' | 'ORGANIZATION' | string;

export interface GraphNode {
  id: string;
  label: string;
  type: EntityType;
  description?: string;
  sourceId?: string;
  /** Chunk ids this entity was extracted from (source_id / kv_store_entity_chunks) */
  chunkIds?: string[];
  degree: number;
  _jitterSeed?: number;
  // d3-force
  x?: number; y?: number; vx?: number; vy?: number;
  fx?: number | null; fy?: number | null;
}

export interface GraphEdge {
  source: string | GraphNode;
  target: string | GraphNode;
  label?: string;
  weight: number;
  description?: string;
  chunkIds?: string[];
}

export interface TextChunk {
  id: string;
  content: string;
  docId?: string;
}

export interface ParsedGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Source text chunks from kv_store_text_chunks.json, when a GRAPH_IS_HERE folder is loaded */
  chunks?: Record<string, TextChunk>;
  metadata: {
    nodeCount: number;
    edgeCount: number;
    chunkCount?: number;
    entityTypes: Record<string, number>;
    sourceName?: string;
  };
}

export type RetrievalMode = 'local' | 'global' | 'mix';

export interface QueryRun {
  id: string;
  timestamp: number;
  prompt: string;
  contextNodeIds: string[];
  response: string;
  citedNodeIds: string[];
  /** Maps numeric citation refs ("1", "2", …) back to node ids for this run */
  refToNodeId: Record<string, string>;
  mode: RetrievalMode;
  provider: 'openai' | 'cohere';
  model: string;
}
