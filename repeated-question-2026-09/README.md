# Repeated-question study — 22 September 2026

The same three buying questions, asked 100 times each to ChatGPT, Gemini and Claude, plus a ChatGPT no-search control on the CRM question. 1,000 answers. Companion to the write-up at https://see-geo.com/blog/same-question-100-times (FR: /fr/blog/…, DE: /de/blog/…).

## Files

- `answers.jsonl` — one line per answer: `q` (crm | pm | acct), `surface` (chatgpt | gemini | claude | chatgpt-nosearch), `run`, `ts` (UTC), `latencyMs`, `grounded` (did the model use web search / grounding), `citations` (cited URLs, ChatGPT only), `model` (as returned by OpenRouter, Claude only), `answer` (verbatim).
- `summary.json` — per (surface, question): n, grounded count, distinct recommended brands, median brands per answer, mean pairwise Jaccard overlap, brands recommended in ≥90% and <20% of answers, first-named distribution, and per-brand counts with 95% Wilson intervals.
- `sources-chatgpt-*.json` — cited domains per question for the grounded ChatGPT cells.
- `brands.json` — the brand dictionary (canonical name → alias regexes) used for extraction.

## Questions

- crm — "What's the best CRM for a small business?"
- pm — "What's the best project management tool for a small team?"
- acct — "Which accounting software should a freelancer use?"

## Engines

- ChatGPT: OpenAI Responses API, `gpt-4.1`, `web_search_preview` tool available on every call (omitted for the `chatgpt-nosearch` control).
- Gemini: `gemini-flash-latest`, `google_search` grounding offered on every call.
- Claude: `anthropic/claude-sonnet-5` via OpenRouter, no tools.

Default temperatures, no system prompt, no conversation history, three concurrent requests per engine, from a single location.

## Extraction

"Recommended" = the brand appears in a heading, numbered/bulleted item, table row or bold-led line. "Named first" = the first such brand in reading order. Passing mentions in body text are excluded. Every unmatched heading was reviewed by hand. Interval = Wilson 95%.

## License

CC BY 4.0. The answers are model outputs; the brands named are public companies. Cite as: SeeGeo, "Repeated-question study", September 2026, https://github.com/Tunisian-Aaron/ai-visibility-study-2026.
