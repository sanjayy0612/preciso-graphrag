# FAQ: Common Problems and Fixes

## Does Preciso parse PDFs?

No. Preciso does not include a built-in PDF parser or OCR layer.

For the best graph quality, use `.md` and `.txt` inputs in `to_be_extracted/`. Those formats give the agent cleaner structure, more predictable chunking, and a more reliable extraction path.

PDFs are discouraged in the default workflow. They may still work with agents that have strong native PDF understanding, but that behavior comes from the external agent, not from Preciso itself.

## What does "local graph artifacts remain the source of truth" mean?

It means `GRAPH_IS_HERE/` is the real copy of your graph.

If you export to Neo4j or Qdrant, those are downstream copies for sharing or scale. When you re-ingest locally, the local graph updates first. The export targets do not update by themselves; you re-export manually when you want them refreshed.

Think of it like this:
- `GRAPH_IS_HERE/` = the original file
- Neo4j/Qdrant = exported copies

If the original changes, the copies stay stale until you export again.

## Do I need Neo4j or Qdrant to use Preciso?

No. Preciso works without them. The local graph in `GRAPH_IS_HERE/` is enough for ingesting and querying. Neo4j and Qdrant are optional if you want shared access, production deployment, or a separate search backend.

## How do I know if my export is out of date?

Check `GRAPH_IS_HERE/artifact_manifest.json` for the latest ingestion metadata. If you re-ingested after the last export, your Neo4j or Qdrant copy is stale and should be refreshed.

## MCP server not starting

### Symptom
```
Error: Cannot find module 'mcp' or command 'mcp_launcher.sh' not found
```

### Fix
1. Ensure `.venv` is activated:
   ```bash
   source .venv/bin/activate
   ```
2. Reinstall dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the server manually:
   ```bash
   python3 -m preciso_mcp.server
   ```

If it still fails, check that `scripts/mcp_launcher.sh` is executable:
```bash
chmod +x scripts/mcp_launcher.sh
```

---

## Embeddings showing as fallback

### Symptom
Running `get_server_status()` returns:
```json
{
  "embedding": {
    "mode": "fallback",
    "provider": "fallback",
    "status": "degraded"
  }
}
```

### Fix
Ollama is not running or not reachable. Ollama provides the `mxbai-embed-large` model locally.

1. **Install Ollama** (if not already installed):
   - macOS: `brew install ollama` or download from [ollama.ai](https://ollama.ai)
   - Linux: `curl https://ollama.ai/install.sh | sh`
   - Windows: Download installer from [ollama.ai](https://ollama.ai)

2. **Start Ollama** in a separate terminal:
   ```bash
   ollama serve
   ```

3. **Pull the embedding model** (in another terminal):
   ```bash
   ollama pull mxbai-embed-large
   ```

4. **Restart your MCP server** and run `get_server_status()` again.

If you still see fallback, verify Ollama is listening on port 11434:
```bash
curl http://localhost:11434/api/tags
```

---

## Graph is empty after ingestion

### Symptom
Ingestion completes with "success" but `GRAPH_IS_HERE/` files are empty or contain no entities.

### Checklist

1. **Verify the extraction JSON is valid:**
   ```bash
   python3 -c "import json; json.load(open('extractions/your_file.json'))"
   ```

2. **Check that entities exist in the extraction:**
   ```bash
   grep -c '"entity_name"' extractions/your_file.json
   ```
   Should return > 0.

3. **Verify source_id mapping:**
   Each entity's `source_id` must map to a real `chunk_id` in the extraction.
   ```bash
   python3 -c "
   import json
   data = json.load(open('extractions/your_file.json'))
   chunks = {c['chunk_id'] for c in data.get('chunks', [])}
   for e in data.get('entities', []):
       if e['source_id'] not in chunks:
           print(f'Missing: {e[\"entity_name\"]} → {e[\"source_id\"]}')
   "
   ```

4. **Check ingestion logs:**
   ```bash
   tail -f preciso_mcp/server.log  # if logging is enabled
   ```

5. **Manually re-ingest:**
   ```bash
   python3 test/ingest_manual.py extractions/your_file.json
   ```

6. **Inspect the graph:**
   ```bash
   python3 test/query_manual.py "test query" local
   ```

---

## YoY questions returning wrong year

### Symptom
You ask "How did revenue change from 2022 to 2023?" and the system returns metrics from the same year or mixes them up.

### Why
By default, `hybrid` mode searches for entities matching the query keywords. If both 2022 and 2023 revenue entities have similar names (e.g., `revenue_2022` and `revenue_2023`), the vector search might pick the wrong one or miss the COMPARED_TO relationship.

### Fix
Use **global mode** instead:
```bash
python3 test/query_manual.py "How did revenue change from 2022 to 2023?" global
```

Or in an agent prompt:
```python
query_graph_tool("How did revenue change from 2022 to 2023?", mode="global")
```

**Global mode** searches for relationships (like COMPARED_TO) first, which ensures the system finds paired metrics. It's slower but more accurate for temporal comparisons.

Alternatively, ensure your extraction defines COMPARED_TO edges explicitly:
```json
{
  "src_id": "revenue_2022",
  "tgt_id": "revenue_2023",
  "type": "COMPARED_TO",
  "description": "Year-over-year comparison"
}
```

---

## Precision is low on queries

### Symptom
Query returns many spurious entities or chunks that aren't relevant to the question.

### Common causes and fixes

**1. top_k is too high**
By default, the system retrieves top-10 entities. Lower it:
```python
query_graph_tool(query, mode="mix", top_k=5)
```

**2. Chunks have poor quality**
If your extraction includes very long or generic chunks, try:
- Break long documents into smaller chunks (max 256 tokens each).
- Improve chunk text — remove boilerplate, focus on content.
- Re-extract and re-ingest.

**3. Embedding vector quality**
Embeddings are only as good as the model. The default `mxbai-embed-large` is competent but not perfect. If you have higher quality embeddings:
- Use a better embedding model (e.g., `paraphrase-multilingual-mpnet-base-v2` via Hugging Face).
- Configure in `config.py`.

**4. Entities are poorly named**
If you extracted entity `metric` instead of `walmart_total_revenue_2023`, the system can't distinguish it from other metrics. See [skills-guide.md](skills-guide.md) for the entity registry rule.

### Try this
```bash
# Lower top_k and use local mode (faster + more focused)
python3 test/query_manual.py "your query" local --top_k 3
```

---

## Queries time out

### Symptom
```
Timeout waiting for query result (30s)
```

### Causes and fixes

**1. Global mode on large graphs**
Global mode traverses relationships, which is slower on large graphs (>100k edges).
- Use `hybrid` or `local` mode instead.
- If you need global, reduce `top_k` to 5.

**2. Ollama embedding is slow**
If Ollama is CPU-bound, it can be slow to embed queries.
- Verify Ollama is not competing with other processes.
- Check Ollama status: `curl http://localhost:11434/api/tags`
- If slow, consider a lighter embedding model or GPU acceleration.

**3. Graph is very large**
If you've ingested hundreds of documents:
- Export to Neo4j or Qdrant for better performance.
- Or, ingest smaller document batches separately and query selectively.

---

## Agent says "no results found"

### Symptom
Query returns:
```json
{
  "status": "failure",
  "message": "Query returned empty dataset"
}
```

### Why
The entities and relationships retrieved don't match the query keywords, *and* no text chunks matched. This usually means:
1. Query uses terminology not in the graph.
2. Entity names in extraction don't match document content.
3. The graph is truly empty (see "Graph is empty after ingestion" above).

### Fix
1. **Try a simpler query:**
   ```bash
   python3 test/query_manual.py "company name" local
   ```

2. **Check what entities exist:**
   ```python
   import json
   manifest = json.load(open('GRAPH_IS_HERE/artifact_manifest.json'))
   print(f"Entities: {manifest['entity_count']}")
   print(f"Relationships: {manifest['relationship_count']}")
   ```

3. **Inspect a sample query:**
   ```bash
   python3 test/query_manual.py "one of the extracted entity names here" local
   ```

4. **Verify the extraction quality:**
   Look at `extractions/your_file.json` — do entity names match the source document? If the agent extracted `COMPANY_XYZ` but the filing says "Company XYZ Inc.", add a normalization rule in your skill.

---

## Hallucinations detected during evaluation

### Symptom
Evaluation report shows hallucinations:
```json
{
  "hallucinations": ["entity_not_in_graph", "wrong_number"]
}
```

### Fix
Hallucinations usually come from LLM summarization (if enabled). The LLM may invent details not in the retrieved context.

**Disable LLM and return raw context:**
```python
query_graph_tool(query, mode="mix")
# Returns raw_data with entities, relationships, chunks — no LLM synthesis
```

**Or, improve the evaluation scoring:**
In `evals/`, ensure the gold answer is *exactly* what's in the retrived chunks. If the chunk says "$100 million" but the gold answer says "$100M", the evaluation will flag it as a hallucination.

---

## "Dimension mismatch" error in vector store

### Symptom
```
Error: vdb_chunks.json.dim-mismatch-TIMESTAMP.bak
```

This happens if you change embedding models (e.g., switch from 768-dim to 1024-dim) without clearing the old vectors.

### Fix

1. **Clear the old vector artifacts:**
   ```bash
   rm GRAPH_IS_HERE/vdb_*.json*
   ```

2. **Clear the graph (optional, if you want fresh):**
   ```bash
   rm GRAPH_IS_HERE/graph_graph.graphml
   ```

3. **Reingest your extractions:**
   ```bash
   python3 test/ingest_manual.py extractions/your_file.json
   ```

Alternatively, before switching embedding models, commit `GRAPH_IS_HERE/` to git and keep a branch. You can always revert:
```bash
git checkout HEAD -- GRAPH_IS_HERE/
```

---

**Still stuck?**

1. Check the MCP server logs for more details.
2. Verify `get_server_status()` returns `"overall": "ready"`.
3. Try the Walmart sample evaluation to confirm your setup works.
4. Review [architecture.md](architecture.md) to understand data flow.
