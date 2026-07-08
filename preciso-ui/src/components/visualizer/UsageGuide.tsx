'use client';
import { useEffect } from 'react';
import { SectionHeader, Divider } from './WorkbenchPanel';

interface Props {
  open: boolean;
  onClose: () => void;
}

// Three ways to read the same graph. Written for people who already know
// GraphRAG — no hand-holding, just what each mode does and its trade-off.
const SECTIONS: { title: string; body: string[] }[] = [
  {
    title: 'Structure View',
    body: [
      'Drop just the .graphml file. Free — no key needed.',
      'Best for checking shape in real time: orphaned nodes, isolated islands, how entities connect — without querying anything.',
    ],
  },
  {
    title: 'Query This Graph',
    body: [
      'Drop the whole GRAPH_IS_HERE folder, then set your OpenAI or Cohere key in the panel.',
      'Retrieval defaults to lexical entity matching — free.',
      "Turning on Embeddings computes a fresh semantic index over this graph's entities and relationships, live in your browser, cached for the session. It's an independent index, not a reuse of whatever you embedded at ingest — if you already paid to embed once, this is a second cost.",
      "Works with any OpenAI/Cohere key even if you ingested with Ollama, since it isn't reusing those vectors — a provider mismatch won't break it, it just means you're paying twice.",
    ],
  },
  {
    title: 'Query via Your Own Agent',
    body: [
      'Open a fresh Claude Code (or other MCP-capable) session and call query_graph_tool against the same GRAPH_IS_HERE folder.',
      "This is the only path that queries against the exact vectors computed at ingest — required if you embedded locally with Ollama, since the browser can't reach a local model.",
    ],
  },
];

export function UsageGuide({ open, onClose }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-6"
      style={{ background: 'color-mix(in srgb, black 60%, transparent)' }}
      onClick={onClose}
    >
      <div
        onClick={e => e.stopPropagation()}
        className="w-full max-w-lg max-h-[80vh] overflow-y-auto border flex flex-col"
        style={{ background: 'var(--bg)', borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}
      >
        <div
          className="px-4 py-3 flex items-center justify-between border-b shrink-0"
          style={{ borderColor: 'color-mix(in srgb, var(--fg) 25%, var(--bg))' }}
        >
          <span className="font-mono text-xs uppercase tracking-widest font-bold" style={{ color: 'var(--fg)' }}>
            How This Works
          </span>
          <button
            onClick={onClose}
            className="font-mono text-xs opacity-50 hover:opacity-100 transition-opacity"
            style={{ color: 'var(--fg)' }}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <p className="px-4 pt-3 text-xs font-mono" style={{ color: 'var(--muted)' }}>
          Three ways to read the same graph — pick the one that fits what you&apos;re doing.
        </p>

        {SECTIONS.map((section, i) => (
          <div key={section.title}>
            <div className={i === 0 ? 'mt-3' : ''}>
              <SectionHeader title={section.title} />
            </div>
            <div className="px-4 py-3 space-y-2 font-mono text-xs leading-relaxed">
              {section.body.map((line, j) => (
                <p key={j} style={{ color: j === 0 ? 'var(--fg)' : 'var(--muted)' }}>{line}</p>
              ))}
            </div>
            {i < SECTIONS.length - 1 && <Divider />}
          </div>
        ))}
      </div>
    </div>
  );
}
