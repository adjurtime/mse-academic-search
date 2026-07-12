# Search Strategy

## Source Layers

| Layer | Sources | Main role | Boundary |
|---|---|---|---|
| Dual structured core | Web of Science Core Collection + Scopus | Reproducible fielded search, indexed records, citation structure | Curated coverage can omit new, regional, or non-indexed work |
| Parallel high-recall core | Google Scholar | Cross-publisher discovery, versions, books, theses, reports, preprints, and repositories | Crawler coverage and metadata vary; totals are estimates |
| Publisher evidence | ScienceDirect and other publisher platforms | Publisher record, abstract, supplement, references, and authorized full text | A publisher platform is not a global cross-publisher index |
| Coverage audit | OpenAlex | Regional, non-English, repository, open-access, and graph-gap detection | Inclusive aggregation is not a WoS-style quality gate |
| Semantic expansion | Semantic Scholar | Similar papers, terminology variants, and targeted citation expansion | Model ranking is not a reproducible systematic-search boundary |
| Identity resolution | Crossref | DOI, canonical metadata, update, and version checks | Metadata completeness varies; it is not a relevance database |
| Bounded evidence | Zotero and local exports | User-defined local library or supplied evidence | Coverage ends at the selected local scope |

Treat these layers as complementary, not interchangeable. Indexing supports reproducibility and source-selection status; Scholar improves recall; publisher verification establishes the publication record; auxiliary graphs test whether the core search has blind spots. A paper's discovery source does not determine its scientific quality.

## Search Contract

Record before searching:

- research question and decision the evidence must support;
- evidence boundary and excluded adjacent topics;
- dates, languages, document types, and geographical scope;
- `quick`, `comprehensive`, `deep`, or `local-only` mode;
- outputs: query plan, screening pool, audit, bibliography, or synthesis;
- known benchmark papers and expected blind spots;
- access limitations and databases requiring manual export.

For a focused serious search, prefer explicit concept blocks. If concepts are inferred from a natural-language prompt, review them before making coverage claims.

## Query Architecture

Build two to four concept blocks. Keep synonyms inside a block with `OR`; connect blocks with `AND`.

Run a small set of purposeful routes:

1. **Core intersection:** all blocks; use for direct evidence.
2. **Critical supplements:** omit only a method, context, or other restrictive block when needed to recover theory, method, or field evidence.
3. **Exact identity:** verify selected titles or DOIs after deduplication.
4. **Citation expansion:** chase references and citing papers from the strongest seeds.

Do not turn every synonym combination into a query for every database. Source-native Boolean blocks preserve recall more efficiently in WoS and Scopus; short anchor queries work better in Scholar; auxiliary sources should run only bounded audit routes.

## Rigorous Parallel Core

1. Translate one reviewed concept contract into separate WoS `TS=` and Scopus `TITLE-ABS-KEY()` syntax.
2. Run the direct route and critical supplements independently in both indexes. Preserve every original export and per-route count. Import WoS files as `ROUTE=PATH`; an unlabeled combined export cannot prove that every planned route ran.
3. Run shorter Scholar routes in parallel from the start. Include exact-title, cited-by, related-work, and all-version routes when relevant.
4. Merge WoS, Scopus, and Scholar before gap auditing. Preserve source-exclusive works and distinguish unique works from alternate versions.
5. If WoS or Scopus cannot be run, label the core search incomplete. OpenAlex or Semantic Scholar may diagnose the gap but cannot silently replace the missing structured index.

Use Scholar's actual retrieved and reviewed record count. Never use its displayed total as a PRISMA or coverage denominator; the estimate is route-level and non-additive.

## Staged Auxiliary Search

After the core merge:

1. Run OpenAlex on one or two broad anchors to identify regional, language, repository, and citation-graph gaps.
2. Run Semantic Scholar around selected high-value anchors or seeds. Avoid repeating the full Boolean matrix.
3. Run Crossref DOI lookups only for deduplicated shortlisted records. Crossref confirms identity; it does not confirm topical relevance or methodological quality.
4. Use ScienceDirect or the appropriate publisher platform for publisher-record and abstract checks. Use browser or authorized retrieval for full text; the bundled ScienceDirect Search API route is not a full-text endpoint.
5. Feed genuinely new terms back into WoS and Scopus, then record the new round separately.

## Deduplication and Canonical Records

Merge in this order:

1. normalized DOI;
2. Scholar version-cluster identifier;
3. exact normalized title only when DOI, year, and first-author evidence are compatible.

Do not merge two records with different DOIs solely because their titles match. Preserve alternate DOI and version relationships. Choose canonical metadata by source reliability rather than thread completion order, and retain citation counts by source because citation universes differ.

## Relevance, Verification, and Screening

Keep three independent axes:

- `relevance_tier`: concept-based topical role such as direct evidence, method support, field background, or broad background;
- `verification_level`: provenance state `V1`–`V4`;
- `screening_status`: `unscreened`, title decision, abstract decision, full-text decision, or excluded with reason.

Verification levels:

- **V1:** present in searched WoS or Scopus records and carrying a stable DOI;
- **V2:** confirmed on a publisher platform with a DOI but absent from the searched structured-core records;
- **V3:** thesis, working paper, report, preprint, or repository version; keep separately as gray literature;
- **V4:** auxiliary or Scholar candidate still requiring structured-index or publisher verification.

Crossref identity verification alone does not promote a record to V1 or V2. Verification status does not assess study design, statistical validity, or claim strength.

## Screening Counts

Use distinct denominators:

- `raw_record_count`: all records actually returned or imported across routes;
- `unique_record_count` / `screening_pool_count`: unique works after DOI, version, and compatible-title merging;
- `title_screened_count` and title exclusions;
- `abstract_screened_count` and abstract exclusions;
- `fulltext_assessed_count` and full-text exclusions with reasons;
- `included_evidence_count`: records that pass the declared inclusion criteria.

The script's `results` are a ranked preview, not an included-evidence set. `--limit` controls preview size; `--screening-output` preserves the complete deduplicated pool.

Report source contribution with two different measures:

- `source_record_counts`: unique works in which the source participated;
- `source_exclusive_contribution`: works discovered only by that source after global deduplication.

Do the same for query routes. Do not label membership counts as unique contribution.

## Domain Vocabulary

Load `domain-vocabulary.json` rather than copying every domain term into this file. Apply these interpretation rules:

- **Innovation management:** distinguish adoption, diffusion, implementation, and performance outcomes.
- **Operations management:** separate production, service operations, quality, inventory, and procurement mechanisms.
- **Supply chains:** distinguish supplier, logistics, procurement, disruption, resilience, and performance concepts.
- **Organizational management:** separate individual, team, leadership, and organizational-level outcomes.
- **Sustainability management:** distinguish disclosure, practices, performance, governance, and stakeholder mechanisms.
- **Causal methods:** keep identification strategies separate from the substantive phenomenon and context blocks.

## Native Query Pattern

Example concept contract:

```text
phenomenon = "technology adoption" OR "process innovation"
industry   = manufacturing OR services
method     = "panel data" OR "difference in differences"
```

WoS:

```text
TS=(("technology adoption" OR "process innovation")
AND (manufacturing OR services)
AND ("panel data" OR "difference in differences"))
```

Scopus:

```text
TITLE-ABS-KEY(("technology adoption" OR "process innovation")
AND (manufacturing OR services)
AND ("panel data" OR "difference in differences"))
```

Apply dates, languages, document types, and index boundaries as explicit filters and record them. Run a documented supplement without the method block when field evidence may not name the method in its title, abstract, or keywords.

## Coverage Audit and Stop Rules

Record:

- exact source-native query, date, filters, index scope, and export format;
- raw, unique, screened, eligible, and included counts;
- DOI, abstract, and full-text completeness;
- source and route membership, exclusivity, and overlap;
- benchmark recall and reasons for benchmark misses;
- Scholar fixed review depth and actual retrieval count;
- API failures, access limitations, languages, regions, and document types not covered;
- verification-level and exclusion-reason counts.

Stop only when:

- planned WoS, Scopus, and Scholar routes are completed or access failures are documented;
- benchmark recall is measured and misses are explained;
- two consecutive planned citation or semantic rounds add no new eligible core evidence;
- unresolved source, language, date, and gray-literature blind spots are reported.

Judge adequacy in two layers: `usable first version` and `complete coverage`. Never collapse them.

## API and Agent Discipline

- Treat provider documentation, live account state, and returned quota headers as authoritative; fixed published limits may change.
- Check source credentials and quota only when that route is about to run. Stop on provider quota exhaustion and record the failure.
- Keep Scholar calls bounded and preserve result, cited-by, and version-cluster identifiers. Stop on CAPTCHA; never rotate proxies or automate CAPTCHA handling.
- Use structured core queries sparingly, batch public graph work, and retrieve abstracts or full text only for deduplicated shortlisted works.
- Use the script for retrieval, normalization, deduplication, version clustering, and first-pass scoring.
- Give agents disjoint batches at query audit, title, abstract, and full-text decision gates. Reuse canonical records so no agent rereads the same work unnecessarily.
- Do not query agent token, goal, or quota balances before searching. Manage token cost through staged screening and compact inputs.
