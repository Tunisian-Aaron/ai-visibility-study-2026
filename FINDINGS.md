# 100-Site Study — Findings (generated 2026-08-17)

Candidates: 120. Successfully audited: **107**. Excluded: checker_blocked 2, unreachable 8, moved 3.

## Access (robots.txt)
- Serve a robots.txt: 101 of 107 (94.4%)
- Explicitly block ≥1 major AI crawler: **3 of 107 (2.8%)**
- Root-blocked for ≥1 AI crawler incl. wildcard rules: 3 of 107 (2.8%)

| Bot | Explicit block | Any root block |
|---|---|---|
| GPTBot | 3 of 107 (2.8%) | 3 of 107 (2.8%) |
| OAI-SearchBot | 0 of 107 (0.0%) | 0 of 107 (0.0%) |
| ClaudeBot | 3 of 107 (2.8%) | 3 of 107 (2.8%) |
| PerplexityBot | 1 of 107 (0.9%) | 1 of 107 (0.9%) |
| Google-Extended | 2 of 107 (1.9%) | 2 of 107 (1.9%) |
| CCBot | 2 of 107 (1.9%) | 2 of 107 (1.9%) |
| Googlebot | 0 of 107 (0.0%) | 0 of 107 (0.0%) |
| Bingbot | 0 of 107 (0.0%) | 0 of 107 (0.0%) |

Cause breakdown among the 3 explicit blockers: robots.txt only (no WAF signals) 2; robots.txt + WAF signals 1. CDN-level blocking beyond these signals is not externally measurable — see METHODOLOGY.

## CDN signals
- Any WAF/CDN signature detected: 32 of 107 (29.9%)
- Cloudflare-fronted: 31 of 107 (29.0%)
- Cloudflare managed-robots.txt signature: 0 of 107 (0.0%)

## Content & schema
- Primary content missing without JavaScript: **3 of 107 (2.8%)**
- No JSON-LD anywhere in the crawl: **29 of 107 (27.1%)**
- JSON-LD on homepage: 75 of 107 (70.1%)
- JSON-LD beyond the homepage: 69 of 107 (64.5%)
- llms.txt present: 27 of 107 (25.2%)
- Plain-language "what we do" statement detected: 38 of 107 (35.5%)

## Platforms (subgroups under n=20 marked insufficient)
| Platform | n | Blocks ≥1 AI bot (explicit) | No schema |
|---|---|---|---|
| wordpress | 38 | 0 of 38 (0.0%) | 4 of 38 (10.5%) |
| custom/unknown | 34 | 2 of 34 (5.9%) | 20 of 34 (58.8%) |
| squarespace | 16 | insufficient sample | insufficient sample |
| shopify | 8 | insufficient sample | insufficient sample |
| wix | 7 | insufficient sample | insufficient sample |
| other:square | 2 | insufficient sample | insufficient sample |
| other:drupal | 1 | insufficient sample | insufficient sample |
| other:starfield | 1 | insufficient sample | insufficient sample |

Cloudflare-fronted split: CF n=31 blocks-any 1 of 31 (3.2%) · non-CF n=76 blocks-any 2 of 76 (2.6%)

## Grades (context, not a headline)
B: 6 · C: 38 · D: 38 · F: 25
