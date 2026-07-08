'use client';
import { useState, useRef, useCallback } from 'react';
import type { ParsedGraph } from '@/lib/graph-types';
import { loadGraphFiles, filesFromDataTransfer } from '@/lib/graph-folder';
import { WALMART_SAMPLE } from '@/lib/sample-graphs';
import { SubBar } from './SubBar';
import { GraphCanvas } from './GraphCanvas';
import { WorkbenchPanel } from './WorkbenchPanel';
import { EntityLegendStrip } from './EntityLegendStrip';
import { EmptyState } from './EmptyState';

export function VisualizerShell() {
  const [graph, setGraph] = useState<ParsedGraph | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [contextNodeIds, setContextNodeIds] = useState<string[]>([]);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [legendOpen, setLegendOpen] = useState(true);
  const [citedNodeIds, setCitedNodeIds] = useState<string[]>([]);

  // Resizable workbench panel. The shell is client-only (gated by
  // DesktopOnlyGuard), so reading localStorage in the initializer is safe.
  const PANEL_MIN = 320, PANEL_MAX = 640;
  const [panelWidth, setPanelWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return 380;
    const saved = Number(localStorage.getItem('preciso.panelWidth'));
    return saved >= PANEL_MIN && saved <= PANEL_MAX ? saved : 380;
  });
  // Tracks the live width during a drag so the mouseup handler can persist it
  // without re-subscribing listeners on every pixel.
  const dragWidth = useRef(panelWidth);

  const startResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev: MouseEvent) => {
      // Panel hugs the right edge — width grows as the cursor moves left
      const width = Math.min(PANEL_MAX, Math.max(PANEL_MIN, window.innerWidth - ev.clientX));
      dragWidth.current = width;
      setPanelWidth(width);
    };
    const onUp = () => {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem('preciso.panelWidth', String(dragWidth.current));
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  function loadGraph(g: ParsedGraph) {
    setGraph(g);
    setSelectedNodeId(null);
    setContextNodeIds([]);
    setCitedNodeIds([]);
  }

  // Drag-and-drop anywhere on the shell — accepts a lone .graphml or a whole
  // GRAPH_IS_HERE/ folder (graphml + kv stores)
  async function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    try {
      const { files, folderName } = await filesFromDataTransfer(e.dataTransfer);
      if (!files.length) return;
      loadGraph(await loadGraphFiles(files, folderName));
    } catch (err) { console.error(err); }
  }

  function handleNodeClick(nodeId: string) {
    setSelectedNodeId(nodeId);
    setContextNodeIds(prev => prev.includes(nodeId) ? prev : [...prev, nodeId]);
  }

  function handleTypeToggle(type: string) {
    setHiddenTypes(prev => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type); else next.add(type);
      return next;
    });
  }

  function handleLoadSample() {
    loadGraph(WALMART_SAMPLE);
  }

  async function handleFilesSelected(files: File[]) {
    try {
      loadGraph(await loadGraphFiles(files));
    } catch (err) { console.error(err); }
  }

  return (
    <div
      className="flex-1 flex flex-col overflow-hidden"
      style={{ background: 'var(--bg)' }}
      onDragOver={e => e.preventDefault()}
      onDrop={handleDrop}
    >
      <SubBar
        graph={graph}
        onFilesSelected={handleFilesSelected}
        workbenchOpen={workbenchOpen}
        onToggleWorkbench={() => setWorkbenchOpen(v => !v)}
      />

      <div className="flex-1 flex overflow-hidden">
        <main className="flex-1 relative overflow-hidden">
          {graph ? (
            <GraphCanvas
              graph={graph}
              selectedNodeId={selectedNodeId}
              hiddenTypes={hiddenTypes}
              citedNodeIds={citedNodeIds}
              onNodeClick={handleNodeClick}
              onDeselect={() => setSelectedNodeId(null)}
            />
          ) : (
            <EmptyState onLoadSample={handleLoadSample} onFilesSelected={handleFilesSelected} />
          )}
        </main>

        {workbenchOpen && (
          <aside
            className="relative flex flex-col border-l overflow-hidden shrink-0"
            style={{ width: panelWidth, borderColor: 'var(--border)', background: 'var(--bg)' }}
          >
            {/* Drag handle — grab the left edge to resize */}
            <div
              onMouseDown={startResize}
              className="absolute left-0 top-0 h-full w-1.5 -ml-0.5 z-10 cursor-col-resize group"
              title="Drag to resize"
            >
              <div
                className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors group-hover:w-0.5"
                style={{ background: 'color-mix(in srgb, var(--fg) 20%, transparent)' }}
              />
            </div>
            <WorkbenchPanel
              graph={graph}
              contextNodeIds={contextNodeIds}
              onRemoveContext={(id) => setContextNodeIds(prev => prev.filter(x => x !== id))}
              onClearAllContext={() => setContextNodeIds([])}
              onSetContext={(ids) => setContextNodeIds([...new Set(ids)])}
              onCitationClick={(id) => { setSelectedNodeId(id); }}
              onCitedNodesChange={setCitedNodeIds}
            />
          </aside>
        )}
      </div>

      {graph && (
        <EntityLegendStrip
          entityTypes={graph.metadata.entityTypes}
          hiddenTypes={hiddenTypes}
          onToggle={handleTypeToggle}
          open={legendOpen}
          onToggleOpen={() => setLegendOpen(v => !v)}
        />
      )}
    </div>
  );
}
