'use client';
import { useRef } from 'react';
import { SolarSystem } from './SolarSystem';

interface Props {
  onLoadSample: () => void;
  onFilesSelected: (files: File[]) => void;
}

export function EmptyState({ onLoadSample, onFilesSelected }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);

  return (
    <div className="absolute inset-0 flex items-center justify-center flex-col gap-6">
      <SolarSystem size={260} />
      <div className="text-center max-w-md">
        <div className="font-mono text-xs text-[var(--muted)] uppercase tracking-widest mb-3">
          No graph loaded
        </div>
        <p className="text-sm text-[var(--fg)] mb-6 leading-relaxed">
          Drop a <span className="font-mono bg-[var(--surface)] px-1.5 py-0.5 rounded">.graphml</span> file
          — or your whole <span className="font-mono bg-[var(--surface)] px-1.5 py-0.5 rounded">GRAPH_IS_HERE</span> folder
          — anywhere to start exploring.
        </p>
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
          className="px-4 py-2 bg-[var(--stripe)] text-[var(--stripe-text)] text-sm font-mono hover:bg-[var(--red-bright)] transition-colors"
        >
          Load your graph →
        </button>
        <div className="mt-3">
          <button
            onClick={onLoadSample}
            className="text-xs font-mono text-[var(--muted)] hover:text-[var(--fg)] transition-colors underline underline-offset-4"
          >
            or load the Walmart sample
          </button>
        </div>
      </div>
    </div>
  );
}
