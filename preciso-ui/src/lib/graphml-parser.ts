import { DOMParser } from '@xmldom/xmldom';
import type { ParsedGraph, GraphNode, GraphEdge, EntityType } from './graph-types';

// Preciso joins merged values with the GRAPH_FIELD_SEP token — clean for display
const SEP = '<SEP>';

function cleanText(s?: string): string | undefined {
  if (!s) return undefined;
  const parts = [...new Set(s.split(SEP).map(p => p.trim()).filter(Boolean))];
  return parts.join(' ');
}

function splitIds(s?: string): string[] | undefined {
  if (!s) return undefined;
  const ids = s.split(SEP).map(p => p.trim()).filter(Boolean);
  return ids.length ? ids : undefined;
}

export function parseGraphML(xmlText: string, sourceName?: string): ParsedGraph {
  const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
  const keys = doc.getElementsByTagName('key');
  const keyMap: Record<string, string> = {};
  for (let i = 0; i < keys.length; i++) {
    const k = keys[i];
    const id = k.getAttribute('id');
    const name = k.getAttribute('attr.name') || id;
    if (id && name) keyMap[id] = name;
  }
  const nodes: GraphNode[] = [];
  const nodeEls = doc.getElementsByTagName('node');
  for (let i = 0; i < nodeEls.length; i++) {
    const n = nodeEls[i];
    const id = n.getAttribute('id')!;
    const props: Record<string, string> = {};
    const dataEls = n.getElementsByTagName('data');
    for (let j = 0; j < dataEls.length; j++) {
      const d = dataEls[j];
      const k = keyMap[d.getAttribute('key') || ''] || d.getAttribute('key') || '';
      props[k] = d.textContent || '';
    }
    nodes.push({
      id,
      label: props.entity_name || props.label || props.name || id,
      type: (props.entity_type || props.type || 'CONCEPT') as EntityType,
      description: cleanText(props.description || props.desc),
      sourceId: props.source_id || props.chunk_id,
      chunkIds: splitIds(props.source_id || props.chunk_id),
      degree: 0,
    });
  }
  const edges: GraphEdge[] = [];
  const edgeEls = doc.getElementsByTagName('edge');
  for (let i = 0; i < edgeEls.length; i++) {
    const e = edgeEls[i];
    const source = e.getAttribute('source')!;
    const target = e.getAttribute('target')!;
    const props: Record<string, string> = {};
    const dataEls = e.getElementsByTagName('data');
    for (let j = 0; j < dataEls.length; j++) {
      const d = dataEls[j];
      const k = keyMap[d.getAttribute('key') || ''] || d.getAttribute('key') || '';
      props[k] = d.textContent || '';
    }
    edges.push({
      source, target,
      label: cleanText(props.keywords || props.relation || props.label),
      weight: parseFloat(props.weight) || 0.5,
      description: cleanText(props.description),
      chunkIds: splitIds(props.source_id || props.chunk_id),
    });
  }
  const degreeMap: Record<string, number> = {};
  edges.forEach((e) => {
    const s = typeof e.source === 'string' ? e.source : (e.source as GraphNode).id;
    const t = typeof e.target === 'string' ? e.target : (e.target as GraphNode).id;
    degreeMap[s] = (degreeMap[s] || 0) + 1;
    degreeMap[t] = (degreeMap[t] || 0) + 1;
  });
  nodes.forEach((n) => { n.degree = degreeMap[n.id] || 1; });
  const entityTypes: Record<string, number> = {};
  nodes.forEach((n) => { entityTypes[n.type] = (entityTypes[n.type] || 0) + 1; });
  return { nodes, edges, metadata: { nodeCount: nodes.length, edgeCount: edges.length, entityTypes, sourceName } };
}

