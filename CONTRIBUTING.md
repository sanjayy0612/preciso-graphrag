# Contributing to Preciso

Thanks for your interest in contributing.
Preciso is early-stage and moving fast —
contributions of any size are genuinely useful.

---

## What We Need Most

### New Extraction Skills
The highest-value contribution right now.

A skill is a markdown file that tells agents how to extract
entities and relationships from a specific document type.

Skills we want:
  - Legal contracts (parties, obligations, dates, penalties)
  - Medical/clinical documents (conditions, treatments, outcomes)
  - ESG reports (metrics, commitments, targets)
  - Patent filings (claims, prior art, inventors)
  - News articles (events, entities, sentiment)
  - Earnings call transcripts (guidance, tone, Q&A)

How to contribute a skill:
  1. Fork the repo
  2. Copy skills/General-graph-extraction-skill/SKILL.md
  3. Rename to skills/Your-Domain-Skill/SKILL.md
  4. Adapt the entity types and relationship types
     for your domain
  5. Add 2-3 extraction examples showing input text
     and expected output JSON
  6. Test it on a real document
  7. Open a PR with your eval results

### Eval Results on New Documents
Run the eval skill on any document type and
open a PR with your results JSON.
This builds the benchmark database.

### Bug Reports
Open an issue with:
  - What you ran
  - What you expected
  - What actually happened
  - Python version and OS

### Integration Adapters
New export targets beyond Neo4j and Qdrant.
Examples: Memgraph, TigerGraph, Weaviate, Pinecone.

---

## How to Open a PR

1. Fork the repo
2. Create a branch: git checkout -b your-feature
3. Make your change
4. Run: python3 -m compileall core ingest preciso_mcp
   Must pass with zero errors
   (Set up once with `pip install -e .` from the repo root so the
   `preciso_mcp`, `core`, and `ingest` packages resolve everywhere.)
5. Open a PR with a clear description

---

## Skill Contribution Template

When contributing a new skill, your PR description
should include:

  Domain: [what document type]
  Entity types added: [list]
  Relationship types added: [list]
  Test document used: [describe it, do not attach if confidential]
  Eval result: [overall score if you ran it]

---

## What We Will Not Merge

  - Changes that break existing eval scores
  - Skills without at least one extraction example
  - Dependencies that require cloud services to function
  - Anything that sends data outside the local machine
    by default

---

## Community

  GitHub Discussions for questions and ideas
  Issues for bugs and feature requests
  PRs for code and skill contributions

Named after Bruno Fernandes.
Every contribution should land exactly where it needs to.