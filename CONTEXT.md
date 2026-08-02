# Preciso GraphRAG

Preciso turns a corpus of source documents into a local, evidence-linked knowledge graph. This glossary defines the document lifecycle language used by users, agents, and maintainers.

## Language

**Source Document**:
A user-provided document whose facts may contribute entities, relationships, and evidence to the graph.
_Avoid_: Data file, input blob

**Extraction**:
The reviewed structured representation of one source document, containing its entities, relationships, chunks, and evidence links.
_Avoid_: Parsed graph, generated graph

**Document ID**:
The stable, unique identity of a source document across an identical recovery replay.
_Avoid_: Version ID, chunk prefix

**Additive Ingestion**:
The operation that adds an extraction's contributions to the existing graph without removing earlier contributions.
_Avoid_: Graph replacement, document update

**Recovery Replay**:
An identical repeat of a previously attempted ingestion after an operational failure.
_Avoid_: Replacement, correction, update

**Graph Artifacts**:
The complete generated representation of the current graph, including graph structure, evidence stores, embeddings, summaries, and metadata.
_Avoid_: Source corpus, extraction backup

**Complete Valid Corpus**:
Every current source document and reviewed extraction that should be represented in the graph, excluding flawed or superseded versions.
_Avoid_: Latest document, changed files

**Full Rebuild**:
Creation of new graph artifacts from an empty graph using the complete valid corpus.
_Avoid_: Reingestion, replacement, partial rebuild
