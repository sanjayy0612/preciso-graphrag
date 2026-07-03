import type { ParsedGraph, GraphNode, GraphEdge, RetrievalMode, TextChunk } from './graph-types';
import { embedBatch, cosineSimilarity } from './llm-providers';

export type { RetrievalMode };

export interface EmbedConfig {
  provider: 'openai' | 'cohere';
  model: string;
  apiKey: string;
  nodeCache: Map<string, number[]>;
  edgeCache: Map<string, number[]>;
}

export interface RetrievalResult {
  /** Directly matched nodes — shown as context chips */
  seedNodeIds: string[];
  /** Full entity set for the LLM context (seeds + neighborhood / edge endpoints) */
  nodeIds: string[];
  edges: GraphEdge[];
  /** Source text excerpts tied to the retrieved entities/relations (needs a GRAPH_IS_HERE folder) */
  chunks: TextChunk[];
  method: 'semantic' | 'lexical';
}

const TOP_K_NODES = 8;
const TOP_K_EDGES = 12;
const TOP_K_CHUNKS = 5;
const MAX_CHUNK_CHARS = 800;
const SEMANTIC_MIN_SCORE = 0.15;
const MAX_CONTEXT_NODES = 40;
const MAX_CONTEXT_EDGES = 60;
const MAX_DESC_CHARS = 300;
const EMBED_BATCH_SIZE = 96;

export function endpointId(v: string | GraphNode): string {
  return typeof v === 'string' ? v : v.id;
}

function nodeDoc(n: GraphNode): string {
  return `${n.label} (${n.type}): ${n.description || ''}`;
}

function edgeDoc(e: GraphEdge, nodeMap: Map<string, GraphNode>): string {
  const s = nodeMap.get(endpointId(e.source));
  const t = nodeMap.get(endpointId(e.target));
  return `${s?.label ?? endpointId(e.source)} ${e.label || 'related to'} ${t?.label ?? endpointId(e.target)}: ${e.description || ''}`;
}

function edgeKey(e: GraphEdge): string {
  return `${endpointId(e.source)}→${endpointId(e.target)}→${e.label || ''}`;
}

// ── Lexical scoring (fallback when embeddings are off) ──────────────────────

const STOPWORDS = new Set([
  'the', 'a', 'an', 'and', 'or', 'of', 'in', 'on', 'for', 'to', 'is', 'are',
  'was', 'were', 'what', 'which', 'who', 'how', 'why', 'when', 'does', 'did',
  'do', 'with', 'about', 'tell', 'me', 'show', 'list', 'their', 'its', 'it',
  'this', 'that', 'between', 'from', 'by', 'as', 'at', 'be', 'has', 'have', 'had',
]);

function tokenize(text: string): string[] {
  return [...new Set(
    text.toLowerCase().split(/[^a-z0-9$%.]+/).filter(t => t.length > 1 && !STOPWORDS.has(t)),
  )];
}

function lexicalScore(queryTokens: string[], doc: string): number {
  if (!queryTokens.length) return 0;
  const d = doc.toLowerCase();
  let hits = 0;
  for (const t of queryTokens) if (d.includes(t)) hits++;
  return hits / queryTokens.length;
}

// ── Semantic scoring ─────────────────────────────────────────────────────────

async function ensureEmbedded(
  docs: string[],
  cacheKeys: string[],
  cache: Map<string, number[]>,
  cfg: EmbedConfig,
  kind: string,
  onStatus?: (msg: string) => void,
): Promise<void> {
  const missing = docs.map((_, i) => i).filter(i => !cache.has(cacheKeys[i]));
  if (!missing.length) return;
  onStatus?.(`Embedding ${missing.length} ${kind}…`);
  for (let off = 0; off < missing.length; off += EMBED_BATCH_SIZE) {
    const batch = missing.slice(off, off + EMBED_BATCH_SIZE);
    const vecs = await embedBatch(batch.map(i => docs[i]), cfg.provider, cfg.model, cfg.apiKey, 'search_document');
    batch.forEach((i, j) => cache.set(cacheKeys[i], vecs[j]));
  }
}

// ── Retrieval — mirrors Preciso's core/query.py modes ────────────────────────
//
//   local  → entity-level match, expanded to the 1-hop neighborhood
//   global → relationship-level match (edges scored directly)
//   mix    → both, merged
export async function retrieve(opts: {
  graph: ParsedGraph;
  query: string;
  mode: RetrievalMode;
  embed?: EmbedConfig;
  onStatus?: (msg: string) => void;
}): Promise<RetrievalResult> {
  const { graph, query, mode, embed, onStatus } = opts;
  const nodeMap = new Map(graph.nodes.map(n => [n.id, n]));

  let queryVec: number[] | null = null;
  if (embed) {
    onStatus?.('Embedding query…');
    [queryVec] = await embedBatch([query], embed.provider, embed.model, embed.apiKey, 'search_query');
  }
  const queryTokens = tokenize(query);

  async function scoreDocs(
    docs: string[], cacheKeys: string[], cache: Map<string, number[]> | undefined, kind: string,
  ): Promise<number[]> {
    if (embed && queryVec && cache) {
      await ensureEmbedded(docs, cacheKeys, cache, embed, kind, onStatus);
      return cacheKeys.map(k => cosineSimilarity(queryVec!, cache.get(k) ?? []));
    }
    return docs.map(d => lexicalScore(queryTokens, d));
  }

  const keep = (score: number) => (embed ? score >= SEMANTIC_MIN_SCORE : score > 0);
  const modelTag = embed ? `${embed.provider}:::${embed.model}` : 'lexical';

  // Entity-level retrieval (local & mix)
  let seedNodes: GraphNode[] = [];
  if (mode !== 'global') {
    const docs = graph.nodes.map(nodeDoc);
    const keys = graph.nodes.map(n => `n:${n.id}:::${modelTag}`);
    const scores = await scoreDocs(docs, keys, embed?.nodeCache, 'entities');
    seedNodes = graph.nodes
      .map((n, i) => ({ n, s: scores[i] }))
      .filter(x => keep(x.s))
      .sort((a, b) => b.s - a.s)
      .slice(0, TOP_K_NODES)
      .map(x => x.n);
  }

  // Relationship-level retrieval (global & mix)
  let seedEdges: GraphEdge[] = [];
  if (mode !== 'local') {
    const docs = graph.edges.map(e => edgeDoc(e, nodeMap));
    const keys = graph.edges.map(e => `e:${edgeKey(e)}:::${modelTag}`);
    const scores = await scoreDocs(docs, keys, embed?.edgeCache, 'relationships');
    seedEdges = graph.edges
      .map((e, i) => ({ e, s: scores[i] }))
      .filter(x => keep(x.s))
      .sort((a, b) => b.s - a.s)
      .slice(0, TOP_K_EDGES)
      .map(x => x.e);
  }

  // Assemble: seed entities first, then edge endpoints, then 1-hop neighbors
  const nodeIds: string[] = [];
  const seen = new Set<string>();
  const push = (id: string) => { if (!seen.has(id) && nodeMap.has(id)) { seen.add(id); nodeIds.push(id); } };

  seedNodes.forEach(n => push(n.id));
  seedEdges.forEach(e => { push(endpointId(e.source)); push(endpointId(e.target)); });

  const edges = new Map<string, GraphEdge>(seedEdges.map(e => [edgeKey(e), e]));
  if (mode !== 'global') {
    const seedIds = new Set(seedNodes.map(n => n.id));
    for (const e of graph.edges) {
      if (edges.size >= MAX_CONTEXT_EDGES) break;
      const s = endpointId(e.source), t = endpointId(e.target);
      if (seedIds.has(s) || seedIds.has(t)) {
        edges.set(edgeKey(e), e);
        push(s); push(t);
      }
    }
  }

  const seedNodeIds = mode === 'global'
    ? [...new Set(seedEdges.flatMap(e => [endpointId(e.source), endpointId(e.target)]))]
    : seedNodes.map(n => n.id);

  return {
    seedNodeIds,
    nodeIds: nodeIds.slice(0, MAX_CONTEXT_NODES),
    edges: [...edges.values()],
    chunks: pickChunks(graph, query, queryTokens, seedNodes, seedEdges, mode),
    method: embed ? 'semantic' : 'lexical',
  };
}

// Source-text retrieval, mirroring how the backend attaches text chunks to
// retrieved entities/relations (and, in mix mode, also scans chunks directly).
// Chunks referenced by more seeds rank higher; lexical match breaks ties.
function pickChunks(
  graph: ParsedGraph,
  query: string,
  queryTokens: string[],
  seedNodes: GraphNode[],
  seedEdges: GraphEdge[],
  mode: RetrievalMode,
): TextChunk[] {
  if (!graph.chunks) return [];

  const refCount = new Map<string, number>();
  const bump = (ids?: string[]) => ids?.forEach(id => {
    if (graph.chunks![id]) refCount.set(id, (refCount.get(id) ?? 0) + 1);
  });
  seedNodes.forEach(n => bump(n.chunkIds));
  seedEdges.forEach(e => bump(e.chunkIds));

  // Mix mode also considers chunks that match the query text directly
  if (mode === 'mix') {
    for (const chunk of Object.values(graph.chunks)) {
      if (!refCount.has(chunk.id) && lexicalScore(queryTokens, chunk.content) > 0) {
        refCount.set(chunk.id, 0);
      }
    }
  }

  return [...refCount.entries()]
    .map(([id, refs]) => ({ chunk: graph.chunks![id], refs, lex: lexicalScore(queryTokens, graph.chunks![id].content) }))
    .sort((a, b) => (b.refs - a.refs) || (b.lex - a.lex))
    .slice(0, TOP_K_CHUNKS)
    .map(x => x.chunk);
}

// ── Context assembly ─────────────────────────────────────────────────────────
//
// Entities get stable numeric refs ([1], [2], …) so the LLM can cite them
// regardless of what the underlying GraphML node ids look like.
export interface GraphContext {
  text: string;
  refToNodeId: Record<string, string>;
}

export function buildContext(graph: ParsedGraph, nodeIds: string[], edges?: GraphEdge[], chunks?: TextChunk[]): GraphContext {
  const nodeMap = new Map(graph.nodes.map(n => [n.id, n]));
  let ids = [...new Set(nodeIds.filter(id => nodeMap.has(id)))];
  let edgePool = edges ?? graph.edges;

  // Nothing matched and nothing selected — fall back to the graph's hubs
  if (!ids.length) {
    ids = [...graph.nodes].sort((a, b) => b.degree - a.degree).slice(0, 30).map(n => n.id);
    edgePool = graph.edges;
  }
  ids = ids.slice(0, MAX_CONTEXT_NODES);

  const included = new Set(ids);
  const seenEdges = new Set<string>();
  const edgeList = edgePool.filter(e => {
    const k = edgeKey(e);
    if (seenEdges.has(k)) return false;
    seenEdges.add(k);
    return included.has(endpointId(e.source)) && included.has(endpointId(e.target));
  }).slice(0, MAX_CONTEXT_EDGES);

  const refOf = new Map<string, string>();
  ids.forEach((id, i) => refOf.set(id, String(i + 1)));

  const trunc = (s?: string) => !s ? '—' : s.length > MAX_DESC_CHARS ? `${s.slice(0, MAX_DESC_CHARS)}…` : s;

  const entityLines = ids.map(id => {
    const n = nodeMap.get(id)!;
    return `[${refOf.get(id)}] ${n.label} (${n.type}): ${trunc(n.description)}`;
  });
  const edgeLines = edgeList.map(e => {
    const base = `[${refOf.get(endpointId(e.source))}] --${e.label || 'RELATED'}--> [${refOf.get(endpointId(e.target))}]`;
    return e.description ? `${base} — ${trunc(e.description)}` : base;
  });

  const refToNodeId: Record<string, string> = {};
  refOf.forEach((ref, id) => { refToNodeId[ref] = id; });

  let text = `ENTITIES:\n${entityLines.join('\n')}\n\nRELATIONSHIPS:\n${edgeLines.join('\n') || '—'}`;
  if (chunks?.length) {
    const chunkLines = chunks.map(c => {
      const body = c.content.length > MAX_CHUNK_CHARS ? `${c.content.slice(0, MAX_CHUNK_CHARS)}…` : c.content;
      return `- (${c.id}${c.docId ? ` · ${c.docId}` : ''}) ${body}`;
    });
    text += `\n\nSOURCE TEXT:\n${chunkLines.join('\n')}`;
  }

  return { text, refToNodeId };
}
