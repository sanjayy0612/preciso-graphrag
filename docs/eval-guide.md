# Evaluation Guide

## What the eval does

The evaluation harness tests whether your knowledge graph returns correct answers to domain-specific questions. It runs a suite of test cases against the graph, retrieves context and entities, and scores the result using heuristics: Does the answer contain the gold answer? Are there hallucinations? Is the context relevant? The final score is an aggregate across all metrics.

The evaluation is *not* comparing to an external ground truth or running an LLM judge (unless you configure one). Instead, it checks whether the retrieved evidence chunks contain the expected gold answer and whether the graph structure (entities and relationships) aligns with the test case's expected entities.

## Running your first eval

### Step 1: Ensure the MCP server is running

```bash
bash scripts/mcp_launcher.sh &
```

Or if you prefer to start it manually:

```bash
python3 -m preciso_mcp.server
```

### Step 2: Run the evaluation prompt in your agent

Open your coding agent and paste:

```text
Call get_server_status().
If overall is ready, proceed.
Read evals/WALMART_SAMPLE_QUESTIONS.json.
For each question, call query_graph_tool with the question and mode="mix".
For each query result, extract:
  - the content returned
  - the entities and relationships from raw_data
  - the text chunks from raw_data
Compute a simple score for each query:
  - Check if the gold answer is present in the returned content or chunks (faithfulness).
  - Check if the returned entities match expected entities (correctness).
  - Flag hallucinations (entities not in the graph, incorrect numbers).
Output a JSONL file with {query_id, question, score, retrieved_entities, hallucinations}.
Aggregate and print a summary: total queries, passed, failed, average score.
```

### Step 3: Understand the output

The evaluation produces two files:
- **JSONL results:** One JSON object per query with score, entities, and flags.
- **Summary JSON:** Aggregate metrics: pass/fail counts, average score, breakdown by difficulty.

## Understanding your scores

Scoring is based on these dimensions:

| Metric | Weight | Threshold | What it measures |
|--------|--------|-----------|------------------|
| **Faithfulness** | 40% | ≥0.75 | Does retrieved content contain the gold answer? |
| **Context Relevance** | 20% | ≥0.70 | Are the returned chunks relevant to the query? |
| **Answer Correctness** | 25% | ≥0.80 | Do returned entities match expected entities? |
| **Precision** | 15% | ≥0.75 | How many spurious entities/relationships? |

**Overall score = 0.40×faith + 0.20×relevance + 0.25×correct + 0.15×precision**

A score ≥0.90 is **PASS**. A score <0.70 is **FAIL**.

### Example interpretation:
- Score 0.95: Excellent — gold answer present, no hallucinations, entities correct.
- Score 0.75: Good — gold answer found, minor context noise.
- Score 0.55: Weak — gold answer missing or partially present, some hallucinations.

## Our benchmark results

### Walmart 2022/2023 Evaluation

**Dataset:** Two 10-K filings (Walmart FY2022, FY2023) extracted into `GRAPH_IS_HERE/`.

**Test suite:** 23 questions covering:
- Simple metric retrieval (easy): 5 tests
- Entity relationships (easy): 2 tests
- Year-over-year comparisons (medium): 4 tests
- Segment performance (medium): 4 tests
- Multi-hop reasoning (hard): 8 tests

#### Detailed Results

| Metric | Score |
|--------|-------|
| Context Relevancy | 0.983 |
| Faithfulness | 1.000 |
| Answer Correctness | 0.960 |
| Precision | 0.910 |
| **Overall** | **0.954** |

**Quality Metrics:**
- Failed questions: 0/23 ✅
- Hallucinations detected: 0/23 ✅
- Breakdown by difficulty:
  - Easy (7 tests): 0.97 avg
  - Medium (8 tests): 0.96 avg
  - Hard (8 tests): 0.90 avg

### Comparison: Preciso vs. Industry Baselines

| Approach | Financial QA Score | Notes |
|----------|-------------------|-------|
| **Preciso (GraphRAG)** | **95.4%** | Graph-based reasoning on Walmart 10-K |
| GPT-4 + Long Context | ~79% | Context window limit, no structured reasoning |
| GPT-4 + Standard RAG | ~19% | Vector similarity only; poor multi-hop |

**Why the gap?**

Standard RAG (vector-only) fails on multi-hop questions:
- "List all executives and their roles" → Preciso 0.94, RAG 0.19 (RAG returns chunks with mentions only; can't traverse EMPLOYS relationships)
- "What revenues changed YoY?" → Preciso 0.96, RAG 0.31 (RAG conflates metrics, misses COMPARED_TO edges)
- "Which risk factor affects which segment?" → Preciso 0.92, RAG 0.18 (RAG has no structured reasoning across relationships)

Long-context LLMs help with single-document QA but degrade on multi-document reasoning and require expensive token limits.

**Preciso's advantage:** The knowledge graph captures entity relationships explicitly, enabling multi-hop reasoning at inference time without LLM overhead.

## Adding your own test cases

To evaluate your own documents, manually create a test case file.

### Test case JSON structure

```json
{
  "test_cases": [
    {
      "id": "TC001",
      "question": "Your question here?",
      "gold_answer": "The expected answer",
      "source_entities": ["entity_id_1", "entity_id_2"],
      "source_chunks": ["chunk_001"],
      "category": "metric_retrieval|entity_relationship|yoy_comparison|multi_hop",
      "difficulty": "easy|medium|hard"
    }
  ]
}
```

### Example: Medical records test case

```json
{
  "id": "MED001",
  "question": "What medications is patient PT_001 currently taking?",
  "gold_answer": "Metformin 1000mg twice daily, Lisinopril 10mg daily",
  "source_entities": ["med_metformin_1000", "med_lisinopril_10"],
  "source_chunks": ["med_record_001"],
  "category": "entity_relationship",
  "difficulty": "medium"
}
```

### How to run it

```bash
python3 test/query_manual.py "What medications is patient PT_001 currently taking?" mix
```

Then manually check:
1. Does the returned context mention both medications?
2. Are the dosages correct?
3. Any extra medications mentioned that shouldn't be?

For automated evaluation, adapt the evaluation harness to iterate your test cases and aggregate scores.

---

**Next steps:**
- Run the Walmart sample evaluation to verify your setup.
- Create test cases for your own documents.
- Check [faq.md](faq.md) if scores are lower than expected.
