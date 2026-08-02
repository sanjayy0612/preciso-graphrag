# Getting Started in 5 Minutes

## What you need

- **Python 3.11+**
- **Ollama running locally** (for embeddings) — [install here](https://ollama.ai)
- **Claude Code, Codex, or GitHub Copilot** (or any supported agent)

## Step 1: Clone and install

```bash
git clone https://github.com/yourusername/preciso-graphrag
cd preciso-graphrag
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Step 2: Drop a document

Place source material into `to_be_extracted/`.

Recommended inputs for the best graph quality:
- Markdown (`.md`)
- Plain text (`.txt`)
- READMEs, technical docs, notes, wiki exports, and other text-first material

Discouraged in the default workflow:
- PDF files

Preciso does not include a built-in PDF parser or OCR layer. For the most reliable graph creation, use `.md` and `.txt` inputs. PDF success depends on the external agent's native document-reading capabilities or on user-side conversion before the file is dropped into `to_be_extracted/`.

For example:
```bash
cp your_document.txt to_be_extracted/
```

## Important: verify your source documents before extraction

Before running the agent prompt, review the source documents yourself. Confirm that they are factually correct, current, complete, and the exact versions you want represented in the graph. The agent can validate extraction structure, but it cannot guarantee that the source document itself is true.

Starting extraction with flawed data can waste paid agent or language-model credits. Ingestion also generates chunk, entity, and relationship embeddings, which may consume paid embedding-provider credits. If the mistake reaches the graph, correcting it may require regenerating the extraction and rebuilding every graph artifact from the complete valid corpus.

Before starting extraction:

1. Remove drafts, duplicates, and superseded versions.
2. Confirm that every remaining document is accurate, final, and belongs in the corpus.
3. Preserve the validated source documents as reproducible inputs.
4. Run the agent prompt only after you are satisfied with the source corpus.

The agent's later extraction validation is a second structural check; it does not replace your review of the original data.

If flawed source data has already entered the graph, follow the [full correction workflow](faq.md#how-do-i-correct-an-already-ingested-document).

## Step 3: Run this prompt in your agent

Open your coding agent (Claude Code, Copilot, etc.) in this repo root and paste:

```text
Call get_server_status().
If overall is ready, proceed.
If overall is degraded, explain what is degraded, what still works, and ask whether to proceed or fix first.
Read the files in to_be_extracted/.
Choose the most appropriate extraction skill from the skills folder for each file.
Extract entities, relationships, and chunks into extractions/{source_name}_extracted.json.
Validate that every source_id maps to a real chunk_id and that all relationships reference defined entities.
If you find duplicate entities, orphaned relationships, or conflicts, use the reconciliation skill before ingestion.
Before calling any ingestion tool, summarize the source files, document IDs,
entity/relationship/chunk counts, validation results, and unresolved concerns.
Explain that ingestion is additive and ask me to confirm that the source documents
and extractions are correct, current, and ready to persist.
Only after I confirm, call ingest_from_file for each generated extraction file.
Then confirm the graph artifacts written to GRAPH_IS_HERE/ and summarize what was ingested.
```

The agent will:
1. Extract structured knowledge (entities, relationships) from your document.
2. Validate the extraction and present a pre-ingestion summary.
3. Wait for your confirmation.
4. Ingest it into a local knowledge graph.
5. Confirm success.

## Step 4: Query your graph

Once ingestion is done, test a query. Open a terminal and run:

```bash
python3 test/query_manual.py "What are the company's risk factors?" mix
```

Expected output: A structured response with entities, relationships, and evidence chunks from your document.

Example (Walmart filing):
```
Entities:
  - walmart_inc (company)
  - supply_chain_risk (risk_factor)

Relationships:
  - walmart_inc EXPOSED_TO supply_chain_risk

Context:
  "Walmart's supply chain is exposed to various risks including disruptions..."
```

## What just happened

You've built a knowledge graph from unstructured text. Instead of retrieving similar chunks (like regular RAG), the system extracted entities and relationships and stored them as a queryable graph. When you ask a question, it traverses the graph to find connected answers — so multi-hop reasoning works: "Who is responsible for managing supply chain risks?" → finds PERSON MANAGES RISK_FACTOR connections and returns executive names.

This is GraphRAG: reasoning across connections, not just searching for similar text.

## Optional: Export to Neo4j or Qdrant

These exports are optional. Skip this section unless you need to share your graph with a team or sync it into an existing Neo4j or Qdrant instance.

### Prerequisites

Install the extra packages if you plan to export:

```bash
pip install neo4j qdrant-client
```

### Configure `.env`

Create a `.env` file at the repo root. Do not commit it.

```bash
# Neo4j export (optional)
GRAPHRAG_NEO4J_URI=bolt://localhost:7687
GRAPHRAG_NEO4J_USERNAME=neo4j
GRAPHRAG_NEO4J_PASSWORD=your-password
GRAPHRAG_NEO4J_DATABASE=neo4j

# Qdrant export (optional)
GRAPHRAG_QDRANT_URL=http://localhost:6333
GRAPHRAG_QDRANT_API_KEY=
GRAPHRAG_QDRANT_COLLECTION_PREFIX=preciso

# Optional shared workspace label for exports
GRAPHRAG_EXPORT_WORKSPACE=default
```

### Export flow

After your graph is built in `GRAPH_IS_HERE/`, call the export tool manually:

```text
Call get_server_status() to confirm the local graph exists.
Then call export_graph_to_neo4j with your Neo4j connection settings.
Or call export_vectors_to_qdrant with your Qdrant connection settings.
```

Remember: these are snapshots. After additive ingestion or a full rebuild, export again when you want Neo4j or Qdrant refreshed.

---

**Next steps:**
- Read [skills-guide.md](skills-guide.md) to learn how to customize extraction for your domain.
- Check [eval-guide.md](eval-guide.md) to validate your graph quality.
- See [faq.md](faq.md) for troubleshooting.
