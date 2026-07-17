---
name: research-paper-graph-extraction
description: >
  Use this skill whenever a user wants to extract and connect knowledge from academic
  research papers, scientific literature, or multi-paper corpora for graph-based ingestion.
  Triggers include: research papers, arXiv links, literature reviews, citation networks,
  hypothesis graphs, experiment comparisons, method lineages, or any academic/scientific text.
  Also trigger when the user says: "map citations in these papers", "connect findings across studies",
  "build a literature graph", "link hypotheses", "trace the origin of this method", "compare results
  across papers", "which papers cite each other", "extract contributions from this paper", or
  "I want to query my paper collection".
  This skill handles multi-paper corpora (a folder of papers) as well as single papers.
  Use this instead of general-graph-extraction whenever the source is academic/scientific literature.
  Output must follow the GraphRAG ingestion schema: document_id + entities + relationships + chunks,
  with chunk-based source_id values.
---

# Research Paper Graph Extraction Skill

## Agent Runtime Contract

Use this skill when the raw input file is in `to_be_extracted/` and the content is academic or scientific literature.

Execution contract:
- Call `get_server_status()` before starting extraction.
- If overall is `degraded`, explain what is degraded, what still works, and ask whether to proceed or fix first.
- Read one paper or one raw source file from `to_be_extracted/` at a time unless the user explicitly asks for a combined corpus extraction.
- For the best graph quality, prefer `.md` and `.txt` source files. If the source is PDF, assume there is no built-in repo parser or OCR layer and be more conservative about noisy layout, broken equations, and repeated headers/footers.
- Prefer one extraction JSON per source paper so ingestion and retries stay simple.
- Validate entity, relationship, citation, and chunk integrity before ingestion.
- If the extraction has duplicates, orphaned relationships, or conflicts that need cleanup, use the reconciliation skill before ingestion.
- After a clean extraction is written, call `ingest_from_file`.

## Why This Skill Exists

Researchers and analysts reading literature face a specific problem: insights are scattered across dozens of papers, methods evolve across years, and contradictions between studies are hard to surface. This skill builds a graph that makes a paper collection *queryable* — like having a research assistant who has read everything and can answer:

- "Which papers first introduced attention mechanisms?"
- "What datasets did papers in 2022-2023 benchmark on?"
- "Which findings from Paper A were contradicted by Paper B?"
- "What methods does Method X build upon?"
- "Which authors published together across these papers?"

This skill should produce extraction output that is:
- clean: remove layout junk, repeated headers, page numbers, citation clutter, and OCR noise
- concise: capture the paper's real contributions without bloating the graph with every sentence
- formula-aware: preserve the meaning of core equations and symbol definitions when they matter to the method or findings
- evidence-grounded: anchor claims in abstract, figures, tables, results, and discussion rather than vague paraphrase

---

## Scholar Reading Protocol

Model the extraction process on how experienced researchers read papers:

### Pass 1 — Triage and structure map

Read:
- title
- abstract
- introduction
- section and subsection headings
- conclusion or discussion ending
- references at a glance

At the end of this pass, identify:
- paper category: empirical study, theory paper, benchmark paper, survey, systems paper, review, position paper
- main question or hypothesis
- claimed contributions
- likely important sections, figures, tables, and equations
- whether the paper is readable enough for high-confidence extraction

### Pass 2 — Evidence-oriented reading

Read for substance using the paper's structure, usually close to IMRaD:
- abstract and introduction for question, scope, and contribution framing
- methods for setup, assumptions, datasets, models, baselines, and evaluation design
- results with special attention to figures, tables, captions, and ablations
- discussion/conclusion for interpretation, limitations, and future work

During this pass, extract:
- the main method, task, datasets, baselines, metrics, findings, and limitations
- key citations and method lineage
- the strongest evidence supporting each major claim

### Pass 3 — Deep verification

Do a focused deep pass only on parts that materially affect graph quality:
- central equations
- major figures and tables
- experimental setup details needed to understand findings
- assumptions, caveats, and failure cases

During this pass, challenge:
- unsupported claims
- overgeneralized conclusions
- missing baselines
- ambiguous references to prior work

If evidence is weak or the text is corrupted, reduce extraction density instead of guessing.

---

## Extraction Output Format (Repo-Compatible)

Write to `extractions/{source_filename}_extracted.json`.

This repo **requires** `document_id`, `entities`, `relationships`, and `chunks`.

```json
{
  "document_id": "attention_is_all_you_need_2017",
  "file_path": "attention_is_all_you_need.pdf",
  "timestamp": 1727740800,
  "entities": [...],
  "relationships": [...],
  "chunks": [...]
}
```

### Chunks (Required)
Chunks are the evidence base. Every entity and relationship must point to a chunk via `source_id`.

```json
{
  "chunk_id": "chunk_002",
  "content": "We present the Transformer, a model relying entirely on attention mechanisms.",
  "chunk_order_index": 2,
  "file_path": "attention_is_all_you_need.pdf"
}

**Chunk size limits (required):**
- Target 400-800 characters per chunk (or ~200-300 tokens if you have a tokenizer).
- Hard cap 1,000 characters per chunk. If a paragraph/table is longer, split it.
```

---

## Entity Types for Research Papers

### Core Academic Entities

| Type | Description | Example |
|------|-------------|---------|
| `PAPER` | A published or preprint work | "Attention Is All You Need" |
| `AUTHOR` | Named researcher | Ashish Vaswani |
| `INSTITUTION` | University, lab, company | Google Brain, MIT CSAIL |
| `METHOD` | Named technique or algorithm | Transformer, BERT, LoRA |
| `EQUATION` | Core formula or objective | Scaled dot-product attention, ELBO, cross-entropy objective |
| `DATASET` | Named evaluation dataset | ImageNet, SQuAD, MMLU |
| `METRIC` | Evaluation measure | BLEU score, F1, Perplexity |
| `FINDING` | A stated result or claim | "Transformer outperforms RNN on WMT14" |
| `HYPOTHESIS` | An untested or proposed claim | "Sparse attention approximates full attention" |
| `CONCEPT` | An abstract idea or term | Self-attention, inductive bias, fine-tuning |
| `TASK` | An NLP/ML/scientific task | Machine translation, question answering |
| `VENUE` | Publication venue | NeurIPS, ICML, Nature, arXiv |
| `LIMITATION` | An acknowledged weakness | "Quadratic complexity in sequence length" |
| `EXPERIMENT` | A distinct evaluation or ablation setup | WMT14 En-De main benchmark, ablation without positional encoding |

---

## Entity Schema (Required Fields)

```json
{
  "entity_name": "transformer_2017",
  "entity_type": "METHOD",
  "description": "Transformer model relying entirely on attention mechanisms; introduced in 2017 paper.",
  "source_id": "chunk_002",
  "file_path": "attention_is_all_you_need.pdf"
}
```

For `FINDING` entities, include evidence details in `description`:

```json
{
  "entity_name": "transformer_bleu_wmt14",
  "entity_type": "FINDING",
  "description": "Transformer achieves 28.4 BLEU on WMT14 En-De (evidence_type=empirical; section=Results).",
  "source_id": "chunk_005",
  "file_path": "attention_is_all_you_need.pdf"
}
```

`evidence_type` values: `empirical`, `theoretical`, `ablation`, `observational`, `claimed`

For `EQUATION` entities:

```json
{
  "entity_name": "scaled_dot_product_attention",
  "entity_type": "EQUATION",
  "description": "Attention(Q, K, V) = softmax(QK^T / sqrt(d_k))V; role=core scoring function; symbols=Q queries, K keys, V values, d_k key dimension.",
  "source_id": "chunk_004",
  "file_path": "attention_is_all_you_need_2017.md"
}
```

Only extract equations that are central to the paper's contribution, method, analysis, or reported objective. Do not create entities for every inline symbol or routine algebra step.

---

## Relationship Types for Research Literature

| Keyword | Usage |
|---------|-------|
| `AUTHORED_BY` | Paper → Author |
| `AFFILIATED_WITH` | Author → Institution |
| `PUBLISHED_IN` | Paper → Venue |
| `CITES` | Paper → Paper (source cites target) |
| `EXTENDS` | Method/Paper → Prior method/paper |
| `PROPOSES` | Paper → Method/Concept |
| `DEFINES` | Paper/Method → Equation or concept |
| `EVALUATES_ON` | Paper → Dataset |
| `REPORTS_METRIC` | Paper → Metric (with value) |
| `ACHIEVES` | Paper/Method → Finding |
| `COMPARES_AGAINST` | Paper/Method → Baseline method/paper |
| `CONTRADICTS` | Finding → Finding (conflicting results) |
| `SUPPORTS` | Finding → Hypothesis (evidence for) |
| `ADDRESSES` | Paper → Task |
| `INTRODUCES` | Paper → Concept/Dataset (first use) |
| `IMPROVES_OVER` | Method/Paper → Baseline method/paper |
| `HAS_LIMITATION` | Paper/Method → Limitation |
| `CO_AUTHORED_WITH` | Author ↔ Author |

### Relationship Schema (Required Fields)

```json
{
  "src_id": "bert_2018",
  "tgt_id": "transformer_2017",
  "description": "BERT uses the Transformer encoder and extends it with masked language modeling pretraining.",
  "keywords": "EXTENDS,aspect=pretraining_objective",
  "source_id": "chunk_008",
  "weight": 1.0,
  "file_path": "bert_2018.pdf"
}
```

Always include a brief justification in `description`.

---

## Multi-Paper Corpus Strategy

When given multiple papers (a folder, a bibliography, a literature review):

### Step 1 — Build the Paper Index First
Extract all `PAPER` entities with metadata before diving into content. This lets you link citations correctly.

### Step 2 — Extract Per-Paper, Then Cross-Link
For each paper:
1. Extract its entities and internal relationships
2. Identify all citations → create `CITES` relationships to other papers in the corpus
3. Flag unresolved citations with `keywords: "CITES,in_corpus=false"`

### Step 3 — Surface Cross-Paper Connections
After individual extractions, make one pass to find:
- **Shared methods**: Paper A and Paper B both evaluate `METHOD_X`
- **Contradictions**: Paper A reports 92% F1 on dataset D; Paper B reports 84% F1
- **Method lineages**: Method A → Method B → Method C chain via `EXTENDS`
- **Author networks**: Authors who appear in multiple papers → `CO_AUTHORED_WITH`

---

## Extraction Precision Rules for Research

1. **Separate findings from claims**: If the paper reports an empirical result, it's a `FINDING`. If the paper proposes something unverified, it's a `HYPOTHESIS`.
2. **Metric values must include context**: `"28.4 BLEU"` alone is meaningless — attach dataset, year, and baseline in `description`.
3. **Contribution vs. claim**: Mark contributions (things the paper introduces or proves) separately from claims (things the paper asserts without full proof).
4. **Citation directionality**: `CITES` is directed. Paper A citing Paper B means A → B.
5. **Don't merge methods carelessly**: "Self-attention" in two papers may refer to different mechanisms — use paper-scoped IDs.
6. **Evidence linkage**: Every entity and relationship must use a `source_id` that matches a real `chunk_id` in `chunks`.
7. **Figures and tables are first-class evidence**: Prefer extracting concrete claims from captions, result summaries, and linked discussion text instead of from speculative prose alone.
8. **Equations need semantic interpretation**: Preserve the role of an equation, its main symbols, and what it computes or optimizes; do not dump raw math without context.
9. **Keep extraction concise**: One strong `FINDING` entity with grounded detail is better than five overlapping paraphrases of the same result.

---

## What To Extract First

Prioritize these sections in order of graph value:

1. Abstract:
   capture the problem, method, headline findings, and claimed contribution
2. Introduction:
   capture the research question, motivation, and positioning against prior work
3. Figures and tables:
   capture metrics, comparisons, trends, error bars, ablations, and best evidence
4. Methods:
   capture setup, model design, datasets, baselines, and assumptions
5. Results:
   capture measured outcomes and what changed versus prior work
6. Discussion / Conclusion:
   capture interpretation, limitations, caveats, and future directions
7. References / Related work:
   capture only important lineage and citation structure, not every citation mention

Ignore or downweight:
- publisher boilerplate
- author footnotes unless they matter scientifically
- repeated section headers/footers
- raw citation clusters with no semantic contribution
- appendix detail unless it changes the meaning of a key result

---

## Formula, Figure, and Table Handling

### Formula handling

- Extract only central equations, objectives, loss functions, update rules, or theorem-defining expressions.
- Convert formulas into concise semantic descriptions in `description`.
- Include symbol definitions only for the symbols needed to understand the method or finding.
- Link equations to the methods, findings, or concepts they support using `DEFINES`, `PROPOSES`, `SUPPORTS`, or `EXTENDS`.
- If equation text is corrupted by OCR or broken line wraps, do not hallucinate the full expression. Extract the role instead, or skip it.

### Figure and table handling

- Treat major figures and tables as dense evidence summaries.
- Read captions together with the nearby results/discussion text.
- Extract:
  - best model or method comparisons
  - key metric values
  - ablation outcomes
  - trend direction
  - statistically or substantively meaningful differences
- When possible, represent a result as:
  - `METHOD` or `PAPER` `ACHIEVES` `FINDING`
  - `PAPER` or `METHOD` `COMPARES_AGAINST` baseline
  - `PAPER` `REPORTS_METRIC` metric
- Do not create graph entities for every row in a large table unless the rows are central to downstream querying.

---

## Clean, Concise Extraction Principles

- Prefer the paper's actual contribution language over long paraphrase.
- Separate:
  - what the paper proposes
  - what the paper measures
  - what the authors conclude
  - what limitations remain
- If multiple sentences restate the same idea, collapse them into one entity or relationship with the strongest wording and evidence.
- Use chunk boundaries that preserve scientific meaning:
  abstract block, contribution paragraph, method definition, figure caption with linked result text, limitation paragraph.
- Do not let related-work name-dropping overwhelm the graph. Extract lineage, not bibliography noise.

---

## Special: Hypothesis & Argument Graphs

For theory-heavy papers (philosophy of science, formal methods, economics), extract the argument structure:

```json
{
  "entities": [
    {
      "entity_name": "h1",
      "entity_type": "HYPOTHESIS",
      "description": "Scaling laws hold for all modalities (status=proposed; paper=scaling_laws_2020).",
      "source_id": "chunk_004",
      "file_path": "scaling_laws_2020.pdf"
    },
    {
      "entity_name": "f1",
      "entity_type": "FINDING",
      "description": "Language model loss decreases as power law of compute (evidence_type=empirical).",
      "source_id": "chunk_006",
      "file_path": "scaling_laws_2020.pdf"
    }
  ],
  "relationships": [
    {
      "src_id": "f1",
      "tgt_id": "h1",
      "description": "Findings across 7 orders of magnitude of compute support the hypothesis.",
      "keywords": "SUPPORTS,strength=strong",
      "source_id": "chunk_006",
      "weight": 1.0,
      "file_path": "scaling_laws_2020.pdf"
    }
  ]
}
```

---

## Step-by-Step Agent Workflow

```
1. CALL get_server_status() and surface degraded modes before continuing
2. READ paper(s) — abstract, introduction, related work, methods, results, conclusion
3. CHUNK the text into evidence blocks (sections, paragraphs, or tables)
   - Assign chunk_id like chunk_001, chunk_002...
4. EXTRACT metadata block (title, authors, year, venue, DOI/arXiv) into descriptions
5. PASS 1 — Named entity extraction:
   - Methods, datasets, metrics, tasks proposed or used
   - Authors and institutions
   - Prior works cited (for CITES relationships)
6. PASS 2 — Finding and hypothesis extraction:
   - Main results from abstract and results sections
   - Ablations as secondary findings
   - Limitations from limitations/conclusion sections
7. PASS 3 — Relationship extraction:
   - EXTENDS, IMPROVES_OVER for method lineage
   - CONTRADICTS for conflicting results vs. prior work
   - SUPPORTS for findings supporting hypotheses
8. VALIDATE:
   - No entity_name duplicates
   - All relationships reference existing entity_name values
   - Every entity/relationship source_id matches a chunk_id
   - Output path matches the current source file name
9. WRITE to extractions/{filename}_extracted.json
10. CALL ingest_from_file MCP tool
11. CALL list_pending_summaries() and resolve any flagged entities/relationships
    via submit_summary(...) before finishing — Preciso never compresses
    descriptions itself, so this step is the only way descriptions
    exceeding raw_tail_size ever get summarized
```

---

## Example: Research Paper Extraction

**Input (abstract excerpt):**
> "We present BERT, a language representation model designed to pre-train deep bidirectional representations by jointly conditioning on both left and right context. BERT achieves state-of-the-art results on eleven NLP tasks, including pushing GLUE to 80.4% (+7.6% absolute improvement)."

**Output:**
```json
{
  "document_id": "bert_2018",
  "file_path": "bert_2018.pdf",
  "timestamp": 1727740800,
  "chunks": [
    {
      "chunk_id": "chunk_001",
      "content": "We present BERT, a language representation model designed to pre-train deep bidirectional representations by jointly conditioning on both left and right context. BERT achieves state-of-the-art results on eleven NLP tasks, including pushing GLUE to 80.4% (+7.6% absolute improvement).",
      "chunk_order_index": 1,
      "file_path": "bert_2018.pdf"
    }
  ],
  "entities": [
    {
      "entity_name": "bert_2018_paper",
      "entity_type": "PAPER",
      "description": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding (year=2018; venue=NAACL).",
      "source_id": "chunk_001",
      "file_path": "bert_2018.pdf"
    },
    {
      "entity_name": "bert_method",
      "entity_type": "METHOD",
      "description": "BERT method using masked language modeling and next sentence prediction.",
      "source_id": "chunk_001",
      "file_path": "bert_2018.pdf"
    },
    {
      "entity_name": "glue_benchmark",
      "entity_type": "DATASET",
      "description": "GLUE benchmark used for evaluation.",
      "source_id": "chunk_001",
      "file_path": "bert_2018.pdf"
    },
    {
      "entity_name": "bert_glue_result",
      "entity_type": "FINDING",
      "description": "BERT achieves 80.4% on GLUE (+7.6% absolute improvement; evidence_type=empirical).",
      "source_id": "chunk_001",
      "file_path": "bert_2018.pdf"
    }
  ],
  "relationships": [
    {
      "src_id": "bert_2018_paper",
      "tgt_id": "bert_method",
      "description": "The BERT paper proposes the BERT method.",
      "keywords": "PROPOSES",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "bert_2018.pdf"
    },
    {
      "src_id": "bert_2018_paper",
      "tgt_id": "glue_benchmark",
      "description": "BERT evaluates on the GLUE benchmark.",
      "keywords": "EVALUATES_ON",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "bert_2018.pdf"
    },
    {
      "src_id": "bert_method",
      "tgt_id": "bert_glue_result",
      "description": "BERT method achieves the GLUE result.",
      "keywords": "ACHIEVES",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "bert_2018.pdf"
    },
    {
      "src_id": "bert_2018_paper",
      "tgt_id": "transformer_2017_paper",
      "description": "BERT extends the Transformer encoder with bidirectional pretraining.",
      "keywords": "EXTENDS",
      "source_id": "chunk_001",
      "weight": 1.0,
      "file_path": "bert_2018.pdf"
    }
  ]
}
```

---

## After Extraction

- Run `ingest_from_file("extractions/{filename}_extracted.json")` via MCP
- If ingestion fails, use `reingest_from_file(...)` — no re-extraction needed
- Call `list_pending_summaries()`. Preciso never compresses descriptions with an LLM — if any entity/relationship is flagged (its raw description history exceeded `raw_tail_size`), write a concise summary of `prior_summary` + `old_descriptions` and call `submit_summary(...)` for each one before considering the job done
- Sample queries after ingestion:
  - `"Which papers cite the Transformer paper?"`
  - `"What methods were evaluated on SQuAD?"`
  - `"Which findings contradict each other?"`
  - `"What are the limitations of BERT?"`
  - `"Who are the most prolific authors in this corpus?"`
  - `"Trace the lineage of the attention mechanism"`

---

## Large Document Handling

### When to use subagents

Never for extraction.
Always extract the full document yourself.
Your entity registry prevents name drift.

Spawn reconciliation subagents only when:
  - Extraction produces more than 150 entities
  - Extraction produces more than 300 relationships
  - You want a quality check before ingestion

### How to spawn reconciliation subagents

After writing your extraction file:

Step 1: Spawn 3 reconciliation subagents in parallel.
        Give each subagent the extraction JSON.

        Subagent 1 gets: entities list only
          Task: find entity name variants and duplicates
          Output: extractions/{filename}_patch_entities.json

        Subagent 2 gets: relationships list only
          Task: find duplicate relationships and conflicts
          Output: extractions/{filename}_patch_relationships.json

        Subagent 3 gets: entities + relationships
          Task: find broken references (src_id or tgt_id
                not in entities list)
          Output: extractions/{filename}_patch_orphans.json

Step 2: Wait for all 3 subagents to finish.

Step 3: Call ingest_with_reconciliation([
          "extractions/{filename}_extracted.json",
          "extractions/{filename}_patch_entities.json",
          "extractions/{filename}_patch_relationships.json",
          "extractions/{filename}_patch_orphans.json"
        ])

### Reconciliation subagent instructions

When you are a reconciliation subagent:
  - You are cleaning data, not extracting from documents
  - Be conservative: only flag what is clearly wrong
  - For entity merges: only merge if you are certain
    they refer to the same real-world entity
  - For temporal entities (FY2024, Q3 2024):
    NEVER merge even if semantically similar
    Different periods = different entities always
  - Write your patch file and stop
  - Do not call any ingest tool
