---
name: mse-academic-search
description: Rigorous multi-source academic search for Management Science and Engineering, including query design, parallel Web of Science, Scopus, and Google Scholar discovery, publisher verification, OpenAlex coverage auditing, Semantic Scholar expansion, Crossref identity checks, deduplication, screening, citation chasing, and coverage reporting. Use for systematic or comprehensive literature searches, search-string design, exported-record analysis, evidence gathering, and database coverage audits. Trigger on literature search, systematic search, 查文献, 文献检索, 检索式, 查漏补缺, WoS检索, Scopus检索, 谷歌学术检索, and related requests.
---

# MSE Academic Search

Separate authority, recall, relevance, and access cost. For serious research, use Web of Science Core Collection and Scopus as dual structured cores and Google Scholar as an independent parallel high-recall core. Use publisher platforms for article verification, OpenAlex for coverage auditing, Semantic Scholar for bounded semantic expansion, and Crossref for identity checks. Never treat the database that discovered a paper as a paper-quality score.

## Required Workflow

1. State a compact search contract: question, evidence boundary, dates, languages, document types, output, and `quick`, `comprehensive`, `deep`, or `local-only` scope.
2. Read [references/search-strategy.md](references/search-strategy.md). Load [references/domain-vocabulary.json](references/domain-vocabulary.json) when designing or auditing terms.
3. Build two to four role-named concept blocks such as `topic`, `context`, `method`, and `outcome`. Calibrate them against 10–20 known papers when a benchmark set exists.
4. Design source-native routes. Run the direct intersection and only critical supplements in WoS and Scopus. Run shorter high-recall queries, cited-by, related-work, and version routes in Scholar. Do not send every combinatorial variant unchanged to every source.
5. For `comprehensive` or `deep` work, run WoS and Scopus as dual structured cores and Scholar in parallel. If a core source is unavailable, record the gap; do not silently substitute an auxiliary graph and call the search complete.
6. Merge immediately. Deduplicate by DOI, then Scholar version cluster, then compatible normalized title, year, and author. Preserve every discovery source and route.
7. Rank topical relevance independently of provenance. Keep `relevance_tier`, `verification_level`, and `screening_status` separate.
8. After the core merge, use OpenAlex to audit coverage, Semantic Scholar around selected anchors or seeds, and Crossref to verify DOI identity. Verify shortlisted evidence against the publisher record before using it for final claims.
9. Stop only after planned core routes finish or their failures are documented, benchmark misses are explained, and consecutive citation or semantic rounds add no new eligible core evidence. Report counts, exclusions, overlaps, failures, costs, and blind spots.

## Source Routing

- **Serious or comprehensive search:** use WoS Core Collection + Scopus + Google Scholar. Treat all three as necessary but non-interchangeable.
- **Quick scan:** use Scholar plus the best available structured index and label the result non-comprehensive.
- **ScienceDirect and other publisher platforms:** use for publisher-record, abstract, supplement, reference-list, and authorized full-text work. The bundled script's ScienceDirect route retrieves search metadata and abstract support; it is not a full-text endpoint.
- **OpenAlex:** use after the core merge for regional, non-English, repository, and citation-graph gap auditing.
- **Semantic Scholar:** use for a bounded semantic-anchor or seed expansion, not as the sole systematic search.
- **Crossref:** use after deduplication for DOI and canonical metadata checks. Do not use it to judge topical relevance or methodological quality.
- **Auxiliary-only records:** keep them in screening, but do not promote them to final evidence until publication status and publisher identity are verified.
- **Chinese literature:** add CNKI, Wanfang, or another appropriate Chinese index when the question requires Chinese coverage. Keep its counts separate.
- **Local-only request:** remain inside supplied PDFs, Zotero, or exports. Do not add online sources silently.

When browser access to WoS, Scholar, or a publisher is required, follow the installed browser-control skill. Reuse a logged-in session only within the user's requested scope.

## Bundled Script

Use `scripts/academic_search.py` for deterministic planning, API retrieval, import, deduplication, ranking, verification labels, and auditing. It uses only the Python standard library.

For a serious search, provide explicit concept blocks and inspect the source-native plan first:

```bash
python3 scripts/academic_search.py \
  --concept 'topic:phenomenon=technology adoption|process innovation' \
  --concept 'context:industry=manufacturing firms|service firms' \
  --concept 'method:method=panel data|difference in differences' \
  --profile rigorous \
  --mode comprehensive \
  --sources auto \
  --dry-run
```

Run WoS externally with the generated `TS=` routes, export each round before merging, then label the import explicitly:

```bash
python3 scripts/academic_search.py \
  --concept 'topic:phenomenon=technology adoption|process innovation' \
  --concept 'context:industry=manufacturing firms|service firms' \
  --concept 'method:method=panel data|difference in differences' \
  --profile rigorous \
  --mode comprehensive \
  --sources auto \
  --wos-import core_intersection=savedrecs_core.bib \
  --wos-import leave_out:method=savedrecs_no_method.bib \
  --wos-import leave_out:industry=savedrecs_no_industry.bib \
  --wos-import leave_out:phenomenon=savedrecs_no_phenomenon.bib \
  --benchmark-dois benchmark.json \
  --screening-output screening_pool.csv \
  --audit-output search_audit.json
```

Use `--import-file` for unlabeled RIS, BibTeX, CSV, TSV, or JSON records. Map every WoS export as `--wos-import ROUTE=PATH`; repeat it for every planned route. An unlabeled WoS path is retained but cannot prove route completion. Use `--format csv` only for the ranked preview; use `--screening-output` to retain the full deduplicated screening pool.

The `legacy` profile preserves the former public-source behavior. Use it only for exploratory or backward-compatible runs; use `--profile rigorous` for scientific coverage claims.

## Verification and Screening

- `V1`: discovered in WoS or Scopus and carries a stable DOI.
- `V2`: verified on the ScienceDirect publisher platform and carries a DOI, but is not present in the searched structured-core records.
- `V3`: thesis, working paper, report, preprint, or repository literature; keep it in a separate gray-literature layer.
- `V4`: candidate not yet verified through a structured core or publisher platform; do not use it to support final claims.

Treat these as provenance states, not assessments of methods or findings. Crossref DOI verification alone does not promote an auxiliary-only candidate to `V1` or `V2`. Treat script output as a ranked screening pool, never as automatically included evidence.

## API and Agent Discipline

Treat source API budgets and agent tokens as different constraints.

- Inspect credentials, entitlement, account state, or returned quota headers only when the relevant source route is about to run.
- Bound Scholar routes, stage Elsevier retrieval, batch public graph calls, and run Crossref identity checks only after deduplication.
- Retrieve metadata first, deduplicate second, abstracts third, and full text last.
- Use deterministic code for retrieval, normalization, deduplication, version clustering, and first-pass scoring.
- Use agents only at decision gates: query audit, title screening, abstract screening, and full-text synthesis. Give agents disjoint batches and reuse the canonical record store.
- Do not query agent token, goal, or quota balances before a search. Control token use through staged screening, compact records, caching, and non-overlapping assignments.
- Never install raw Scholar scrapers, rotate proxies, bypass CAPTCHAs, or call PDF/full-text endpoints without authorization.

## Default Output

Keep the user-facing result small:

1. one-sentence adequacy judgment;
2. the strongest direct evidence;
3. the defensible coverage limitation;
4. the screening pool and audit artifact when requested.

Report `not found in the searched sources`, not `no study exists`. Distinguish `usable first version` from `complete coverage`.
