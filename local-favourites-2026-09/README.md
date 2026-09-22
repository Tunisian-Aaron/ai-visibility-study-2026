# Local favourites study — 22–23 September 2026

"Who's the best plumber / dentist / bakery in {city}?" asked 25 times each in 20 cities to ChatGPT (gpt-4.1, web search available) and Gemini (gemini-flash-latest, Google Search grounding offered). 3,000 answers. Write-up: https://see-geo.com/blog/chatgpt-local-favourite (FR /fr/blog/…, DE /de/blog/…).

## Files
- `answers.jsonl` — one line per answer: `q` (trade-city), `city`, `trade`, `surface`, `run`, `ts`, `latencyMs`, `grounded`, `citations` (URLs), `answer` (verbatim).
- `summary.json` — per (surface, trade-city): n, grounded, distinct businesses named, median per answer, answers naming nobody, most-named business and share, businesses ≥90% / ≥50%, first-named distribution, pairwise Jaccard overlap, per-business counts with Wilson 95% intervals, cited domains, directory share.
- `rollup.json` — per engine and trade: medians and counts used in the article; `cells` holds the per-city table.
- `extra-chatgpt.json` — card-format share, own-site vs Google Maps link shares, GBP-tagged link count, Gemini Reddit share.

## Extraction
A business is "named" when it appears in a heading, list item, table row or bold-led line; names normalised (case, punctuation, LLC/Inc/Ltd/Co/DDS/PC removed) and a shorter name folded into a longer one with the same prefix within a city when the longer is at least as frequent; generic headings stop-listed and the residue reviewed by hand. The code (`analyze.py`, `rollup.py`) is included.

## License
CC BY 4.0. Businesses are named because the engines named them; none was audited or contacted. Cite as: SeeGeo, "Local favourites study", September 2026, this repository.
