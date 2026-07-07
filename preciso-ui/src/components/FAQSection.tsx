"use client";
import { useState } from "react";
import RevealSection from "./RevealSection";

const FAQS = [
  {
    q: "Does my data ever leave my machine?",
    a: "No. Preciso is local-first by design. Extraction runs in your agent, and the graph is written to GRAPH_IS_HERE/ on disk (NetworkX + JSON). There is no cloud dependency by default. Neo4j and Qdrant exports exist, but they are manual, one-way, and opt-in — nothing is pushed off your machine unless you explicitly run an export.",
  },
  {
    q: "Which agents can drive Preciso?",
    a: "Any MCP-capable coding agent. It is tested with Claude Code, Codex, GitHub Copilot, and OpenCode. You point the agent at the repo via .mcp.json and it calls the ingestion and query tools directly — no SDK wrappers or glue code.",
  },
  {
    q: "Do I need Neo4j, Qdrant, or a vector database?",
    a: "No. The local graph in GRAPH_IS_HERE/ is the single source of truth and it ships with a built-in vector store (NanoVectorDB) for retrieval. Neo4j and Qdrant are optional export targets for visualization or downstream tooling — never storage backends.",
  },
  {
    q: "Can it read PDFs?",
    a: "Yes, indirectly. Agents like Claude Code and Codex read PDFs natively, so you can drop them in and let the agent extract. For a pure text pipeline, convert to .md or .txt first — those are the best-supported inputs alongside README and wiki exports.",
  },
  {
    q: "How is this different from regular RAG?",
    a: "Regular RAG embeds your query and returns the top-k most similar chunks — good for lookup, weak on multi-hop questions. Preciso extracts entities and relationships into a graph, then traverses connections at query time. On the Walmart 10-K benchmark that difference is ~19% vs 95.4% answer accuracy.",
  },
  {
    q: "What does an extraction skill actually do?",
    a: "Skills are markdown files (not code) that tell the agent how to extract a given domain — what entity and relationship types to look for, and how to ground them. Preciso ships Financial, Research, General, and Reconciliation skills, and you can write your own by adding a folder with a SKILL.md.",
  },
  {
    q: "Is it free and open source?",
    a: "Yes — Apache 2.0. You can self-host, modify, and contribute skills or code. See the Contributing guide on GitHub to add a new domain skill or improve the core.",
  },
];

function FaqItem({ q, a }: { q: string; a: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b-2 border-border last:border-b-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-6 px-6 py-5 text-left group hover:bg-card transition-colors duration-150"
        aria-expanded={open}
      >
        <span className="font-barlow text-xl sm:text-2xl uppercase text-foreground leading-tight">
          {q}
        </span>
        <span
          className={`shrink-0 font-barlow text-3xl leading-none text-[var(--red)] transition-transform duration-200 ${
            open ? "rotate-45" : ""
          }`}
        >
          +
        </span>
      </button>
      <div
        className="grid transition-all duration-300 ease-out"
        style={{ gridTemplateRows: open ? "1fr" : "0fr" }}
      >
        <div className="overflow-hidden">
          <p className="px-6 pb-6 text-muted leading-relaxed max-w-3xl">{a}</p>
        </div>
      </div>
    </div>
  );
}

export default function FAQSection() {
  return (
    <section id="faq" className="py-28 border-b-2 border-border bg-surface">
      <div className="max-w-6xl mx-auto px-6">
        <RevealSection>
          <p className="font-barlow text-sm uppercase tracking-[0.25em] text-[var(--red)] mb-4">FAQ</p>
          <h2 className="font-barlow text-[72px] sm:text-[96px] leading-[0.88] uppercase text-foreground mb-16">
            Common<br />
            <span className="text-[var(--red)]">questions.</span>
          </h2>
        </RevealSection>

        <RevealSection delay={100} className="border-2 border-foreground bg-background">
          {FAQS.map((item) => (
            <FaqItem key={item.q} q={item.q} a={item.a} />
          ))}
        </RevealSection>

        {/* CTA bar — RED */}
        <RevealSection
          delay={200}
          className="border-2 border-t-0 border-foreground bg-[var(--stripe)] px-8 py-5 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4"
        >
          <p className="text-[var(--stripe-text)] text-sm font-mono opacity-70">
            Still have questions?
          </p>
          <a
            href="https://github.com/Preciso-GR/preciso-graphrag/blob/main/docs/faq.md"
            target="_blank"
            rel="noopener noreferrer"
            className="font-barlow text-lg uppercase tracking-wide text-[var(--stripe-text)] hover:opacity-70 transition-opacity"
          >
            Read the full FAQ →
          </a>
        </RevealSection>
      </div>
    </section>
  );
}
