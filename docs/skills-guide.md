# Skills Guide

## What a skill is

A skill is an agent-readable instruction file that teaches the system how to extract entities and relationships from a specific type of document. Skills are domain-specific — a financial skill knows how to find revenue, executives, and risks in a 10-K filing, while a research skill knows how to find hypotheses, datasets, and conclusions in a paper. Each skill is a markdown file in `skills/` that an agent reads before deciding how to extract and structure knowledge from source material the agent can read reliably.

For the best graph quality in this repo, prefer markdown and plain text inputs. Preciso does not include a built-in parser or OCR layer, so PDFs are outside the recommended default path.

## Verify the source before using a skill

Do not start extraction until a human has confirmed that every source document is accurate, current, complete, and the intended version. A skill tells the agent how to represent what a document says; it does not certify that the document itself is true.

This review must happen before the agent reads and extracts the corpus. Extraction may consume paid agent or language-model credits, and ingestion may consume paid embedding-provider credits when it creates vectors for chunks, entities, and relationships. Processing flawed data can waste both sets of credits and may later require re-extraction plus a full graph rebuild.

## The three built-in skills

### Financial-Graph-Extraction

**When to use:** 10-K/10-Q filings, earnings calls, analyst reports, financial presentations, shareholder letters.

**What it extracts:**
- **Entities:** Companies, executives, metrics (revenue, net income, headcount), segments, risk factors.
- **Relationships:** REPORTS_METRIC, EMPLOYS, EXPOSED_TO, COMPETES_WITH.
- **Structure:** Fiscal-year-aware; includes year-over-year COMPARED_TO edges for metrics.

**Location:** `skills/Financial-Graph-Extraction/SKILL.md`

### Research-paper-graph-extraction-skill

**When to use:** Academic papers, research reports, whitepapers, scientific literature.

**What it extracts:**
- **Entities:** Hypotheses, methodologies, datasets, findings, authors, institutions.
- **Relationships:** TESTS_HYPOTHESIS, USES_METHOD, CITES, VALIDATES_FINDING.
- **Structure:** Citation-aware; cross-references and result lineage.

**Location:** `skills/Research-paper-graph-extraction-skill/SKILL.md`

### General-graph-extraction-skill

**When to use:** Codebases, internal wikis, documentation, blogs, meeting notes — anything non-financial and non-academic.

**What it extracts:**
- **Entities:** Concepts, tools, processes, people, decisions, components.
- **Relationships:** USES, DEPENDS_ON, IMPLEMENTS, OWNS, DOCUMENTS.
- **Structure:** Flexible; designed to capture organizational and technical knowledge.

**Location:** `skills/General-graph-extraction-skill/SKILL.md`

## Writing your own skill

### Why write one

The built-in skills cover common document types. If you have a specialized domain — medical records, legal contracts, insurance claims, internal systems documentation — writing a domain-specific skill will improve extraction quality and ensure the graph captures domain-relevant entities and relationships.

### Template

Create a new file `skills/Your-Domain-Extraction/SKILL.md`:

```markdown
# Your Domain Extraction Skill

## Purpose
[One sentence: what documents this skill processes]

## Entity Types
[List entity types relevant to your domain with examples]

Example:
- PATIENT (name, age, medical_id)
- CONDITION (diagnosis, severity, onset_date)
- MEDICATION (name, dosage, frequency)
- PROVIDER (name, title, specialization)

## Relationship Types
[List relationships with examples]

Example:
- PATIENT HAS_CONDITION CONDITION
- PATIENT TAKES_MEDICATION MEDICATION
- PROVIDER TREATS_PATIENT PATIENT
- MEDICATION TREATS_CONDITION CONDITION

## Extraction Rules
[Specific extraction instructions for your domain]

1. Always extract patient anonymized ID (never raw names in production).
2. Normalize medication names to standard nomenclature (INN or RxNorm).
3. Extract severity as structured enum: mild, moderate, severe.
4. Link conditions to ICD-10 codes when available.

## Example Extraction
[JSON example of expected output]

```

### Concrete example: Medical Records Skill

```markdown
# Medical Records Extraction Skill

## Purpose
Extracts clinical facts and patient care relationships from electronic health records (EHRs).

## Entity Types
- PATIENT (medical_id, age, gender, allergies)
- CONDITION (icd10_code, name, onset_date, status)
- MEDICATION (rxnorm_code, name, dosage_mg, frequency)
- LAB_TEST (code, name, result, reference_range, date)
- PROVIDER (npi_code, name, specialty, organization)

## Relationship Types
- PATIENT HAS_CONDITION CONDITION (date_diagnosed)
- PATIENT TAKES_MEDICATION MEDICATION (date_start, date_end)
- PATIENT UNDERGOES LAB_TEST (date_ordered, date_resulted)
- PROVIDER TREATS_PATIENT PATIENT (specialty_context)
- CONDITION RELATED_TO CONDITION (clinical_notes)

## Extraction Rules
1. Extract numeric values for lab results with units.
2. Always normalize drug names to RxNorm identifiers.
3. Keep provider NPI and patient medical_id; never expose raw PII.
4. Extract clinical decision dates as ISO 8601.

## Example Extraction
{
  "patient_id": "PT_HASH_001",
  "conditions": [
    {
      "entity_id": "cond_001",
      "icd10": "E11.9",
      "name": "Type 2 Diabetes Mellitus",
      "onset_date": "2019-06-15"
    }
  ],
  "medications": [
    {
      "entity_id": "med_001",
      "rxnorm": "314014",
      "name": "Metformin",
      "dosage_mg": 1000,
      "frequency": "twice daily"
    }
  ],
  "relationships": [
    {
      "src_id": "PT_HASH_001",
      "tgt_id": "cond_001",
      "type": "HAS_CONDITION",
      "date_diagnosed": "2019-06-15"
    }
  ]
}
```

## The entity registry rule

**Why it matters:** Every entity extracted into the graph must have a unique, stable identifier. If you extract the same entity multiple times with different IDs, the graph will have orphaned nodes and broken relationships.

**The rule:** Use a canonical identifier for each entity type. For companies, use ticker or CIK. For people, use a normalized name or employee ID. For metrics, include the fiscal year and context (e.g., `walmart_revenue_2023` not just `revenue`).

**Example:**
- ❌ Bad: Entity `revenue` with id `metric_1` in one document, `metric_2` in another. The graph now has two separate "revenue" nodes.
- ✅ Good: Entity `walmart_revenue_2023` with stable id `walmart_revenue_2023`. If the same metric appears in multiple docs, the graph correctly merges them.

When writing your skill, define a clear ID strategy upfront. The agent will follow it consistently.

---

**Next steps:**
- Copy `skills/General-graph-extraction-skill/SKILL.md` as a template.
- Define your entity and relationship types.
- Test it on a sample document by asking an agent to extract using your new skill.
