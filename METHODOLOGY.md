# Methodology — SeeGeo 107-site small-business AI visibility study

*Runs conducted 2026-08-17. This document is published verbatim alongside the study.*

## Sample
- 120 candidate domains were collected from public "best of" listicles, local directory pages, and trade-association member lists across 12 business categories (restaurants/cafés, trades, clinics, salons/fitness, independent retail, professional services, auto repair, artisan producers) and multiple regions (US, UK, Ireland, Canada, Australia, France, Germany).
- Selection rules: small businesses only; national chains, franchises of major brands, universities, and nonprofits excluded during collection (43 active exclusions logged with reasons in the study's exclusions file); aggregator/directory domains excluded; the business's own site only. No site owned by SeeGeo or anyone we have a relationship with.
- The sample is a convenience sample. It is NOT random and NOT representative of "the web" — findings describe this sample only.
- We do not name the audited businesses. Aggregate statistics only.

## Runs
- Each site was crawled once on 2026-08-17 by the same engine that powers SeeGeo's public audit (homepage + up to ~8 site pages + robots.txt + sitemap + an llms.txt probe).
- User-agent: `SeeGeoAudit/1.0 (+https://see-geo.com/bots)` — honestly identified, with an explanation page.
- Politeness: same-domain requests spaced ≥1 second (AUDIT_CRAWL_DELAY_MS=1000); ~15–20 requests per site total, one visit, no re-crawls except a single retry after a network failure.
- 13 of 120 candidates were excluded at run time (checker_blocked: 2, unreachable: 8, moved: 3); they appear in no denominator. A site that blocked or challenged OUR checker is recorded as "could not verify" and excluded — it is never counted as "blocks AI crawlers".

## Definitions (exact)
- **"Blocks bot X" (explicit)**: the site's robots.txt contains a user-agent group that explicitly matches X, and evaluating X against the site root ("/") yields *disallowed*. Wildcard-only disallows (`User-agent: *`) are reported separately as "any root block" and never merged into the explicit number.
- **CDN/WAF signals**: response-header signatures (e.g. `server: cloudflare`, challenge headers). We report *detected signals* only. CDN-level bot blocking that leaves no external signature is not measurable from outside — our numbers are therefore a floor, not a ceiling.
- **"Primary content missing without JavaScript"**: the engine's deterministic verdict that the crawled HTML (no JS execution — what most AI crawlers see) contains no usable primary content.
- **Structured data**: presence of parseable JSON-LD blocks; "beyond the homepage" = ≥1 non-homepage crawled page carries JSON-LD.
- **llms.txt present**: HTTP 200 at /llms.txt with non-HTML body (soft-404s serving the site shell are counted absent; one initial positive was corrected to absent on re-verification and is logged as such). Each present file was re-fetched and classified by signature: Shopify's auto-generated "Agent Instructions" format, Wix's auto-generated markdown, SEO-plugin output (All in One SEO), or other.
- **"What we do" statement**: the engine's shipped heuristic (EN/FR/DE patterns over the first 1,200 characters of homepage text plus the meta description).

## Limitations
- Convenience sample; results describe these 107 sites on 2026-08-17, nothing more.
- CDN-level blocking is partially detectable at best; explicit robots.txt rules are fully detectable. The two are never summed.
- Bot rosters change; the list evaluated is the engine's roster as of the run date.
- "Unreachable" exclusions include sites that refused all connections; some of those refusals may themselves be network-level blocks of our checker. We cannot distinguish the two from outside, which is one more reason excluded sites never enter any denominator.
- Subgroup statistics are reported only where n ≥ 20; smaller subgroups are marked "insufficient sample".
- The underlying aggregate data (no business names) is available on request: info@see-geo.com.
