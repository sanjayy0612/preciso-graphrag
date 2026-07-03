export async function* streamOpenAI(opts: {
  apiKey: string; model: string; system: string; user: string;
}): AsyncGenerator<string> {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${opts.apiKey}` },
    body: JSON.stringify({
      model: opts.model, stream: true,
      messages: [{ role: 'system', content: opts.system }, { role: 'user', content: opts.user }],
    }),
  });
  if (!res.ok) throw new Error(`OpenAI error ${res.status}: ${await res.text()}`);
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value);
    const lines = buffer.split('\n');
    buffer = lines.pop()!;
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const payload = line.slice(6).trim();
      if (payload === '[DONE]') return;
      try {
        const json = JSON.parse(payload);
        const delta = json.choices?.[0]?.delta?.content;
        if (delta) yield delta;
      } catch { /* skip */ }
    }
  }
}

export async function* streamCohere(opts: {
  apiKey: string; model: string; system: string; user: string;
}): AsyncGenerator<string> {
  const res = await fetch('https://api.cohere.ai/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${opts.apiKey}` },
    body: JSON.stringify({
      model: opts.model, stream: true,
      preamble: opts.system,
      message: opts.user,
    }),
  });
  if (!res.ok) throw new Error(`Cohere error ${res.status}: ${await res.text()}`);
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value);
    const lines = buffer.split('\n');
    buffer = lines.pop()!;
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const json = JSON.parse(line);
        if (json.event_type === 'text-generation' && json.text) yield json.text;
      } catch { /* skip */ }
    }
  }
}

// ── Non-streaming completions (keyword extraction) ──────────────────────────

async function completeOpenAI(opts: {
  apiKey: string; model: string; system: string; user: string;
}): Promise<string> {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${opts.apiKey}` },
    body: JSON.stringify({
      model: opts.model,
      response_format: { type: 'json_object' },
      messages: [{ role: 'system', content: opts.system }, { role: 'user', content: opts.user }],
    }),
  });
  if (!res.ok) throw new Error(`OpenAI error ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return json.choices?.[0]?.message?.content ?? '';
}

async function completeCohere(opts: {
  apiKey: string; model: string; system: string; user: string;
}): Promise<string> {
  const res = await fetch('https://api.cohere.ai/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${opts.apiKey}` },
    body: JSON.stringify({ model: opts.model, preamble: opts.system, message: opts.user }),
  });
  if (!res.ok) throw new Error(`Cohere error ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return json.text ?? '';
}

// Mirrors the backend's extract_keywords_only (core/query.py): distills a long
// question into low-level keywords (specific entities — drives entity search)
// and high-level keywords (themes — drives relationship search).
// Returns null on any failure so callers fall back to the raw query.
export interface QueryKeywords {
  lowLevel: string[];
  highLevel: string[];
}

const KEYWORD_SYSTEM = `Extract search keywords from the user's question about a knowledge graph. Respond with ONLY compact JSON, no prose: {"low_level_keywords": [...], "high_level_keywords": [...]}. low_level_keywords are specific entities, names, metrics, and dates mentioned or implied. high_level_keywords are broader themes and relationship concepts the question is about.`;

export async function extractKeywords(
  query: string,
  llm: { provider: 'openai' | 'cohere'; model: string; apiKey: string },
): Promise<QueryKeywords | null> {
  try {
    const raw = llm.provider === 'openai'
      ? await completeOpenAI({ apiKey: llm.apiKey, model: llm.model, system: KEYWORD_SYSTEM, user: query })
      : await completeCohere({ apiKey: llm.apiKey, model: llm.model, system: KEYWORD_SYSTEM, user: query });
    const match = raw.match(/\{[\s\S]*\}/);
    if (!match) return null;
    const data = JSON.parse(match[0]);
    const clean = (v: unknown) => Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string' && !!s.trim()) : [];
    const lowLevel = clean(data.low_level_keywords);
    const highLevel = clean(data.high_level_keywords);
    if (!lowLevel.length && !highLevel.length) return null;
    return { lowLevel, highLevel };
  } catch {
    return null;
  }
}

// ── Embeddings ──────────────────────────────────────────────────────────────

export async function embedOpenAI(texts: string[], model: string, apiKey: string): Promise<number[][]> {
  const res = await fetch('https://api.openai.com/v1/embeddings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model, input: texts }),
  });
  if (!res.ok) throw new Error(`OpenAI embed error ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return (json.data as { embedding: number[] }[]).map(d => d.embedding);
}

export async function embedCohere(texts: string[], model: string, apiKey: string, inputType: 'search_document' | 'search_query' = 'search_document'): Promise<number[][]> {
  const res = await fetch('https://api.cohere.ai/v1/embed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ model, texts, input_type: inputType }),
  });
  if (!res.ok) throw new Error(`Cohere embed error ${res.status}: ${await res.text()}`);
  const json = await res.json();
  return json.embeddings as number[][];
}

export async function embedBatch(
  texts: string[],
  provider: 'openai' | 'cohere',
  model: string,
  apiKey: string,
  inputType: 'search_document' | 'search_query' = 'search_document',
): Promise<number[][]> {
  if (provider === 'openai') return embedOpenAI(texts, model, apiKey);
  return embedCohere(texts, model, apiKey, inputType);
}

export function cosineSimilarity(a: number[], b: number[]): number {
  if (!a.length || a.length !== b.length) return 0;
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  return denom === 0 ? 0 : dot / denom;
}

export const SYSTEM_PROMPT = `You are a knowledge graph reasoning assistant for Preciso. Answer using ONLY the provided graph context. Cite supporting entities inline with their bracketed reference numbers exactly as they appear in the context, e.g. [1] or [3]. Be concise. If the graph doesn't contain the answer, say so — do not speculate.`;
