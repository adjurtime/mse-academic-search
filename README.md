# MSE Academic Search

A public, reusable Codex Skill for rigorous and auditable literature searches in Management Science and Engineering.

## What it does

- Designs source-native searches for Web of Science, Scopus, and Google Scholar.
- Uses OpenAlex for coverage auditing, Semantic Scholar for bounded expansion, and Crossref for DOI verification.
- Imports RIS, BibTeX, CSV, TSV, and JSON records.
- Deduplicates records while preserving every discovery source and query route.
- Separates topical relevance, verification level, and screening status.
- Produces ranked previews, complete screening pools, and search audit records.

The skill supports search design and evidence discovery. It does not claim that a search is exhaustive and does not automatically include papers as final evidence.

## Install

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-installer/scripts/install-skill-from-github.py" \
  --repo adjurtime/mse-academic-search \
  --path . \
  --name mse-academic-search
```

Open a new Codex task and invoke:

```text
$mse-academic-search
```

The bundled search script uses only the Python standard library.

## Example

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

Use `--wos-import ROUTE=PATH` for labeled Web of Science exports and `--import-file PATH` for other RIS, BibTeX, CSV, TSV, or JSON records.

## Optional API access

The script reads credentials from process environment variables only:

- `OPENALEX_API_KEY`
- `SEMANTIC_SCHOLAR_API_KEY`
- `ELSEVIER_API_KEY`
- `ELSEVIER_INSTTOKEN`
- `SERPAPI_API_KEY`
- `CROSSREF_MAILTO`
- `OPENALEX_MAILTO`

Do not commit credentials or local search outputs. `.env` files and common result artifacts are ignored by default.

## Validation

```bash
python3 -B -m unittest scripts/test_academic_search.py
```

The test suite is offline. CSV output neutralizes spreadsheet-formula prefixes, and compact terminal output removes control characters from untrusted metadata.

## License

No open-source license has been selected. Public visibility does not grant permission to copy, modify, or redistribute the repository.
