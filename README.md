# SeeGeo 107-site AI visibility study — aggregate data (2026-08-17)

Reference data for the study published at
**https://see-geo.com/blog/how-many-websites-block-ai-crawlers**

We audited 120 candidate small-business websites across 7 countries with
the same engine that powers [SeeGeo's free audit](https://see-geo.com);
107 audits succeeded and form every denominator here.

## Files

- **aggregate.json** — every statistic in the study, machine-readable
- **FINDINGS.md** — the same numbers, human-readable
- **METHODOLOGY.md** — sample construction, run parameters, exact metric
  definitions, limitations (published verbatim with the study)
- **llms-classification.json** — per-file classification of the 27
  llms.txt files found (platform-generated vs other), by signature

## What is deliberately NOT here

The domain list and per-site results. The study reports aggregate
statistics only — we do not name the audited businesses, and this
repository follows that rule. Researchers with a legitimate need can
contact info@see-geo.com.

## Headline numbers

- 3 of 107 (2.8%) explicitly block ≥1 major AI crawler in robots.txt
- 0 of 107 block OAI-SearchBot (the ChatGPT-citations crawler)
- 29 of 107 (27.1%) have no JSON-LD structured data anywhere
- 69 of 107 (64.5%) lack a plain-language "what we do" statement
- 27 of 107 (25.2%) have llms.txt — nearly all platform-generated,
  not owner-adopted
- Grades: B 6 · C 38 · D 38 · F 25

Snapshot data, one crawl per site on 2026-08-17. Convenience sample —
describes these 107 sites only, not "the web".

## License

Data released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
— cite "SeeGeo 107-site AI visibility study, August 2026" with a link to
the study.

## Second dataset: the repeated-question study (2026-09-22)

The same three buying questions asked 100 times each to ChatGPT, Gemini and Claude (1,000 answers), with per-brand recommendation rates, Wilson intervals, answer-to-answer overlap and the domains ChatGPT read. See **repeated-question-2026-09/** and the write-up at https://see-geo.com/blog/same-question-100-times.
