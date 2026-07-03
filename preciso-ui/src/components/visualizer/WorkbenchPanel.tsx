'use client';
import { useState, useEffect, useRef } from 'react';
import type { ParsedGraph, QueryRun, RetrievalMode } from '@/lib/graph-types';
import { streamOpenAI, streamCohere, SYSTEM_PROMPT } from '@/lib/llm-providers';
import { retrieve, buildContext, endpointId } from '@/lib/retrieval';

interface Props {
  graph: ParsedGraph | null;
  contextNodeIds: string[];
  onRemoveContext: (id: string) => void;
  onClearAllContext: () => void;
  onSetContext: (ids: string[]) => void;
  onCitationClick: (id: string) => void;
  onCitedNodesChange: (ids: string[]) => void;
}

const LLM_MODELS = {
  openai: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini'],
  cohere: ['command-r', 'command-r-plus'],
};

const EMBED_MODELS = {
  openai: ['text-embedding-3-small', 'text-embedding-3-large', 'text-embedding-ada-002'],
  cohere: ['embed-english-v3.0', 'embed-multilingual-v3.0', 'embed-english-light-v3.0'],
};

const MODE_HINTS: Record<RetrievalMode, string> = {
  local: 'Entity match → 1-hop neighborhood.',
  global: 'Relationship-level match.',
  mix: 'Entities + relationships, merged.',
};

function parseCitations(text: string, refMap: Record<string, string>): string[] {
  const matches = text.match(/\[(\d+)\]/g) || [];
  return [...new Set(matches.map(m => refMap[m.slice(1, -1)]).filter(Boolean))];
}

function renderWithCitations(text: string, refMap: Record<string, string>, onCite: (id: string) => void) {
  return text.split(/(\[\d+\])/g).map((part, i) => {
    const match = part.match(/^\[(\d+)\]$/);
    const nodeId = match ? refMap[match[1]] : undefined;
    if (nodeId) {
      return (
        <button key={i} onClick={() => onCite(nodeId)}
          className="inline-flex items-center px-1 py-0.5 mx-0.5 text-[10px] font-mono border"
          style={{ color: 'var(--stripe)', borderColor: 'var(--stripe)', background: 'color-mix(in srgb, var(--stripe) 8%, transparent)' }}>
          {part}
        </button>
      );
    }
    return <span key={i}>{part}</span>;
  });
}

const SectionHeader = ({ title, count, countKey }: { title: string; count?: number; countKey?: number }) => (
  <div
    className="px-4 py-2 border-b font-mono text-xs uppercase tracking-widest flex items-center justify-between"
    style={{
      color: 'var(--fg)',
      borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))',
      background: 'color-mix(in srgb, var(--fg) 5%, var(--bg))',
    }}
  >
    <span className="font-bold">{title}</span>
    {count !== undefined && (
      <span key={countKey ?? count} className="count-pulse tabular-nums" style={{ color: 'var(--muted)' }}>
        {count}
      </span>
    )}
  </div>
);

// Solid black-based divider — the main visual separator between sections
const Divider = () => (
  <div className="border-t" style={{ borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }} />
);

// Lighter divider for within a section (e.g. between embedding rows)
const InnerDivider = () => (
  <div className="border-t mx-4" style={{ borderColor: 'color-mix(in srgb, var(--fg) 10%, var(--bg))' }} />
);

export function WorkbenchPanel({
  graph, contextNodeIds, onRemoveContext, onClearAllContext,
  onSetContext, onCitationClick, onCitedNodesChange,
}: Props) {
  // LLM config
  const [provider, setProvider] = useState<'openai' | 'cohere'>('openai');
  const [model, setModel] = useState('gpt-4o-mini');
  const [apiKey, setApiKey] = useState('');
  const [editingKey, setEditingKey] = useState(false);

  // Retrieval config
  const [mode, setMode] = useState<RetrievalMode>('mix');

  // Embedding config
  const [embedEnabled, setEmbedEnabled] = useState(false);
  const [embedProvider, setEmbedProvider] = useState<'openai' | 'cohere'>('openai');
  const [embedModel, setEmbedModel] = useState('text-embedding-3-small');
  const [embedKey, setEmbedKey] = useState('');
  const [editingEmbedKey, setEditingEmbedKey] = useState(false);
  const [embedStatus, setEmbedStatus] = useState('');

  // Embedding caches — keyed by node/edge identity + provider + model
  const nodeEmbCache = useRef<Map<string, number[]>>(new Map());
  const edgeEmbCache = useRef<Map<string, number[]>>(new Map());
  // Ids that were auto-added by retrieval on the last run (vs clicked by the user)
  const autoIdsRef = useRef<string[]>([]);

  // Query state
  const [prompt, setPrompt] = useState('');
  const [response, setResponse] = useState('');
  const [refMap, setRefMap] = useState<Record<string, string>>({});
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const [history, setHistory] = useState<QueryRun[]>([]);

  // Load keys from localStorage
  useEffect(() => {
    setApiKey(localStorage.getItem(`preciso.${provider}_key`) || '');
    setModel(LLM_MODELS[provider][0]);
  }, [provider]);

  useEffect(() => {
    setEmbedKey(localStorage.getItem(`preciso.embed_${embedProvider}_key`) || '');
    setEmbedModel(EMBED_MODELS[embedProvider][0]);
  }, [embedProvider]);

  // Clear embedding caches when a new graph is loaded
  useEffect(() => {
    nodeEmbCache.current.clear();
    edgeEmbCache.current.clear();
    autoIdsRef.current = [];
  }, [graph]);

  // Reset query output tied to the previous graph (render-adjustment pattern)
  const [lastGraph, setLastGraph] = useState<ParsedGraph | null>(graph);
  if (graph !== lastGraph) {
    setLastGraph(graph);
    setRefMap({});
    setResponse('');
  }

  const contextNodes = graph ? graph.nodes.filter(n => contextNodeIds.includes(n.id)) : [];

  async function runQuery() {
    if (!graph || !apiKey || streaming) return;
    if (!prompt.trim()) { setError('Enter a prompt to run a query.'); return; }
    if (embedEnabled && !embedKey) {
      setError('Embeddings are on but no embed key is set — add one or switch embeddings off.');
      return;
    }

    setStreaming(true);
    setResponse('');
    setError('');

    // Nodes the user clicked themselves survive across runs; last run's
    // auto-retrieved nodes are replaced by this run's retrieval.
    const manualIds = contextNodeIds.filter(id => !autoIdsRef.current.includes(id));

    let result;
    try {
      setEmbedStatus('Retrieving…');
      result = await retrieve({
        graph,
        query: prompt,
        mode,
        embed: embedEnabled && embedKey ? {
          provider: embedProvider, model: embedModel, apiKey: embedKey,
          nodeCache: nodeEmbCache.current, edgeCache: edgeEmbCache.current,
        } : undefined,
        onStatus: setEmbedStatus,
      });
      setEmbedStatus('');
    } catch (err: unknown) {
      setEmbedStatus('');
      setError(`Retrieval failed: ${err instanceof Error ? err.message : 'unknown error'}`);
      setStreaming(false);
      return;
    }

    // Merge manual selections (plus their incident edges) with retrieval
    const manualSet = new Set(manualIds);
    const manualEdges = manualSet.size
      ? graph.edges.filter(e => manualSet.has(endpointId(e.source)) || manualSet.has(endpointId(e.target)))
      : [];
    const allNodeIds = [
      ...manualIds,
      ...result.nodeIds,
      ...manualEdges.flatMap(e => [endpointId(e.source), endpointId(e.target)]),
    ];
    // Manually pinned nodes contribute their source chunks too
    const manualChunks = graph.chunks
      ? manualIds.flatMap(id => graph.nodes.find(n => n.id === id)?.chunkIds ?? [])
          .map(cid => graph.chunks![cid]).filter(Boolean)
      : [];
    const chunks = [...new Map([...manualChunks, ...result.chunks].map(c => [c.id, c])).values()];
    const ctx = buildContext(graph, allNodeIds, [...result.edges, ...manualEdges], chunks);
    setRefMap(ctx.refToNodeId);

    autoIdsRef.current = result.seedNodeIds.filter(id => !manualSet.has(id));
    onSetContext([...manualIds, ...autoIdsRef.current]);

    const userMsg = `${ctx.text}\n\nQUESTION: ${prompt}`;
    let fullResponse = '';

    try {
      const stream = provider === 'openai'
        ? streamOpenAI({ apiKey, model, system: SYSTEM_PROMPT, user: userMsg })
        : streamCohere({ apiKey, model, system: SYSTEM_PROMPT, user: userMsg });

      for await (const chunk of stream) {
        fullResponse += chunk;
        setResponse(fullResponse);
      }

      const cited = parseCitations(fullResponse, ctx.refToNodeId);
      onCitedNodesChange(cited);

      setHistory(prev => [{
        id: crypto.randomUUID(),
        timestamp: Date.now(),
        prompt,
        contextNodeIds: [...manualIds, ...autoIdsRef.current],
        response: fullResponse,
        citedNodeIds: cited,
        refToNodeId: ctx.refToNodeId,
        mode,
        provider,
        model,
      }, ...prev].slice(0, 10));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Query failed');
    } finally {
      setStreaming(false);
    }
  }

  const canRun = !streaming && !!apiKey && !!graph;

  return (
    <div className="flex-1 flex flex-col overflow-y-auto text-sm" style={{ color: 'var(--fg)' }}>

      {/* ── RUN CONFIG ─────────────────────────────────────── */}
      <SectionHeader title="Run Config" />
      <div className="px-4 py-3 space-y-2 font-mono text-xs">
        <Row label="Provider">
          <select value={provider} onChange={e => setProvider(e.target.value as 'openai' | 'cohere')}
            className="flex-1 px-2 py-1 border text-xs font-mono"
            style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
            <option value="openai">OpenAI</option>
            <option value="cohere">Cohere</option>
          </select>
        </Row>
        <Row label="Model">
          <select value={model} onChange={e => setModel(e.target.value)}
            className="flex-1 px-2 py-1 border text-xs font-mono"
            style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
            {LLM_MODELS[provider].map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </Row>
        <Row label="API key">
          {editingKey ? (
            <input type="password" value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              onBlur={() => { setEditingKey(false); localStorage.setItem(`preciso.${provider}_key`, apiKey); }}
              autoFocus
              className="flex-1 px-2 py-1 border text-xs font-mono"
              style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'var(--stripe)' }}
              placeholder={provider === 'openai' ? 'sk-…' : 'your-cohere-key'} />
          ) : (
            <span style={{ color: apiKey ? 'var(--fg)' : 'var(--muted)' }}>
              {apiKey ? '•'.repeat(Math.min(12, apiKey.length)) : 'Not set'}
              <button onClick={() => setEditingKey(true)} className="ml-2 opacity-50 hover:opacity-100"> ✎</button>
            </span>
          )}
        </Row>
        <p style={{ color: 'var(--muted)', opacity: 0.55, fontSize: 10, marginTop: 2 }}>
          Key stays in browser — never sent to Preciso servers.
        </p>
      </div>

      <InnerDivider />

      {/* Retrieval mode — mirrors Preciso's local / global / mix query modes */}
      <div className="px-4 py-2.5 space-y-2 font-mono text-xs">
        <Row label="Retrieval">
          <div className="flex-1 flex border" style={{ borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
            {(['local', 'global', 'mix'] as RetrievalMode[]).map(m => (
              <button key={m} onClick={() => setMode(m)}
                className="flex-1 px-2 py-1 text-xs font-mono transition-colors"
                style={{
                  background: mode === m ? 'var(--fg)' : 'transparent',
                  color: mode === m ? 'var(--bg)' : 'var(--muted)',
                }}>
                {m}
              </button>
            ))}
          </div>
        </Row>
        <p style={{ color: 'var(--muted)', opacity: 0.55, fontSize: 10 }}>
          {MODE_HINTS[mode]} {embedEnabled ? 'Semantic (embeddings).' : 'Keyword match — enable embeddings for semantic.'}
        </p>
      </div>

      <InnerDivider />

      {/* Embedding sub-section */}
      <div className="px-4 py-2.5 space-y-2 font-mono text-xs">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setEmbedEnabled(v => !v)}
            className="flex items-center gap-2 text-xs font-mono transition-colors"
            style={{ color: embedEnabled ? 'var(--fg)' : 'var(--muted)' }}
          >
            <span
              className="w-7 h-3.5 border flex items-center transition-colors"
              style={{
                borderColor: embedEnabled ? 'var(--fg)' : 'color-mix(in srgb, var(--fg) 25%, var(--bg))',
                background: embedEnabled ? 'color-mix(in srgb, var(--fg) 12%, var(--bg))' : 'transparent',
              }}
            >
              <span
                className="w-2.5 h-2.5 border transition-all"
                style={{
                  borderColor: 'var(--fg)',
                  background: 'var(--fg)',
                  marginLeft: embedEnabled ? 'auto' : '1px',
                  marginRight: embedEnabled ? '1px' : 'auto',
                  opacity: embedEnabled ? 1 : 0.3,
                }}
              />
            </span>
            Embeddings {embedEnabled ? '— semantic retrieval' : '(off — keyword retrieval)'}
          </button>
        </div>

        {embedEnabled && (
          <div className="space-y-2 pt-1">
            <Row label="Embed via">
              <select value={embedProvider} onChange={e => setEmbedProvider(e.target.value as 'openai' | 'cohere')}
                className="flex-1 px-2 py-1 border text-xs font-mono"
                style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
                <option value="openai">OpenAI</option>
                <option value="cohere">Cohere</option>
              </select>
            </Row>
            <Row label="Model">
              <select value={embedModel} onChange={e => setEmbedModel(e.target.value)}
                className="flex-1 px-2 py-1 border text-xs font-mono"
                style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
                {EMBED_MODELS[embedProvider].map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </Row>
            <Row label="Embed key">
              {editingEmbedKey ? (
                <input type="password" value={embedKey}
                  onChange={e => setEmbedKey(e.target.value)}
                  onBlur={() => { setEditingEmbedKey(false); localStorage.setItem(`preciso.embed_${embedProvider}_key`, embedKey); }}
                  autoFocus
                  className="flex-1 px-2 py-1 border text-xs font-mono"
                  style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'var(--stripe)' }}
                  placeholder={embedProvider === 'openai' ? 'sk-…' : 'your-cohere-key'} />
              ) : (
                <span style={{ color: embedKey ? 'var(--fg)' : 'var(--muted)' }}>
                  {embedKey ? '•'.repeat(Math.min(12, embedKey.length)) : 'Not set'}
                  <button onClick={() => setEditingEmbedKey(true)} className="ml-2 opacity-50 hover:opacity-100"> ✎</button>
                </span>
              )}
            </Row>
            <p style={{ color: 'var(--muted)', opacity: 0.55, fontSize: 10 }}>
              On query: entities & relationships ranked by cosine similarity, cached per graph.
            </p>
          </div>
        )}
      </div>

      <Divider />

      {/* ── CONTEXT ────────────────────────────────────────── */}
      <SectionHeader title="Context" count={contextNodeIds.length} countKey={contextNodeIds.length} />
      <div className="px-4 py-3">
        {contextNodes.length === 0 ? (
          <p className="text-xs font-mono" style={{ color: 'var(--muted)' }}>
            Click a node to pin context — or just run a query, retrieval picks the rest.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              {contextNodes.map(n => (
                <div key={n.id}
                  className="chip-appear inline-flex items-center gap-1.5 px-2.5 py-1 border text-xs font-mono"
                  style={{ borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))', background: 'var(--bg)', color: 'var(--fg)' }}>
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: 'var(--fg)', opacity: 0.4 }} />
                  <span className="truncate max-w-[140px]">{n.label}</span>
                  <button onClick={() => onRemoveContext(n.id)} className="ml-1 opacity-40 hover:opacity-100 transition-opacity">×</button>
                </div>
              ))}
            </div>
            {contextNodes.length >= 3 && (
              <button onClick={() => { autoIdsRef.current = []; onClearAllContext(); }}
                className="mt-2 text-xs font-mono hover:text-[var(--fg)] transition-colors"
                style={{ color: 'var(--muted)' }}>
                Clear all
              </button>
            )}
          </>
        )}
      </div>

      <Divider />

      {/* ── PROMPT ─────────────────────────────────────────── */}
      <SectionHeader title="Prompt" />
      <div className="px-4 py-3 flex flex-col gap-2">
        <textarea
          value={prompt}
          onChange={e => { setPrompt(e.target.value); if (error) setError(''); }}
          onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) runQuery(); }}
          rows={4}
          placeholder="Ask a question about this graph…"
          className="w-full px-3 py-2 text-xs font-mono border resize-none focus:outline-none"
          style={{ background: 'var(--bg)', color: 'var(--fg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}
        />
        <div className="flex items-center gap-3">
          <button
            onClick={runQuery}
            disabled={!canRun}
            className="px-4 py-2 text-xs font-mono transition-colors disabled:opacity-40"
            style={{ background: 'var(--stripe)', color: 'var(--stripe-text)' }}
          >
            {embedStatus || (streaming ? 'Running…' : 'Run query → ⌘↵')}
          </button>
          {!apiKey && <p className="text-xs font-mono" style={{ color: 'var(--muted)' }}>Set API key above to run</p>}
          {!graph && <p className="text-xs font-mono" style={{ color: 'var(--muted)' }}>Load a graph first</p>}
        </div>
        {error && <p className="text-xs font-mono" style={{ color: 'var(--red-bright)' }}>{error}</p>}
      </div>

      <Divider />

      {/* ── RESPONSE ───────────────────────────────────────── */}
      <SectionHeader title="Response" />
      <div className="px-4 py-3 font-mono text-xs leading-relaxed min-h-[80px]">
        {response ? (
          <div className="space-y-2">
            <p style={{ color: 'var(--fg)' }}>{renderWithCitations(response, refMap, onCitationClick)}</p>
            <button onClick={() => navigator.clipboard.writeText(response)}
              className="px-2 py-1 border text-xs font-mono hover:bg-[var(--surface)] mt-2"
              style={{ borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}>
              ⟲ Copy
            </button>
          </div>
        ) : (
          <p style={{ color: 'var(--muted)' }}>
            {!graph ? 'Load a graph first.' : !apiKey ? 'Set API key and run a query.' : 'Run a query — retrieval builds the context.'}
          </p>
        )}
      </div>

      <Divider />

      {/* ── HISTORY ────────────────────────────────────────── */}
      <SectionHeader title="History" count={history.length} />
      <div className="px-4 py-3 space-y-1 font-mono text-xs">
        {history.length === 0 ? (
          <p style={{ color: 'var(--muted)' }}>No queries yet this session</p>
        ) : (
          <>
            {history.map(item => (
              <button key={item.id} onClick={() => { setPrompt(item.prompt); setResponse(item.response); setRefMap(item.refToNodeId); onCitedNodesChange(item.citedNodeIds); }}
                className="w-full text-left py-1.5 flex items-start gap-2 hover:bg-[var(--surface)] px-2 -mx-2 transition-colors">
                <span style={{ color: 'var(--muted)' }}>·</span>
                <span className="flex-1 truncate" style={{ color: 'var(--fg)' }}>{item.prompt}</span>
                <span style={{ color: 'var(--muted)', opacity: 0.55, fontSize: 10, flexShrink: 0 }}>
                  {item.mode} · {Math.round((Date.now() - item.timestamp) / 60000)}m ago
                </span>
              </button>
            ))}
            <button onClick={() => setHistory([])} className="mt-2 text-xs font-mono hover:text-[var(--fg)] transition-colors" style={{ color: 'var(--muted)' }}>
              Clear history
            </button>
          </>
        )}
      </div>
    </div>
  );
}

// Small layout helper to keep label+control rows DRY
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span style={{ color: 'var(--muted)', minWidth: 72 }}>{label}</span>
      <div className="flex-1 flex items-center">{children}</div>
    </div>
  );
}
