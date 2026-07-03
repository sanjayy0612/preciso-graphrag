import type { ParsedGraph, TextChunk } from './graph-types';
import { parseGraphML } from './graphml-parser';

// Loads a Preciso graph from a set of files — either a lone .graphml or a
// whole GRAPH_IS_HERE/ folder. Recognized files:
//   *.graphml                     → the graph itself (required)
//   kv_store_text_chunks.json     → chunk_id → source text
//   kv_store_entity_chunks.json   → entity_id → chunk_ids
//   kv_store_relation_chunks.json → "src<SEP>tgt" → chunk_ids
// vdb_*.json vector stores are skipped: their vectors live in the embedding
// space of the model used at ingest (often local Ollama), which the browser
// cannot embed queries into — retrieval re-embeds via OpenAI/Cohere instead.

const SEP = '<SEP>';

async function readJson(file: File): Promise<Record<string, unknown> | null> {
  try {
    return JSON.parse(await file.text());
  } catch {
    console.warn(`Skipping unparseable JSON: ${file.name}`);
    return null;
  }
}

export async function loadGraphFiles(files: File[], folderName?: string): Promise<ParsedGraph> {
  const byName = (name: string) => files.find(f => f.name === name);
  const graphml = files.find(f => f.name === 'graph_graph.graphml')
    || files.find(f => /\.(graphml|xml)$/i.test(f.name));
  if (!graphml) throw new Error('No .graphml file found — a Preciso graph folder must contain one.');

  const graph = parseGraphML(await graphml.text(), folderName || graphml.name.replace(/\.graphml$/i, ''));

  // Source text chunks
  const chunksFile = byName('kv_store_text_chunks.json');
  if (chunksFile) {
    const raw = await readJson(chunksFile);
    if (raw) {
      const chunks: Record<string, TextChunk> = {};
      for (const [id, v] of Object.entries(raw)) {
        const rec = v as { content?: string; full_doc_id?: string };
        if (rec?.content) chunks[id] = { id, content: rec.content, docId: rec.full_doc_id };
      }
      if (Object.keys(chunks).length) {
        graph.chunks = chunks;
        graph.metadata.chunkCount = Object.keys(chunks).length;
      }
    }
  }

  // Entity → chunks mapping (more complete than the graphml's source_id)
  const entityChunksFile = byName('kv_store_entity_chunks.json');
  if (entityChunksFile) {
    const raw = await readJson(entityChunksFile);
    if (raw) {
      const nodeMap = new Map(graph.nodes.map(n => [n.id, n]));
      for (const [entityId, v] of Object.entries(raw)) {
        const ids = (v as { chunk_ids?: string[] })?.chunk_ids;
        const node = nodeMap.get(entityId);
        if (node && ids?.length) node.chunkIds = [...new Set([...(node.chunkIds ?? []), ...ids])];
      }
    }
  }

  // Relation → chunks mapping, keyed "src<SEP>tgt" (order not guaranteed)
  const relChunksFile = byName('kv_store_relation_chunks.json');
  if (relChunksFile) {
    const raw = await readJson(relChunksFile);
    if (raw) {
      const byPair = new Map<string, string[]>();
      for (const [key, v] of Object.entries(raw)) {
        const ids = (v as { chunk_ids?: string[] })?.chunk_ids;
        if (ids?.length) byPair.set(key, ids);
      }
      for (const e of graph.edges) {
        const s = typeof e.source === 'string' ? e.source : e.source.id;
        const t = typeof e.target === 'string' ? e.target : e.target.id;
        const ids = byPair.get(`${s}${SEP}${t}`) ?? byPair.get(`${t}${SEP}${s}`);
        if (ids) e.chunkIds = [...new Set([...(e.chunkIds ?? []), ...ids])];
      }
    }
  }

  return graph;
}

// Expands a drag-and-drop payload into a flat file list, descending into
// dropped directories (e.g. the whole GRAPH_IS_HERE/ folder).
export async function filesFromDataTransfer(dt: DataTransfer): Promise<{ files: File[]; folderName?: string }> {
  const entries = [...dt.items]
    .map(item => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter((e): e is FileSystemEntry => !!e);

  // Fallback for browsers without the entries API
  if (!entries.length) return { files: [...dt.files] };

  const files: File[] = [];
  let folderName: string | undefined;

  async function walk(entry: FileSystemEntry): Promise<void> {
    if (entry.name.startsWith('.') || entry.name.endsWith('.bak')) return;
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) =>
        (entry as FileSystemFileEntry).file(resolve, reject));
      files.push(file);
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      // readEntries returns results in batches — drain until empty
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
          reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await walk(child);
      }
    }
  }

  for (const entry of entries) {
    if (entry.isDirectory && !folderName) folderName = entry.name;
    await walk(entry);
  }
  return { files, folderName };
}
