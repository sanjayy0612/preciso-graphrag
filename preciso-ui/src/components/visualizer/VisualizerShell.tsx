'use client';
import { useState } from 'react';
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
  const [workbenchOpen, setWorkbenchOpen] = useState(true);
  const [legendOpen, setLegendOpen] = useState(true);
  const [citedNodeIds, setCitedNodeIds] = useState<string[]>([]);

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
            className="w-[380px] flex flex-col border-l overflow-hidden"
            style={{ borderColor: 'var(--border)', background: 'var(--bg)' }}
          >
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
