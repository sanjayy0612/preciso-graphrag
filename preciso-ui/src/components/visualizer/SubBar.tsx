'use client';
import { useRef, useState } from 'react';
import type { ParsedGraph } from '@/lib/graph-types';
import { UsageGuide } from './UsageGuide';

interface Props {
  graph: ParsedGraph | null;
  onFilesSelected: (files: File[]) => void;
  workbenchOpen: boolean;
  onToggleWorkbench: () => void;
}

export function SubBar({ graph, onFilesSelected, workbenchOpen, onToggleWorkbench }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [guideOpen, setGuideOpen] = useState(false);

  return (
    <div
      className="h-10 px-4 border-b flex items-center gap-3 font-mono text-xs shrink-0"
      style={{ borderColor: 'var(--border)', color: 'var(--fg)' }}
    >
      {graph ? (
        <>
          <span className="text-[var(--fg)]">Graph:</span>
          <span className="font-mono text-[var(--fg)]">{graph.metadata.sourceName || 'untitled'}</span>
          <span style={{ opacity: 0.4 }}>·</span>
          <span>{graph.metadata.nodeCount} nodes</span>
          <span style={{ opacity: 0.4 }}>·</span>
          <span>{graph.metadata.edgeCount} edges</span>
          <span style={{ opacity: 0.4 }}>·</span>
          <span>{Object.keys(graph.metadata.entityTypes).length} types</span>
          {graph.metadata.chunkCount ? (
            <>
              <span style={{ opacity: 0.4 }}>·</span>
              <span>{graph.metadata.chunkCount} source chunks</span>
            </>
          ) : null}
        </>
      ) : (
        <span>No graph loaded — drop a .graphml file or your GRAPH_IS_HERE folder</span>
      )}

      <div className="flex-1" />

      <input
        ref={fileRef}
        type="file"
        accept=".graphml,.xml,.json"
        multiple
        className="hidden"
        onChange={(e) => {
          const files = [...(e.target.files ?? [])];
          if (files.length) onFilesSelected(files);
          e.target.value = '';
        }}
      />
      <button
        onClick={() => fileRef.current?.click()}
        className="px-2 py-1 rounded hover:bg-[var(--surface)] transition-colors text-[var(--fg)]"
      >
        ⇪ Load graph
      </button>
      <button
        onClick={onToggleWorkbench}
        className="px-2 py-1 rounded hover:bg-[var(--surface)] transition-colors"
      >
        {workbenchOpen ? '⇄ Hide panel' : '⇄ Show panel'}
      </button>
      <button
        onClick={() => setGuideOpen(true)}
        className="px-2 py-1 rounded hover:bg-[var(--surface)] transition-colors"
        title="How this works"
      >
        ? How this works
      </button>

      <UsageGuide open={guideOpen} onClose={() => setGuideOpen(false)} />
    </div>
  );
}
