#!/usr/bin/env python3
"""Multi-source search and audit for Management Science and Engineering.

The script uses only the Python standard library. It searches public scholarly
APIs, imports WoS/Scholar citation exports, deduplicates records, ranks them by
concept coverage, and reports an auditable search ledger.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import html as html_lib
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
SCIENCEDIRECT_API = "https://api.elsevier.com/content/search/sciencedirect"
SCOPUS_SEARCH_API = "https://api.elsevier.com/content/search/scopus"
SCOPUS_ABSTRACT_API = "https://api.elsevier.com/content/abstract/doi"
SERPAPI_SCHOLAR_API = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_API = "https://serpapi.com/account.json"
DEFAULT_MAILTO = "mse-academic-search@users.noreply.github.com"
DEFAULT_VOCABULARY = Path(__file__).resolve().parent.parent / "references" / "domain-vocabulary.json"
MODE_DEFAULTS = {
    "quick": {
        "per_query": 25, "limit": 25, "variants": 5,
        "scholar_depth": 20, "scholar_routes": 2, "scholar_budget": 2,
    },
    "comprehensive": {
        "per_query": 100, "limit": 100, "variants": 24,
        "scholar_depth": 40, "scholar_routes": 4, "scholar_budget": 8,
    },
    "deep": {
        "per_query": 200, "limit": 200, "variants": 24,
        "scholar_depth": 100, "scholar_routes": 5, "scholar_budget": 25,
    },
}

RESEARCH_PROFILES = {
    "legacy": {
        "default_sources": ["openalex", "crossref"],
        "description": "Backward-compatible topic discovery",
    },
    "rigorous": {
        "default_sources": [
            "scopus", "google-scholar", "openalex",
            "semantic-scholar", "crossref",
        ],
        "description": "Dual-index plan, parallel Scholar recall, and bounded auxiliary audit",
    },
}

SOURCE_ROLES = {
    "wos": {
        "stage": "core_discovery",
        "role": "structured_core",
        "execution": "external_search_then_import",
    },
    "scopus": {
        "stage": "core_discovery",
        "role": "structured_core",
        "execution": "api",
    },
    "google-scholar": {
        "stage": "core_discovery",
        "role": "broad_discovery_core",
        "execution": "serpapi",
    },
    "sciencedirect": {
        "stage": "publisher_depth",
        "role": "publisher_platform_depth",
        "execution": "api_metadata_and_abstract_support",
    },
    "openalex": {
        "stage": "coverage_audit",
        "role": "coverage_audit",
        "execution": "api",
    },
    "semantic-scholar": {
        "stage": "semantic_expansion",
        "role": "semantic_expansion",
        "execution": "api",
    },
    "crossref": {
        "stage": "identity_verification",
        "role": "identifier_verification",
        "execution": "api_after_deduplication",
    },
}

STAGE_ORDER = [
    "core_discovery",
    "publisher_depth",
    "coverage_audit",
    "semantic_expansion",
    "legacy_discovery",
    "identity_verification",
]

SOURCE_METADATA_PRIORITY = {
    "wos": 90,
    "scopus": 85,
    "sciencedirect": 80,
    "crossref": 75,
    "google-scholar": 60,
    "openalex": 50,
    "semantic-scholar": 45,
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "of", "on", "or", "the", "to", "using", "via",
    "with", "within", "study", "analysis", "research",
    "effect", "effects", "impact", "impacts", "relationship", "empirical",
}


class SearchError(RuntimeError):
    pass


class QuotaExceeded(SearchError):
    pass


class RequestPacer:
    def __init__(self, requests_per_second):
        self.interval = 1.0 / requests_per_second
        self.next_request = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_request - now)
            if delay:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.interval


REQUEST_PACERS = {
    "sciencedirect-search": RequestPacer(1.5),
    "scopus-search": RequestPacer(6.0),
    "scopus-abstract": RequestPacer(6.0),
    "google-scholar": RequestPacer(0.2),
    "crossref-verify": RequestPacer(5.0),
}
API_LIMITS = {}
API_LIMITS_LOCK = threading.Lock()


def elsevier_api_key():
    return os.environ.get("ELSEVIER_API_KEY", "").strip()


def serpapi_api_key():
    return os.environ.get("SERPAPI_API_KEY", "").strip()


def semantic_scholar_api_key():
    return os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()


def elsevier_headers():
    api_key = elsevier_api_key()
    if not api_key:
        raise SearchError(
            "Elsevier API key is unavailable; set ELSEVIER_API_KEY"
        )
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
    }
    insttoken = os.environ.get("ELSEVIER_INSTTOKEN")
    if insttoken:
        headers["X-ELS-Insttoken"] = insttoken
    return headers


def normalize_doi(value):
    if not value:
        return ""
    doi = html_lib.unescape(str(value)).strip().lower()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi)
    doi = doi.strip("{}[]() <>\t\r\n.,;:")
    return doi


def strip_markup(value):
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def normalize_text(value):
    text = unicodedata.normalize("NFKD", strip_markup(value)).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def meaningful_tokens(value):
    return {
        token for token in normalize_text(value).split()
        if len(token) > 1 and token not in STOPWORDS
    }


def clean_query(value):
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"\bM\s*&\s*A\b", "mergers acquisitions", text, flags=re.I)
    text = re.sub(r"[\"'()*]", " ", text)
    text = text.replace("-", " ")
    text = re.sub(r"\b(?:AND|OR|NOT)\b", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def clean_concept_term(value):
    text = html_lib.unescape(str(value or ""))
    text = re.sub(r"\bM\s*&\s*A\b", "mergers acquisitions", text, flags=re.I)
    text = text.strip().strip("\"'() ")
    return re.sub(r"\s+", " ", text).strip()


def first_value(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def parse_int(value, default=0):
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group()) if match else default


def rate_limit_snapshot():
    with API_LIMITS_LOCK:
        return {key: dict(value) for key, value in sorted(API_LIMITS.items())}


def record_rate_limit(rate_key, headers, status_code):
    if not rate_key:
        return
    values = {
        "http_status": status_code,
        "limit": parse_int(headers.get("X-RateLimit-Limit"), None),
        "remaining": parse_int(headers.get("X-RateLimit-Remaining"), None),
        "reset_epoch": parse_int(headers.get("X-RateLimit-Reset"), None),
        "provider_status": headers.get("X-ELS-Status") or "",
    }
    if values["reset_epoch"]:
        values["reset_at"] = dt.datetime.fromtimestamp(
            values["reset_epoch"], tz=dt.timezone.utc
        ).isoformat()
    with API_LIMITS_LOCK:
        current = API_LIMITS.setdefault(rate_key, {})
        current.update({key: value for key, value in values.items() if value not in (None, "")})


def serpapi_account_status(timeout=30, retries=1, stage="before"):
    data = request_json(
        SERPAPI_ACCOUNT_API,
        {"api_key": serpapi_api_key()},
        timeout=timeout,
        retries=retries,
    )
    status = {
        "plan": data.get("plan_name") or "",
        "monthly_limit": parse_int(data.get("searches_per_month"), None),
        "remaining": parse_int(data.get("total_searches_left"), None),
        "used": parse_int(data.get("this_month_usage"), None),
        "hourly_limit": parse_int(data.get("account_rate_limit_per_hour"), None),
        "renewal_date": data.get("plan_renewal_date") or "",
    }
    with API_LIMITS_LOCK:
        account = API_LIMITS.setdefault("serpapi-account", {})
        for key in ("plan", "monthly_limit", "hourly_limit", "renewal_date"):
            if status[key] not in (None, ""):
                account[key] = status[key]
        for key in ("remaining", "used"):
            if status[key] is not None:
                account[f"{key}_{stage}"] = status[key]
    return status


def request_json(url, params=None, headers=None, timeout=30, retries=1, rate_key=None):
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = f"{url}?{query}" if query else url
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "mse-academic-search/1.0",
    }
    req_headers.update(headers or {})

    for attempt in range(retries + 1):
        pacer = REQUEST_PACERS.get(rate_key)
        if pacer:
            pacer.wait()
        request = urllib.request.Request(full_url, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                record_rate_limit(rate_key, response.headers, response.status)
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            record_rate_limit(rate_key, exc.headers, exc.code)
            provider_status = (exc.headers.get("X-ELS-Status") or "").upper()
            if exc.code == 429 and "QUOTA_EXCEEDED" in provider_status:
                reset_at = rate_limit_snapshot().get(rate_key, {}).get("reset_at", "the provider reset time")
                raise QuotaExceeded(f"weekly quota exhausted for {rate_key}; resets at {reset_at}") from exc
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < retries:
                delay = min(parse_int(exc.headers.get("Retry-After"), 1), 60)
                time.sleep(max(delay, 1))
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:240]
            raise SearchError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt < retries:
                time.sleep(attempt + 1)
                continue
            raise SearchError(f"request failed for {url}: {exc}") from exc


def reconstruct_openalex_abstract(index):
    if not index:
        return ""
    positioned = []
    for word, positions in index.items():
        positioned.extend((position, word) for position in positions)
    positioned.sort()
    return " ".join(word for _, word in positioned)


def common_record(title, doi, authors, year, publication_date, citations,
                  journal, abstract, url, work_type, language, source,
                  query, rank, returned):
    rank_score = max(0.0, 1.0 - (rank - 1) / max(returned, 1))
    citation_count = parse_int(citations)
    return {
        "title": strip_markup(title),
        "doi": normalize_doi(doi),
        "authors": [strip_markup(author) for author in (authors or []) if strip_markup(author)],
        "year": parse_int(year, None),
        "publication_date": str(publication_date or ""),
        "cited_by_count": citation_count,
        "citation_counts_by_source": {source: citation_count},
        "journal": strip_markup(journal),
        "abstract": strip_markup(abstract),
        "abstract_kind": "abstract" if strip_markup(abstract) else "missing",
        "url": str(url or ""),
        "work_type": str(work_type or ""),
        "language": str(language or ""),
        "sources": [source],
        "canonical_source": source,
        "verification_sources": [],
        "screening_status": "unscreened",
        "hits": [{
            "source": source,
            "query": query,
            "rank": rank,
            "rank_score": round(rank_score, 6),
        }],
    }


def search_openalex(query, limit, year_from, year_to, doc_type, language,
                    mailto, timeout, retries):
    records = []
    cursor = "*"
    total = 0
    type_map = {"journal-article": "article", "review": "review", "book-chapter": "book-chapter"}

    while len(records) < limit:
        page_size = min(200, limit - len(records))
        filters = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if doc_type:
            filters.append(f"type:{type_map.get(doc_type, doc_type)}")
        if language:
            filters.append(f"language:{language}")

        params = {
            "search": clean_query(query),
            "per_page": page_size,
            "cursor": cursor,
            "mailto": mailto,
        }
        api_key = os.environ.get("OPENALEX_API_KEY")
        if api_key:
            params["api_key"] = api_key
        if filters:
            params["filter"] = ",".join(filters)

        data = request_json(OPENALEX_API, params, timeout=timeout, retries=retries)
        total = (data.get("meta") or {}).get("count", total) or total
        works = data.get("results") or []
        if not works:
            break

        for work in works:
            authors = [
                ((item.get("author") or {}).get("display_name") or "")
                for item in (work.get("authorships") or [])
            ]
            location = work.get("primary_location") or {}
            source = location.get("source") or {}
            records.append(common_record(
                work.get("title") or work.get("display_name"),
                work.get("doi"),
                authors,
                work.get("publication_year"),
                work.get("publication_date"),
                work.get("cited_by_count"),
                source.get("display_name"),
                reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
                work.get("doi") or work.get("id"),
                work.get("type"),
                work.get("language"),
                "openalex",
                query,
                len(records) + 1,
                limit,
            ))
            if len(records) >= limit:
                break

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return records, total


def crossref_year(item):
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            return parts[0][0]
    return None


def search_crossref(query, limit, year_from, year_to, doc_type, _language,
                    mailto, timeout, retries):
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if doc_type:
        filters.append(f"type:{doc_type}")
    params = {
        "query.bibliographic": clean_query(query),
        "rows": min(limit, 1000),
        "mailto": mailto,
    }
    if filters:
        params["filter"] = ",".join(filters)

    data = request_json(CROSSREF_API, params, timeout=timeout, retries=retries)
    message = data.get("message") or {}
    items = message.get("items") or []
    records = []
    for rank, item in enumerate(items, 1):
        authors = []
        for author in item.get("author") or []:
            name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
            if name:
                authors.append(name)
        year = crossref_year(item)
        records.append(common_record(
            first_value(item.get("title")),
            item.get("DOI"),
            authors,
            year,
            year,
            item.get("is-referenced-by-count"),
            first_value(item.get("container-title")),
            item.get("abstract"),
            item.get("URL"),
            item.get("type"),
            item.get("language"),
            "crossref",
            query,
            rank,
            max(len(items), 1),
        ))
    return records, message.get("total-results", 0)


def fetch_crossref_metadata(doi, mailto, timeout=30, retries=1):
    normalized = normalize_doi(doi)
    if not normalized:
        raise SearchError("DOI is required for Crossref verification")
    endpoint = f"{CROSSREF_API}/{urllib.parse.quote(normalized, safe='')}"
    data = request_json(
        endpoint,
        {"mailto": mailto},
        timeout=timeout,
        retries=retries,
        rate_key="crossref-verify",
    )
    item = data.get("message") or {}
    if not item.get("DOI"):
        raise SearchError(f"Crossref returned no DOI metadata for {normalized}")
    authors = []
    for author in item.get("author") or []:
        name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
        if name:
            authors.append(name)
    year = crossref_year(item)
    record = common_record(
        first_value(item.get("title")),
        item.get("DOI"),
        authors,
        year,
        year,
        item.get("is-referenced-by-count"),
        first_value(item.get("container-title")),
        item.get("abstract"),
        item.get("URL"),
        item.get("type"),
        item.get("language"),
        "crossref",
        f"doi:{normalized}",
        1,
        1,
    )
    record["crossref_verified"] = True
    record["verification_sources"] = ["crossref"]
    return record


def search_semantic_scholar(query, limit, year_from, year_to, doc_type, _language,
                            _mailto, timeout, retries):
    records = []
    offset = 0
    total = 0
    headers = {}
    api_key = semantic_scholar_api_key()
    if api_key:
        headers["x-api-key"] = api_key

    while len(records) < min(limit, 1000):
        page_size = min(100, limit - len(records))
        params = {
            "query": clean_query(query),
            "offset": offset,
            "limit": page_size,
            "fields": "title,abstract,authors,year,venue,citationCount,publicationDate,publicationTypes,externalIds,url",
        }
        if year_from or year_to:
            params["year"] = f"{year_from or ''}-{year_to or ''}".strip("-")
        if doc_type == "journal-article":
            params["publicationTypes"] = "JournalArticle"

        data = request_json(SEMANTIC_SCHOLAR_API, params, headers=headers,
                            timeout=timeout, retries=retries)
        total = data.get("total", total) or total
        papers = data.get("data") or []
        if not papers:
            break
        for paper in papers:
            external_ids = paper.get("externalIds") or {}
            records.append(common_record(
                paper.get("title"),
                external_ids.get("DOI"),
                [author.get("name", "") for author in paper.get("authors") or []],
                paper.get("year"),
                paper.get("publicationDate"),
                paper.get("citationCount"),
                paper.get("venue"),
                paper.get("abstract"),
                paper.get("url"),
                ",".join(paper.get("publicationTypes") or []),
                "",
                "semantic-scholar",
                query,
                len(records) + 1,
                limit,
            ))
        offset += len(papers)
        if not data.get("next") or offset >= 1000:
            break
        if not api_key:
            time.sleep(1.05)
    return records, total


def sciencedirect_authors(item):
    authors = []
    author_data = (item.get("authors") or {}).get("author") or []
    if not isinstance(author_data, list):
        author_data = [author_data]
    for author in author_data:
        name = author.get("$") if isinstance(author, dict) else author
        if name:
            authors.append(name)
    creators = item.get("dc:creator") or []
    if not isinstance(creators, list):
        creators = [creators]
    for creator in creators:
        name = creator.get("$") if isinstance(creator, dict) else creator
        if name and name not in authors:
            authors.append(name)
    return authors


def sciencedirect_link(item):
    links = item.get("link") or []
    if not isinstance(links, list):
        links = [links]
    for preferred in ("scidir", "self"):
        for link in links:
            if not isinstance(link, dict):
                continue
            if (link.get("@ref") or link.get("rel")) == preferred:
                return link.get("@href") or link.get("href") or ""
    return item.get("prism:url") or ""


def search_sciencedirect(query, limit, year_from, year_to, doc_type, language,
                         _mailto, timeout, retries):
    headers = elsevier_headers()

    records = []
    start = 0
    total = 0
    while len(records) < min(limit, 6000):
        count = min(200, limit - len(records))
        data = request_json(
            SCIENCEDIRECT_API,
            {
                "query": query.strip(),
                "count": count,
                "start": start,
                "view": "STANDARD",
                "httpAccept": "application/json",
            },
            headers=headers,
            timeout=timeout,
            retries=retries,
            rate_key="sciencedirect-search",
        )
        search_results = data.get("search-results") or {}
        total = parse_int(search_results.get("opensearch:totalResults"), total)
        entries = search_results.get("entry") or []
        if not isinstance(entries, list):
            entries = [entries]
        if not entries:
            break

        for item in entries:
            if item.get("error") or not item.get("dc:title"):
                continue
            year = parse_int(item.get("prism:coverDate"), None)
            if year_from and year and year < year_from:
                continue
            if year_to and year and year > year_to:
                continue
            item_type = item.get("prism:publicationType") or item.get("prism:aggregationType") or ""
            if doc_type == "journal-article" and item_type and "journal" not in str(item_type).lower():
                continue
            item_language = item.get("dc:language") or ""
            if language and item_language and not str(item_language).lower().startswith(language.lower()):
                continue
            identifier = item.get("prism:doi") or item.get("dc:identifier") or ""
            records.append(common_record(
                item.get("dc:title"),
                identifier,
                sciencedirect_authors(item),
                year,
                item.get("prism:coverDate"),
                0,
                item.get("prism:publicationName"),
                item.get("dc:description"),
                sciencedirect_link(item),
                item_type,
                item_language,
                "sciencedirect",
                query,
                len(records) + 1,
                limit,
            ))
            if len(records) >= limit:
                break
        start += len(entries)
        if start >= total or not entries:
            break
    return records, total


def scopus_authors(item):
    authors = []
    author_data = item.get("author") or []
    if not isinstance(author_data, list):
        author_data = [author_data]
    for author in author_data:
        if not isinstance(author, dict):
            name = str(author)
        else:
            name = author.get("authname") or author.get("indexed-name") or " ".join(
                part for part in (author.get("given-name"), author.get("surname")) if part
            )
        if name and name not in authors:
            authors.append(name)
    creator = item.get("dc:creator") or ""
    if creator and creator not in authors:
        authors.append(creator)
    return authors


def scopus_link(item):
    links = item.get("link") or []
    if not isinstance(links, list):
        links = [links]
    for preferred in ("scopus", "self"):
        for link in links:
            if isinstance(link, dict) and (link.get("@ref") or link.get("rel")) == preferred:
                return link.get("@href") or link.get("href") or ""
    return item.get("prism:url") or ""


def build_scopus_query(query, year_from, year_to, doc_type):
    clauses = [f"TITLE-ABS-KEY({query.strip()})"]
    if year_from:
        clauses.append(f"PUBYEAR > {year_from - 1}")
    if year_to:
        clauses.append(f"PUBYEAR < {year_to + 1}")
    type_map = {"journal-article": "ar", "review": "re", "book-chapter": "ch"}
    if doc_type:
        clauses.append(f"DOCTYPE({type_map[doc_type]})")
    return " AND ".join(clauses)


def search_scopus(query, limit, year_from, year_to, doc_type, language,
                  _mailto, timeout, retries):
    records = []
    start = 0
    total = 0
    api_query = build_scopus_query(query, year_from, year_to, doc_type)
    while len(records) < min(limit, 5000):
        count = min(200, limit - len(records))
        data = request_json(
            SCOPUS_SEARCH_API,
            {
                "query": api_query,
                "count": count,
                "start": start,
                "view": "STANDARD",
                "httpAccept": "application/json",
            },
            headers=elsevier_headers(),
            timeout=timeout,
            retries=retries,
            rate_key="scopus-search",
        )
        search_results = data.get("search-results") or {}
        total = parse_int(search_results.get("opensearch:totalResults"), total)
        entries = search_results.get("entry") or []
        if not isinstance(entries, list):
            entries = [entries]
        if not entries:
            break
        for item in entries:
            if item.get("error") or not item.get("dc:title"):
                continue
            item_language = item.get("dc:language") or ""
            if language and item_language and not str(item_language).lower().startswith(language.lower()):
                continue
            records.append(common_record(
                item.get("dc:title"),
                item.get("prism:doi"),
                scopus_authors(item),
                item.get("prism:coverDate"),
                item.get("prism:coverDate"),
                item.get("citedby-count"),
                item.get("prism:publicationName"),
                item.get("dc:description"),
                scopus_link(item),
                item.get("subtypeDescription") or item.get("subtype"),
                item_language,
                "scopus",
                query,
                len(records) + 1,
                limit,
            ))
            if len(records) >= limit:
                break
        start += len(entries)
        if start >= total:
            break
    return records, total


def scholar_publication_details(item):
    publication = item.get("publication_info") or {}
    authors = [
        author.get("name", "")
        for author in publication.get("authors") or []
        if isinstance(author, dict)
    ]
    summary = publication.get("summary") or ""
    year_matches = re.findall(r"\b(?:19|20)\d{2}\b", summary)
    parts = [part.strip() for part in summary.split(" - ")]
    journal = parts[1] if len(parts) > 1 else ""
    return authors, (int(year_matches[-1]) if year_matches else None), journal


def doi_from_values(*values):
    for value in values:
        match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", str(value or ""), flags=re.I)
        if match:
            return normalize_doi(match.group())
    return ""


def search_google_scholar(query, limit, year_from, year_to, doc_type, language,
                          _mailto, timeout, retries):
    api_key = serpapi_api_key()
    if not api_key:
        raise SearchError(
            "SerpApi key is unavailable; set SERPAPI_API_KEY"
        )
    base_params = {
        "engine": "google_scholar",
        "api_key": api_key,
        "hl": "en",
        "as_vis": 1,
    }
    citation_match = re.fullmatch(r"cites:(\d+)", query.strip())
    if citation_match:
        base_params["cites"] = citation_match.group(1)
    else:
        base_params["q"] = query.strip()
    if year_from:
        base_params["as_ylo"] = year_from
    if year_to:
        base_params["as_yhi"] = year_to
    if doc_type == "review":
        base_params["as_rr"] = 1

    records = []
    total = 0
    start = 0
    while len(records) < limit:
        page_size = min(20, limit - len(records))
        params = dict(base_params, num=page_size, start=start)
        data = request_json(
            SERPAPI_SCHOLAR_API,
            params,
            timeout=timeout,
            retries=retries,
            rate_key="google-scholar",
        )
        if data.get("error"):
            raise SearchError(f"SerpApi Google Scholar error: {data['error']}")
        total = parse_int(
            (data.get("search_information") or {}).get("total_results"),
            total,
        )
        items = data.get("organic_results") or []
        if not items:
            break
        for item in items:
            authors, year, journal = scholar_publication_details(item)
            inline_links = item.get("inline_links") or {}
            cited_by = inline_links.get("cited_by") or {}
            versions = inline_links.get("versions") or {}
            snippet = item.get("snippet") or ""
            link = item.get("link") or ""
            resource_links = [
                resource.get("link", "")
                for resource in item.get("resources") or []
                if isinstance(resource, dict)
            ]
            record = common_record(
                item.get("title"),
                doi_from_values(item.get("doi"), link, snippet, *resource_links),
                authors,
                year,
                year,
                cited_by.get("total"),
                journal,
                snippet,
                link,
                "search-result-snippet",
                "",
                "google-scholar",
                query,
                len(records) + 1,
                limit,
            )
            record["abstract_kind"] = "search_snippet"
            record["scholar_result_id"] = item.get("result_id") or ""
            record["scholar_cites_id"] = str(cited_by.get("cites_id") or "")
            record["scholar_cluster_id"] = str(versions.get("cluster_id") or "")
            record["scholar_versions_count"] = parse_int(versions.get("total"), 0)
            records.append(record)
            if len(records) >= limit:
                break
        pagination = data.get("serpapi_pagination")
        if pagination is not None and not (pagination or {}).get("next"):
            break
        if len(items) < page_size:
            break
        start += page_size
    return records, total


def fetch_scopus_abstract(doi, timeout=30, retries=1):
    normalized = normalize_doi(doi)
    if not normalized:
        raise SearchError("DOI is required for abstract retrieval")
    endpoint = f"{SCOPUS_ABSTRACT_API}/{urllib.parse.quote(normalized, safe='')}"
    data = request_json(
        endpoint,
        {"view": "META_ABS", "httpAccept": "application/json"},
        headers=elsevier_headers(),
        timeout=timeout,
        retries=retries,
        rate_key="scopus-abstract",
    )
    response = data.get("abstracts-retrieval-response") or {}
    core = response.get("coredata") or {}
    return {
        "abstract": strip_markup(core.get("dc:description")),
        "cited_by_count": parse_int(core.get("citedby-count")),
        "url": core.get("prism:url") or "",
    }


def enrich_abstracts(records, limit, timeout, retries, elsevier_only=True):
    candidates = [
        record for record in records
        if record.get("doi")
        and not record.get("abstract")
        and (
            not elsevier_only
            or {"sciencedirect", "scopus"}.intersection(record.get("sources", []))
        )
    ][:limit]
    report = {
        "mode": "scopus_meta_abs",
        "attempted": len(candidates),
        "succeeded": 0,
        "missing": 0,
        "errors": [],
    }

    for record in candidates:
        try:
            metadata = fetch_scopus_abstract(record["doi"], timeout, retries)
        except QuotaExceeded as exc:
            report["errors"].append({"doi": record["doi"], "error": str(exc)})
            break
        except SearchError as exc:
            report["errors"].append({"doi": record["doi"], "error": str(exc)})
            continue
        abstract = metadata.get("abstract", "")
        if not abstract:
            report["missing"] += 1
            continue
        record["abstract"] = abstract
        record["abstract_kind"] = "abstract"
        record["cited_by_count"] = max(
            record.get("cited_by_count", 0),
            metadata.get("cited_by_count", 0),
        )
        if not record.get("url") and metadata.get("url"):
            record["url"] = metadata["url"]
        report["succeeded"] += 1
    return report


SOURCE_FUNCTIONS = {
    "openalex": search_openalex,
    "crossref": search_crossref,
    "semantic-scholar": search_semantic_scholar,
    "sciencedirect": search_sciencedirect,
    "scopus": search_scopus,
    "google-scholar": search_google_scholar,
}


def parse_concept(spec):
    if "=" not in spec:
        raise ValueError(f"concept must use name=term1|term2 syntax: {spec}")
    label, raw_terms = spec.split("=", 1)
    label = label.strip()
    if ":" in label:
        role, name = label.split(":", 1)
    else:
        name = label
        role = "method" if any(key in label.lower() for key in ("method", "model", "network", "theory")) else "topic"
    terms = []
    for term in raw_terms.split("|"):
        cleaned = clean_concept_term(term)
        if cleaned and cleaned.casefold() not in {item.casefold() for item in terms}:
            terms.append(cleaned)
    if not terms:
        raise ValueError(f"concept has no terms: {spec}")
    return {"role": role.strip() or "topic", "name": name.strip() or role.strip(), "terms": terms}


def load_vocabulary(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def term_present(query, term):
    query_norm = normalize_text(query)
    term_norm = normalize_text(term)
    if term_norm in query_norm:
        return True
    tokens = meaningful_tokens(term)
    return bool(tokens) and tokens.issubset(meaningful_tokens(query))


def detect_domain(query, vocabulary):
    best_domain = "general"
    best_score = 0
    for name, profile in (vocabulary.get("profiles") or {}).items():
        if name in {"auto", "general"}:
            continue
        score = sum(1 for trigger in profile.get("triggers", []) if term_present(query, trigger))
        if score > best_score:
            best_domain, best_score = name, score
    return best_domain


def automatic_concepts(query, domain, vocabulary):
    group_defs = vocabulary.get("groups") or {}
    selected = []
    seen = set()

    for key, group in group_defs.items():
        if any(term_present(query, term) for term in group.get("terms", [])):
            selected.append(parse_concept(f"{group['name']}={'|'.join(group['terms'])}"))
            seen.add(key)

    if domain not in {"auto", "general"}:
        for key in (vocabulary.get("profiles", {}).get(domain, {}).get("groups") or []):
            if key not in seen and key in group_defs:
                group = group_defs[key]
                selected.append(parse_concept(f"{group['name']}={'|'.join(group['terms'])}"))
                seen.add(key)

    if selected and query:
        covered_tokens = set()
        for concept in selected:
            for term in concept["terms"]:
                if term_present(query, term):
                    covered_tokens.update(meaningful_tokens(term))
        residual = [
            token for token in normalize_text(query).split()
            if token not in covered_tokens and token not in STOPWORDS and len(token) > 1
        ]
        if residual:
            selected.append({
                "role": "context",
                "name": "anchor",
                "terms": [" ".join(dict.fromkeys(residual))],
            })

    if not selected and query:
        selected.append({"role": "topic", "name": "query", "terms": [clean_query(query)]})
    return selected


def generate_query_variants(base_query, concepts, mode, max_variants):
    variants = []
    seen = set()

    def add(parts):
        query = clean_query(" ".join(part for part in parts if part))
        key = normalize_text(query)
        if query and key not in seen and len(variants) < max_variants:
            variants.append(query)
            seen.add(key)

    if base_query:
        add([base_query])
    if not concepts:
        return variants

    canonical = [concept["terms"][0] for concept in concepts]
    add(canonical)

    if mode in {"comprehensive", "deep"} and len(concepts) >= 3:
        for omitted in range(len(concepts)):
            add([term for index, term in enumerate(canonical) if index != omitted])

        method_indexes = [
            index for index, concept in enumerate(concepts)
            if concept.get("role") == "method"
        ]
        for omitted in method_indexes:
            remaining = [concept for index, concept in enumerate(concepts) if index != omitted]
            pools = [concept["terms"][:4] for concept in remaining]
            for combination in itertools.product(*pools):
                add(list(combination))
                if len(variants) >= max_variants:
                    return variants

    for index, concept in enumerate(concepts):
        for alternative in concept["terms"][1:4]:
            candidate = list(canonical)
            candidate[index] = alternative
            add(candidate)

    if mode in {"comprehensive", "deep"} and len(variants) < max_variants:
        pools = [concept["terms"][:3] for concept in concepts]
        for combination in itertools.product(*pools):
            add(list(combination))
            if len(variants) >= max_variants:
                break

    return variants


def wos_term(term):
    escaped = str(term).replace('"', '\\"')
    return f'"{escaped}"' if " " in escaped else escaped


def build_wos_queries(concepts):
    if not concepts:
        return []

    def render(selected):
        blocks = []
        for concept in selected:
            alternatives = " OR ".join(wos_term(term) for term in concept["terms"])
            blocks.append(f"({alternatives})")
        return f"TS=({' AND '.join(blocks)})"

    queries = [{"route": "core_intersection", "query": render(concepts)}]
    if len(concepts) >= 3:
        for omitted, concept in enumerate(concepts):
            selected = [item for index, item in enumerate(concepts) if index != omitted]
            queries.append({
                "route": f"leave_out:{concept['name']}",
                "query": render(selected),
            })
    return queries


def render_boolean_query(concepts):
    blocks = []
    for concept in concepts:
        alternatives = " OR ".join(wos_term(term) for term in concept["terms"])
        blocks.append(f"({alternatives})")
    return " AND ".join(blocks)


def build_structured_routes(base_query, concepts, mode):
    routes = []
    if concepts:
        routes.append({
            "route": "core_intersection",
            "query": render_boolean_query(concepts),
            "purpose": "direct_evidence",
        })
        if mode in {"comprehensive", "deep"} and len(concepts) >= 3:
            ordered = sorted(
                enumerate(concepts),
                key=lambda item: (
                    item[1].get("role") not in {"method", "context", "outcome"},
                    item[0],
                ),
            )
            cap = 4 if mode == "comprehensive" else 6
            for omitted, concept in ordered:
                selected = [item for index, item in enumerate(concepts) if index != omitted]
                routes.append({
                    "route": f"leave_out:{concept['name']}",
                    "query": render_boolean_query(selected),
                    "purpose": "critical_supplement",
                })
                if len(routes) >= cap:
                    break
    elif base_query:
        routes.append({
            "route": "core_query",
            "query": clean_query(base_query),
            "purpose": "direct_evidence",
        })
    return routes


def scholar_term(term):
    value = str(term).strip()
    return f'"{value}"' if " " in value else value


def build_scholar_routes(base_query, concepts, mode, max_routes=None):
    routes = []
    seen = set()
    cap = max_routes or MODE_DEFAULTS[mode]["scholar_routes"]

    def add(name, query, purpose):
        key = str(query or "").strip().casefold()
        if key and key not in seen and len(routes) < cap:
            routes.append({"route": name, "query": str(query).strip(), "purpose": purpose})
            seen.add(key)

    add("natural_language", base_query, "broad_discovery")
    if concepts:
        primary = [scholar_term(concept["terms"][0]) for concept in concepts]
        add("concept_anchors", " ".join(primary), "broad_discovery")
        non_method = [
            scholar_term(concept["terms"][0])
            for concept in concepts
            if concept.get("role") != "method"
        ]
        if len(non_method) >= 2:
            add("field_without_method", " ".join(non_method), "field_recall")
        if mode in {"comprehensive", "deep"}:
            for index, concept in enumerate(concepts):
                if len(concept["terms"]) < 2:
                    continue
                alternative = list(primary)
                alternative[index] = scholar_term(concept["terms"][1])
                add(
                    f"synonym:{concept['name']}",
                    " ".join(alternative),
                    "terminology_recall",
                )
    return routes


def route_record(source, item):
    if isinstance(item, dict):
        route = dict(item)
    else:
        route = {"route": "legacy_variant", "query": str(item), "purpose": "topic_discovery"}
    role = SOURCE_ROLES.get(source, {})
    route.setdefault("source", source)
    route.setdefault("stage", role.get("stage", "discovery"))
    route.setdefault("purpose", role.get("role", "topic_discovery"))
    return route


def build_source_query_plan(base_query, concepts, generic_queries, mode, profile,
                            sources, scholar_route_cap=None):
    if profile == "legacy":
        plan = {
            "wos": [
                {
                    **item,
                    "source": "wos",
                    "stage": "core_discovery",
                    "purpose": (
                        "direct_evidence"
                        if item.get("route") == "core_intersection"
                        else "critical_supplement"
                    ),
                    "execution": "external_search_then_import",
                }
                for item in build_wos_queries(concepts)
            ],
        }
        for source in sources:
            routes = [route_record(source, query) for query in generic_queries]
            if source == "crossref":
                for route in routes:
                    route["stage"] = "legacy_discovery"
                    route["purpose"] = "legacy_topic_discovery"
            plan[source] = routes
        return plan

    structured = build_structured_routes(base_query, concepts, mode)
    scholar = build_scholar_routes(base_query, concepts, mode, scholar_route_cap)
    field_query = clean_query(" ".join(
        concept["terms"][0]
        for concept in concepts
        if concept.get("role") != "method"
    )) or clean_query(base_query)
    core_query = clean_query(base_query) or field_query
    plan = {
        "wos": [
            {
                **item,
                "query": f"TS=({item['query']})",
                "source": "wos",
                "stage": "core_discovery",
                "execution": "external_search_then_import",
            }
            for item in structured
        ],
    }
    for source in sources:
        if source == "scopus":
            items = structured
        elif source == "google-scholar":
            items = scholar
        elif source == "sciencedirect":
            cap = 1 if mode == "quick" else 2
            items = structured[:cap]
        elif source == "openalex":
            items = [{
                "route": "coverage_core",
                "query": core_query,
                "purpose": "coverage_gap_audit",
            }]
            if mode in {"comprehensive", "deep"} and field_query != core_query:
                items.append({
                    "route": "coverage_field",
                    "query": field_query,
                    "purpose": "regional_repository_language_gap",
                })
        elif source == "semantic-scholar":
            items = [{
                "route": "semantic_anchor",
                "query": core_query,
                "purpose": "semantic_anchor_expansion",
            }]
        elif source == "crossref":
            items = []
        else:
            items = []
        plan[source] = [route_record(source, item) for item in items if item.get("query")]
    return plan


def resolve_sources(value, profile, journals=None):
    source_text = str(value or "auto").strip().lower()
    if source_text == "none":
        return []
    if source_text == "all":
        return list(SOURCE_FUNCTIONS)
    if source_text == "auto":
        sources = list(RESEARCH_PROFILES[profile]["default_sources"])
        if profile == "rigorous" and journals and "sciencedirect" not in sources:
            sources.append("sciencedirect")
        return sources
    return [source.strip() for source in source_text.split(",") if source.strip()]


def effective_source_roles(profile):
    roles = {source: dict(details) for source, details in SOURCE_ROLES.items()}
    if profile == "legacy":
        roles["crossref"].update({
            "stage": "legacy_discovery",
            "role": "legacy_topic_discovery",
            "execution": "api_before_global_deduplication",
        })
    return roles


def parse_ris(path):
    records = []
    current = {}
    with Path(path).open("r", encoding="utf-8-sig", errors="replace") as handle:
        for raw_line in handle:
            match = re.match(r"^([A-Z0-9]{2})\s*-\s?(.*)$", raw_line.rstrip("\n"))
            if not match:
                continue
            tag, value = match.groups()
            if tag == "ER":
                if current:
                    records.append(current)
                current = {}
            else:
                current.setdefault(tag, []).append(value.strip())
    if current:
        records.append(current)

    source = f"import:{Path(path).name}"
    output = []
    for rank, item in enumerate(records, 1):
        output.append(common_record(
            first_value(item.get("TI") or item.get("T1")),
            first_value(item.get("DO")),
            item.get("AU") or item.get("A1") or [],
            first_value(item.get("PY") or item.get("Y1")),
            first_value(item.get("DA") or item.get("Y1")),
            first_value(item.get("TC")),
            first_value(item.get("JO") or item.get("JF") or item.get("T2")),
            " ".join(item.get("AB") or []),
            first_value(item.get("UR")),
            first_value(item.get("TY")),
            first_value(item.get("LA")),
            source,
            "import",
            rank,
            len(records),
        ))
    return output


def parse_bibtex_entries(text):
    entries = []
    position = 0
    while True:
        match = re.search(r"@(\w+)\s*([({])", text[position:], flags=re.I)
        if not match:
            break
        entry_type = match.group(1)
        open_char = match.group(2)
        close_char = "}" if open_char == "{" else ")"
        start = position + match.end()
        depth = 1
        cursor = start
        quoted = False
        escaped = False
        while cursor < len(text) and depth:
            char = text[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted and char == open_char:
                depth += 1
            elif not quoted and char == close_char:
                depth -= 1
            cursor += 1
        body = text[start:cursor - 1]
        comma = body.find(",")
        if comma >= 0:
            fields_text = body[comma + 1:]
            fields = {"entry_type": entry_type}
            index = 0
            while index < len(fields_text):
                key_match = re.search(r"([A-Za-z][\w-]*)\s*=\s*", fields_text[index:])
                if not key_match:
                    break
                key = key_match.group(1).lower()
                value_start = index + key_match.end()
                if value_start >= len(fields_text):
                    break
                opener = fields_text[value_start]
                if opener in "{\"":
                    closer = "}" if opener == "{" else '"'
                    depth = 1 if opener == "{" else 0
                    quoted_value = opener == '"'
                    value_end = value_start + 1
                    escaped_value = False
                    while value_end < len(fields_text):
                        char = fields_text[value_end]
                        if escaped_value:
                            escaped_value = False
                        elif char == "\\":
                            escaped_value = True
                        elif opener == "{" and char == "{":
                            depth += 1
                        elif opener == "{" and char == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        elif quoted_value and char == closer:
                            break
                        value_end += 1
                    value = fields_text[value_start + 1:value_end]
                    index = value_end + 1
                else:
                    value_end = fields_text.find(",", value_start)
                    if value_end < 0:
                        value_end = len(fields_text)
                    value = fields_text[value_start:value_end]
                    index = value_end + 1
                cleaned_value = re.sub(r"\\([&_#%{}])", r"\1", value)
                cleaned_value = cleaned_value.replace("~", " ")
                fields[key] = re.sub(r"[{}]", "", cleaned_value).strip()
            entries.append(fields)
        position = max(cursor, position + match.end())
    return entries


def parse_bibtex(path):
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    entries = parse_bibtex_entries(text)
    source = f"import:{Path(path).name}"
    records = []
    for rank, item in enumerate(entries, 1):
        author_text = item.get("author", "")
        authors = [part.strip() for part in re.split(r"\s+and\s+|\s*;\s*", author_text) if part.strip()]
        records.append(common_record(
            item.get("title"),
            item.get("doi"),
            authors,
            item.get("year"),
            item.get("date") or item.get("year"),
            item.get("times-cited") or item.get("times_cited") or item.get("cited"),
            item.get("journal") or item.get("booktitle"),
            item.get("abstract"),
            item.get("url"),
            item.get("entry_type"),
            item.get("language"),
            source,
            "import",
            rank,
            len(entries),
        ))
    return records


def normalized_row(row):
    return {normalize_text(key).replace(" ", "_"): value for key, value in row.items() if key}


def row_pick(row, *keys):
    for key in keys:
        value = row.get(normalize_text(key).replace(" ", "_"))
        if value not in (None, ""):
            return value
    return ""


def parse_csv_file(path):
    path = Path(path)
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if path.suffix.lower() == ".tsv" or sample.count("\t") > sample.count(",") else ","
        rows = [normalized_row(row) for row in csv.DictReader(handle, delimiter=delimiter)]

    source = f"import:{path.name}"
    records = []
    for rank, row in enumerate(rows, 1):
        authors_text = row_pick(row, "authors", "author full names", "au", "af")
        authors = [part.strip() for part in re.split(r"\s*;\s*|\s+and\s+", authors_text) if part.strip()]
        records.append(common_record(
            row_pick(row, "article title", "title", "ti"),
            row_pick(row, "doi", "di"),
            authors,
            row_pick(row, "publication year", "year", "py"),
            row_pick(row, "publication date", "date", "pd"),
            row_pick(row, "times cited wos core", "times cited", "cited by", "tc", "z9"),
            row_pick(row, "source title", "journal", "publication name", "so"),
            row_pick(row, "abstract", "ab"),
            row_pick(row, "url", "ut", "unique article identifier"),
            row_pick(row, "document type", "type", "dt"),
            row_pick(row, "language", "la"),
            source,
            "import",
            rank,
            len(rows),
        ))
    return records


def parse_json_file(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    items = data.get("results", []) if isinstance(data, dict) else data
    source = f"import:{Path(path).name}"
    records = []
    for rank, item in enumerate(items or [], 1):
        authors = item.get("authors") or []
        if isinstance(authors, str):
            authors = [part.strip() for part in re.split(r"\s*;\s*|\s+and\s+", authors) if part.strip()]
        records.append(common_record(
            item.get("title"), item.get("doi"), authors, item.get("year"),
            item.get("publication_date"), item.get("cited_by_count") or item.get("citations"),
            item.get("journal") or item.get("venue"), item.get("abstract"),
            item.get("url") or item.get("openalex_id"), item.get("work_type") or item.get("type"),
            item.get("language"), source, "import", rank, len(items),
        ))
    return records


def import_records(path):
    suffix = Path(path).suffix.lower()
    if suffix in {".ris", ".txt"}:
        return parse_ris(path)
    if suffix == ".bib":
        return parse_bibtex(path)
    if suffix in {".csv", ".tsv"}:
        return parse_csv_file(path)
    if suffix == ".json":
        return parse_json_file(path)
    raise ValueError(f"unsupported import format: {path}")


def source_metadata_priority(source):
    value = str(source or "")
    if value.startswith("import:"):
        return 65
    return SOURCE_METADATA_PRIORITY.get(value, 40)


def parse_wos_import_spec(spec, valid_routes):
    route, separator, path = str(spec).partition("=")
    if separator and route in valid_routes and path:
        return route, path
    return "", str(spec)


def relabel_import_records(records, source, origin, route=""):
    for record in records:
        old_sources = list(record.get("sources", []))
        record["sources"] = [source]
        record["canonical_source"] = source
        record["import_origin"] = str(origin)
        if route:
            record["import_route"] = route
        counts = record.get("citation_counts_by_source", {})
        count = max(counts.values(), default=record.get("cited_by_count", 0))
        record["citation_counts_by_source"] = {source: count}
        for hit in record.get("hits", []):
            hit["source"] = source
            hit["import_origin"] = str(origin)
            if route:
                hit["route"] = route
        if old_sources:
            record["original_import_label"] = old_sources[0]
    return records


def title_fallback_compatible(left, right):
    left_doi = normalize_doi(left.get("doi"))
    right_doi = normalize_doi(right.get("doi"))
    if left_doi and right_doi and left_doi != right_doi:
        return False
    left_year = parse_int(left.get("year"), None)
    right_year = parse_int(right.get("year"), None)
    if left_year and right_year and abs(left_year - right_year) > 1:
        return False
    left_authors = left.get("authors") or []
    right_authors = right.get("authors") or []
    if left_authors and right_authors:
        left_key = first_author_surname(left_authors[0])
        right_key = first_author_surname(right_authors[0])
        if left_key and right_key and left_key != right_key:
            return False
    return True


def first_author_surname(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "," in raw:
        surname = normalize_text(raw.split(",", 1)[0])
        return surname.split()[-1] if surname else ""
    tokens = normalize_text(raw).split()
    if not tokens:
        return ""
    if len(tokens) > 1 and len(tokens[-1]) <= 2 and len(tokens[0]) > 2:
        return tokens[0]
    return tokens[-1]


def merge_record(target, incoming, include_discovery_source=True):
    target_source = target.get("canonical_source") or first_value(target.get("sources"))
    incoming_source = incoming.get("canonical_source") or first_value(incoming.get("sources"))
    target_priority = source_metadata_priority(target_source)
    incoming_priority = source_metadata_priority(incoming_source)

    for field in (
        "title", "journal", "url", "work_type", "language", "publication_date",
        "scholar_result_id", "scholar_cites_id", "scholar_cluster_id",
    ):
        if not target.get(field) and incoming.get(field):
            target[field] = incoming[field]
    if not target.get("doi") and incoming.get("doi"):
        target["doi"] = incoming["doi"]
    if not target.get("year") and incoming.get("year"):
        target["year"] = incoming["year"]
    if incoming_priority > target_priority:
        for field in ("title", "journal", "url", "work_type", "language", "publication_date", "year"):
            if incoming.get(field):
                target[field] = incoming[field]
        if incoming.get("authors"):
            target["authors"] = incoming["authors"]
        target["canonical_source"] = incoming_source

    target_doi = normalize_doi(target.get("doi"))
    incoming_doi = normalize_doi(incoming.get("doi"))
    if target_doi and incoming_doi and target_doi != incoming_doi:
        target["alternate_dois"] = sorted(
            set(target.get("alternate_dois", [])) | {incoming_doi}
        )
    abstract_priority = {"missing": 0, "search_snippet": 1, "abstract": 2}
    incoming_kind = incoming.get("abstract_kind", "missing")
    target_kind = target.get("abstract_kind", "missing")
    if (
        abstract_priority.get(incoming_kind, 0) > abstract_priority.get(target_kind, 0)
        or (
            incoming_kind == target_kind
            and len(incoming.get("abstract", "")) > len(target.get("abstract", ""))
        )
    ):
        target["abstract"] = incoming["abstract"]
        target["abstract_kind"] = incoming_kind
    if (
        incoming_priority <= target_priority
        and len(incoming.get("authors", [])) > len(target.get("authors", []))
    ):
        target["authors"] = incoming["authors"]
    versions_count = max(
        target.get("scholar_versions_count", 0),
        incoming.get("scholar_versions_count", 0),
    )
    if versions_count:
        target["scholar_versions_count"] = versions_count
    target_counts = target.setdefault("citation_counts_by_source", {})
    for source, count in incoming.get("citation_counts_by_source", {}).items():
        target_counts[source] = max(target_counts.get(source, 0), parse_int(count))
    target["cited_by_count"] = max(
        target.get("cited_by_count", 0),
        incoming.get("cited_by_count", 0),
    )
    target["verification_sources"] = sorted(
        set(target.get("verification_sources", []))
        | set(incoming.get("verification_sources", []))
    )
    target["crossref_verified"] = bool(
        target.get("crossref_verified") or incoming.get("crossref_verified")
    )
    if include_discovery_source:
        target["sources"] = sorted(
            set(target.get("sources", [])) | set(incoming.get("sources", []))
        )
        target["hits"].extend(incoming.get("hits", []))


def deduplicate(records):
    unique = []
    by_doi = {}
    by_title = {}
    by_scholar_cluster = {}
    duplicates = 0

    for record in records:
        doi = normalize_doi(record.get("doi"))
        title_key = normalize_text(record.get("title"))
        scholar_cluster = str(record.get("scholar_cluster_id") or "")
        target = by_doi.get(doi) if doi else None
        if target is None and scholar_cluster:
            target = by_scholar_cluster.get(scholar_cluster)
        if target is None and title_key:
            for candidate in by_title.get(title_key, []):
                if title_fallback_compatible(candidate, record):
                    target = candidate
                    break
        if target is None:
            record["doi"] = doi
            unique.append(record)
            if doi:
                by_doi[doi] = record
            if title_key:
                by_title.setdefault(title_key, []).append(record)
            if scholar_cluster:
                by_scholar_cluster[scholar_cluster] = record
        else:
            duplicates += 1
            merge_record(target, record)
            if doi:
                by_doi[doi] = target
            if title_key:
                candidates = by_title.setdefault(title_key, [])
                if target not in candidates:
                    candidates.append(target)
            if scholar_cluster:
                by_scholar_cluster[scholar_cluster] = target
    return unique, duplicates


def verify_crossref_records(records, limit, mailto, timeout, retries):
    candidates = [
        record for record in records
        if record.get("doi") and "crossref" not in record.get("verification_sources", [])
    ][:limit]
    report = {
        "mode": "doi_identity_verification",
        "attempted": len(candidates),
        "verified": 0,
        "failed": 0,
        "errors": [],
    }
    for record in candidates:
        try:
            metadata = fetch_crossref_metadata(record["doi"], mailto, timeout, retries)
        except SearchError as exc:
            report["failed"] += 1
            report["errors"].append({"doi": record["doi"], "error": str(exc)})
            continue
        merge_record(record, metadata, include_discovery_source=False)
        report["verified"] += 1
    return report


def is_gray_literature(record):
    work_type = normalize_text(record.get("work_type", ""))
    if not work_type:
        return False
    phrases = {
        "working paper", "technical report", "research report",
        "posted content", "repository version",
    }
    if any(phrase in work_type for phrase in phrases):
        return True
    return bool(re.search(r"\b(?:thesis|dissertation|preprint|report)\b", work_type))


def assign_verification_status(records):
    for record in records:
        sources = set(record.get("sources", []))
        if {"wos", "scopus"}.intersection(sources) and record.get("doi"):
            level = "V1"
            status = "core_indexed_with_identifier"
            claim_eligible = True
        elif "sciencedirect" in sources and record.get("doi"):
            level = "V2"
            status = "publisher_platform_verified"
            claim_eligible = True
        elif is_gray_literature(record):
            level = "V3"
            status = "verified_or_candidate_gray_literature"
            claim_eligible = False
        else:
            level = "V4"
            status = "candidate_requires_publisher_verification"
            claim_eligible = False
        record["verification_level"] = level
        record["verification_status"] = status
        record["claim_eligible"] = claim_eligible
        record.setdefault("screening_status", "unscreened")
    return records


def matches_term(text, term):
    text_norm = normalize_text(text)
    term_norm = normalize_text(term)
    if term_norm and term_norm in text_norm:
        return True
    tokens = meaningful_tokens(term)
    return bool(tokens) and tokens.issubset(meaningful_tokens(text))


def rank_records(records, concepts, base_query):
    max_citations = max((record.get("cited_by_count", 0) for record in records), default=0)
    citation_denominator = math.log1p(max_citations) or 1.0
    query_tokens = meaningful_tokens(base_query or " ".join(c["terms"][0] for c in concepts))
    current_year = dt.date.today().year

    for record in records:
        title = record.get("title", "")
        text = f"{title} {record.get('abstract', '')} {record.get('journal', '')}"
        matched = []
        matched_terms = {}
        title_matched = 0
        for concept in concepts:
            terms = [term for term in concept["terms"] if matches_term(text, term)]
            if terms:
                matched.append(concept["name"])
                matched_terms[concept["name"]] = terms[:3]
            if any(matches_term(title, term) for term in concept["terms"]):
                title_matched += 1

        concept_coverage = len(matched) / len(concepts) if concepts else 0.0
        title_concept_coverage = title_matched / len(concepts) if concepts else 0.0
        title_tokens = meaningful_tokens(title)
        title_overlap = len(query_tokens & title_tokens) / max(len(query_tokens), 1)
        source_rank = max((hit.get("rank_score", 0.0) for hit in record.get("hits", [])), default=0.0)
        citation_score = math.log1p(record.get("cited_by_count", 0)) / citation_denominator
        year = record.get("year")
        recency = max(0.0, min(1.0, 1.0 - (current_year - year) / 30.0)) if year else 0.0
        completeness = sum(bool(record.get(field)) for field in ("doi", "abstract", "journal", "authors")) / 4.0

        score = (
            0.45 * concept_coverage
            + 0.15 * title_concept_coverage
            + 0.10 * title_overlap
            + 0.15 * source_rank
            + 0.08 * citation_score
            + 0.04 * recency
            + 0.03 * completeness
        )

        if concepts and len(matched) == len(concepts):
            tier = "core_intersection"
        elif any(concept["role"] == "method" and concept["name"] in matched for concept in concepts):
            tier = "theory_support"
        elif matched:
            tier = "field_background"
        else:
            tier = "broad_background"

        record["evidence_tier"] = tier
        record["relevance_tier"] = tier
        record["matched_concepts"] = matched
        record["matched_terms"] = matched_terms
        record["relevance_score"] = round(score, 6)
        record["score_components"] = {
            "concept_coverage": round(concept_coverage, 4),
            "title_concept_coverage": round(title_concept_coverage, 4),
            "title_overlap": round(title_overlap, 4),
            "source_rank": round(source_rank, 4),
            "citation": round(citation_score, 4),
            "recency": round(recency, 4),
            "metadata": round(completeness, 4),
        }

    records.sort(key=lambda item: (
        item.get("relevance_score", 0),
        item.get("cited_by_count", 0),
        item.get("year") or 0,
    ), reverse=True)
    return records


def read_benchmark(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    values = data.get("dois", []) if isinstance(data, dict) else data
    return [normalize_doi(value) for value in values if normalize_doi(value)]


def select_records(records, limit, mode, profile="legacy"):
    if profile == "rigorous" or mode == "quick" or len(records) <= limit:
        return records[:limit]

    quotas = {
        "core_intersection": max(1, round(limit * 0.35)),
        "field_background": max(1, round(limit * 0.35)),
        "theory_support": max(1, round(limit * 0.20)),
        "broad_background": max(1, round(limit * 0.10)),
    }
    selected = []
    selected_ids = set()
    for tier, quota in quotas.items():
        for record in (item for item in records if item.get("evidence_tier") == tier):
            if len([item for item in selected if item.get("evidence_tier") == tier]) >= quota:
                break
            selected.append(record)
            selected_ids.add(id(record))

    for record in records:
        if len(selected) >= limit:
            break
        if id(record) not in selected_ids:
            selected.append(record)
            selected_ids.add(id(record))

    global_rank = {id(record): rank for rank, record in enumerate(records)}
    selected.sort(key=lambda record: global_rank[id(record)])
    return selected[:limit]


def benchmark_report(records, selected_records, benchmark_dois):
    rank_by_doi = {
        normalize_doi(record.get("doi")): rank
        for rank, record in enumerate(records, 1)
        if normalize_doi(record.get("doi"))
    }
    found = [doi for doi in benchmark_dois if doi in rank_by_doi]
    selected_rank_by_doi = {
        normalize_doi(record.get("doi")): rank
        for rank, record in enumerate(selected_records, 1)
        if normalize_doi(record.get("doi"))
    }
    top_found = [doi for doi in found if doi in selected_rank_by_doi]
    return {
        "known_count": len(benchmark_dois),
        "found_count": len(found),
        "recall": round(len(found) / max(len(benchmark_dois), 1), 4),
        "top_limit_found_count": len(top_found),
        "top_limit_recall": round(len(top_found) / max(len(benchmark_dois), 1), 4),
        "found": [{
            "doi": doi,
            "rank": rank_by_doi[doi],
            "selected_rank": selected_rank_by_doi.get(doi),
        } for doi in found],
        "missing": [doi for doi in benchmark_dois if doi not in rank_by_doi],
    }


def prepare_google_scholar_routes(queries, options):
    defaults = MODE_DEFAULTS[getattr(options, "mode", "quick")]
    max_routes = getattr(options, "scholar_max_queries", defaults["scholar_routes"])
    per_query = getattr(options, "scholar_per_query", defaults["scholar_depth"])
    call_budget = getattr(options, "scholar_api_budget", defaults["scholar_budget"])
    structured = bool(queries and isinstance(queries[0], dict))
    citation_routes = []
    for value in getattr(options, "scholar_cites_id", [])[:max_routes]:
        if structured:
            citation_routes.append(route_record("google-scholar", {
                "route": f"cited_by:{value}",
                "query": f"cites:{value}",
                "purpose": "forward_citation_chasing",
            }))
        else:
            citation_routes.append(f"cites:{value}")
    regular_slots = max(0, max_routes - len(citation_routes))
    routes = citation_routes + list(queries[:regular_slots])
    notices = []

    try:
        account = serpapi_account_status(options.timeout, options.retries, stage="before")
        if account.get("remaining") is not None:
            call_budget = min(call_budget, account["remaining"])
    except SearchError as exc:
        notices.append({
            "source": "google-scholar",
            "query": "*",
            "severity": "warning",
            "error": f"SerpApi quota check failed; local call budget was used: {exc}",
        })

    calls_per_query = math.ceil(per_query / 20)
    max_routes_by_budget = call_budget // calls_per_query
    if max_routes_by_budget == 0 and call_budget > 0:
        per_query = min(per_query, call_budget * 20)
        calls_per_query = math.ceil(per_query / 20)
        max_routes_by_budget = 1
    routes = routes[:max_routes_by_budget]
    planned_calls = calls_per_query * len(routes)
    with API_LIMITS_LOCK:
        API_LIMITS.setdefault("google-scholar", {}).update({
            "mode": getattr(options, "mode", "quick"),
            "records_per_query": per_query,
            "query_routes": len(routes),
            "call_budget": call_budget,
            "planned_max_calls": planned_calls,
        })
    if not routes:
        notices.append({
            "source": "google-scholar",
            "query": "*",
            "severity": "error",
            "error": "No Google Scholar route fits the available SerpApi quota",
        })
    return routes, per_query, notices


def run_source(source, queries, options):
    if source in {"sciencedirect", "scopus"} and not elsevier_api_key():
        return source, [], [], [{
            "source": source,
            "query": "*",
            "severity": "error",
            "error": "Elsevier API key is unavailable; the Elsevier source was skipped",
        }]
    if source == "google-scholar" and not serpapi_api_key():
        return source, [], [], [{
            "source": source,
            "query": "*",
            "severity": "error",
            "error": "SERPAPI_API_KEY is unavailable; Google Scholar API search was skipped",
        }]
    source_queries = queries
    per_query_limit = options.per_query
    notices = []
    if source == "sciencedirect" and options.journal:
        source_queries = []
        for journal in options.journal:
            for item in queries:
                route = route_record(source, item)
                route["query"] = f"SRCTITLE({journal}) AND ({route['query']})"
                route["route"] = f"journal:{journal}:{route['route']}"
                route["purpose"] = "target_journal_depth"
                source_queries.append(route)
    if source == "openalex" and not os.environ.get("OPENALEX_API_KEY"):
        notices.append({
            "source": source,
            "query": "*",
            "severity": "warning",
            "error": "OPENALEX_API_KEY is not set; anonymous quota may make a comprehensive search incomplete",
        })
    if source == "semantic-scholar" and not semantic_scholar_api_key() and len(queries) > 1:
        source_queries = queries[:1]
        notices.append({
            "source": source,
            "query": "*",
            "severity": "warning",
            "error": "SEMANTIC_SCHOLAR_API_KEY is not set; only the first query variant was attempted",
        })
    if source == "google-scholar":
        source_queries, per_query_limit, scholar_notices = prepare_google_scholar_routes(
            queries, options
        )
        notices.extend(scholar_notices)
    function = SOURCE_FUNCTIONS[source]
    records = []
    stats = []
    errors = []
    for item in source_queries:
        route = route_record(source, item)
        query = route["query"]
        try:
            found, estimated_total = function(
                query,
                per_query_limit,
                options.year_from,
                options.year_to,
                options.doc_type,
                options.language,
                options.mailto,
                options.timeout,
                options.retries,
            )
            for record in found:
                for hit in record.get("hits", []):
                    if hit.get("source") == source and hit.get("query") == query:
                        hit["route"] = route.get("route")
                        hit["stage"] = route.get("stage")
                        hit["purpose"] = route.get("purpose")
            records.extend(found)
            stats.append({
                "source": source,
                "query": query,
                "returned": len(found),
                "estimated_total": estimated_total,
                "exact_retrieved_count": len(found),
                "provider_reported_total": estimated_total,
                "provider_total_kind": (
                    "estimate" if source == "google-scholar" else "reported_result_count"
                ),
                "provider_estimated_total": (
                    estimated_total if source == "google-scholar" else None
                ),
                "route": route.get("route"),
                "stage": route.get("stage"),
                "purpose": route.get("purpose"),
            })
        except QuotaExceeded as exc:
            errors.append({
                "source": source,
                "query": query,
                "route": route.get("route"),
                "severity": "error",
                "error": str(exc),
            })
            break
        except SearchError as exc:
            errors.append({
                "source": source,
                "query": query,
                "route": route.get("route"),
                "severity": "error",
                "error": str(exc),
            })
    if source == "google-scholar" and source_queries:
        try:
            serpapi_account_status(options.timeout, options.retries, stage="after")
        except SearchError:
            pass
    return source, records, stats, notices + errors


def source_contributions(records):
    contributions = {}
    query_contributions = {}
    for record in records:
        for source in record.get("sources", []):
            contributions[source] = contributions.get(source, 0) + 1
        routes = {
            f"{hit.get('source', '')}::{hit.get('route') or hit.get('query', '')}"
            for hit in record.get("hits", [])
            if hit.get("source") and (hit.get("route") or hit.get("query"))
        }
        for route in routes:
            query_contributions[route] = query_contributions.get(route, 0) + 1
    return contributions, query_contributions


def query_exclusive_contributions(records):
    exclusive = {}
    for record in records:
        routes = {
            f"{hit.get('source', '')}::{hit.get('route') or hit.get('query', '')}"
            for hit in record.get("hits", [])
            if hit.get("source") and (hit.get("route") or hit.get("query"))
        }
        if len(routes) == 1:
            route = next(iter(routes))
            exclusive[route] = exclusive.get(route, 0) + 1
    return exclusive


def source_status_report(sources, source_plan, query_stats, source_errors,
                         source_roles, wos_imported_routes,
                         wos_unmapped_imports, crossref_report):
    report = {}
    for source, role in source_roles.items():
        planned_routes = {
            item.get("route") or item.get("query")
            for item in source_plan.get(source, [])
        }
        planned_routes.discard(None)
        successful_routes = {
            item.get("route") or item.get("query")
            for item in query_stats
            if item.get("source") == source
        }
        successful_routes.discard(None)
        planned = len(planned_routes)
        attempted = len(successful_routes)
        issues = [item for item in source_errors if item.get("source") == source]
        errors = [item for item in issues if item.get("severity", "error") != "warning"]
        warnings = [item for item in issues if item.get("severity") == "warning"]
        if source == "wos":
            if planned_routes and planned_routes.issubset(wos_imported_routes):
                status = "completed_by_import"
            elif not planned_routes and (wos_imported_routes or wos_unmapped_imports):
                status = "imported_without_route_plan"
            elif wos_imported_routes or wos_unmapped_imports:
                status = "partial_import"
            else:
                status = "external_import_required"
            successful_routes = planned_routes.intersection(wos_imported_routes)
            attempted = len(successful_routes)
        elif (
            source == "crossref"
            and source in sources
            and role.get("role") == "identifier_verification"
        ):
            if crossref_report.get("mode") == "none":
                status = "verification_disabled"
            elif crossref_report.get("failed") and not crossref_report.get("verified"):
                status = "failed"
            elif crossref_report.get("failed"):
                status = "partial"
            else:
                status = "completed"
        elif source not in sources:
            status = "not_planned"
        elif planned and attempted == planned and not errors:
            status = "completed"
        elif attempted:
            status = "partial"
        elif errors:
            status = "failed"
        elif planned:
            status = "skipped"
        else:
            status = "not_applicable"
        report[source] = {
            **role,
            "status": status,
            "planned_routes": planned,
            "attempted_routes": attempted,
            "completed_routes": sorted(successful_routes),
            "missing_routes": sorted(planned_routes - successful_routes),
            "issues": len(issues),
            "errors": len(errors),
            "warnings": len(warnings),
        }
        if source == "wos":
            report[source]["imported_routes"] = sorted(wos_imported_routes)
            report[source]["unmapped_import_files"] = wos_unmapped_imports
    return report


def source_overlap_report(records):
    sources = sorted({source for record in records for source in record.get("sources", [])})
    exclusive = {
        source: sum(set(record.get("sources", [])) == {source} for record in records)
        for source in sources
    }
    pairwise = {}
    for index, left in enumerate(sources):
        for right in sources[index + 1:]:
            count = sum(
                left in record.get("sources", []) and right in record.get("sources", [])
                for record in records
            )
            if count:
                pairwise[f"{left} & {right}"] = count
    scholar_records = [
        record for record in records if "google-scholar" in record.get("sources", [])
    ]
    return {
        "exclusive": exclusive,
        "pairwise": pairwise,
        "google_scholar": {
            "total": len(scholar_records),
            "only": sum(set(record.get("sources", [])) == {"google-scholar"} for record in scholar_records),
            "with_other_sources": sum(len(record.get("sources", [])) > 1 for record in scholar_records),
        },
    }


def csv_safe(value):
    if not isinstance(value, str):
        return value
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", value)
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def terminal_safe(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or ""))


def write_csv(records, handle):
    fields = [
        "rank", "relevance_tier", "verification_level", "verification_status",
        "screening_status", "claim_eligible", "relevance_score", "title", "authors", "year",
        "journal", "doi", "cited_by_count", "sources", "matched_concepts",
        "abstract", "abstract_kind", "scholar_cites_id", "scholar_cluster_id",
        "scholar_versions_count", "canonical_source", "verification_sources", "url",
    ]
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    for rank, record in enumerate(records, 1):
        row = {
            "rank": rank,
            "relevance_tier": record.get("relevance_tier") or record.get("evidence_tier"),
            "verification_level": record.get("verification_level"),
            "verification_status": record.get("verification_status"),
            "screening_status": record.get("screening_status"),
            "claim_eligible": record.get("claim_eligible"),
            "relevance_score": record.get("relevance_score"),
            "title": record.get("title"),
            "authors": "; ".join(record.get("authors", [])),
            "year": record.get("year"),
            "journal": record.get("journal"),
            "doi": record.get("doi"),
            "cited_by_count": record.get("cited_by_count"),
            "sources": "; ".join(record.get("sources", [])),
            "matched_concepts": "; ".join(record.get("matched_concepts", [])),
            "abstract": record.get("abstract"),
            "abstract_kind": record.get("abstract_kind"),
            "scholar_cites_id": record.get("scholar_cites_id"),
            "scholar_cluster_id": record.get("scholar_cluster_id"),
            "scholar_versions_count": record.get("scholar_versions_count"),
            "canonical_source": record.get("canonical_source"),
            "verification_sources": "; ".join(record.get("verification_sources", [])),
            "url": record.get("url"),
        }
        writer.writerow({key: csv_safe(value) for key, value in row.items()})


def build_parser():
    parser = argparse.ArgumentParser(description="Auditable multi-source search for Management Science and Engineering")
    parser.add_argument("query", nargs="?", help="Natural-language research query")
    parser.add_argument("--concept", action="append", default=[],
                        help="Concept block: role:name=term1|term2|term3 (repeatable)")
    parser.add_argument("--domain", default="auto",
                        choices=["auto", "general", "innovation-management", "operations-management", "organizational-management", "sustainability-management"])
    parser.add_argument("--mode", choices=["quick", "comprehensive", "deep"], default="quick")
    parser.add_argument("--profile", choices=sorted(RESEARCH_PROFILES), default="legacy",
                        help="rigorous enables source-specific roles and staged routing; legacy preserves the previous behavior")
    parser.add_argument("--sources", default="auto",
                        help="Comma-separated: openalex,crossref,semantic-scholar,sciencedirect,scopus,google-scholar; use auto, all, or none")
    parser.add_argument("--per-query", type=int, help="Records per query and source; defaults depend on mode")
    parser.add_argument("--max-variants", type=int, help="Maximum generated query variants; defaults depend on mode")
    parser.add_argument("--limit", type=int, help="Ranked preview size; defaults depend on mode")
    parser.add_argument("--scholar-per-query", type=int,
                        help="Google Scholar records per query; defaults to 20/40/100 by mode")
    parser.add_argument("--scholar-max-queries", type=int,
                        help="Google Scholar query routes; defaults to 2/4/5 by mode")
    parser.add_argument("--scholar-api-budget", type=int,
                        help="Maximum SerpApi search calls; defaults to 2/8/25 by mode")
    parser.add_argument("--scholar-cites-id", action="append", default=[],
                        help="Google Scholar cited-by ID from a prior result; repeatable")
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    parser.add_argument("--type", dest="doc_type", choices=["journal-article", "review", "book-chapter"])
    parser.add_argument("--language", help="ISO language code; applied only where the source supports document-language filtering")
    parser.add_argument("--journal", action="append", default=[],
                        help="Target journal for ScienceDirect; repeatable")
    parser.add_argument("--fetch-abstracts", choices=["auto", "none", "scopus"], default="auto",
                        help="Retrieve abstracts by DOI after discovery; auto enriches Elsevier-source results")
    parser.add_argument("--abstract-limit", type=int,
                        help="Maximum DOI abstracts to retrieve; defaults to the ranked preview size")
    parser.add_argument("--import-file", action="append", default=[], help="WoS/Scholar RIS, BibTeX, CSV, TSV, or JSON export")
    parser.add_argument("--wos-import", action="append", default=[],
                        help="WoS export as ROUTE=PATH (repeat per planned route); an unlabeled PATH is imported but cannot prove route completion")
    parser.add_argument("--crossref-verify", choices=["auto", "none", "all"], default="auto",
                        help="Verify DOI identity after deduplication; auto enables this for the rigorous profile")
    parser.add_argument("--verification-limit", type=int,
                        help="Maximum shortlisted DOI records to verify through Crossref")
    parser.add_argument("--benchmark-dois", help="JSON file containing known DOIs")
    parser.add_argument("--vocabulary", default=str(DEFAULT_VOCABULARY))
    parser.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO") or os.environ.get("CROSSREF_MAILTO") or DEFAULT_MAILTO)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="Print the query plan without searching")
    parser.add_argument("--format", choices=["json", "csv", "compact"], default="json")
    parser.add_argument("--output", help="Write results to this file instead of stdout")
    parser.add_argument("--audit-output", help="Write the audit object to a separate JSON file")
    parser.add_argument("--screening-output",
                        help="Write the complete deduplicated screening pool to JSON or CSV")
    return parser


def main(argv=None):
    with API_LIMITS_LOCK:
        API_LIMITS.clear()
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        not args.query
        and not args.concept
        and not args.import_file
        and not args.wos_import
        and not args.scholar_cites_id
    ):
        parser.error(
            "provide a query, a --concept, an --import-file, a --wos-import, "
            "or --scholar-cites-id"
        )
    if args.year_from and args.year_to and args.year_from > args.year_to:
        parser.error("--year-from must not exceed --year-to")
    defaults = MODE_DEFAULTS[args.mode]
    args.per_query = max(1, min(args.per_query or defaults["per_query"], 1000))
    args.limit = max(1, args.limit or defaults["limit"])
    args.max_variants = max(1, args.max_variants or defaults["variants"])
    args.scholar_per_query = max(1, min(args.scholar_per_query or defaults["scholar_depth"], 100))
    args.scholar_max_queries = max(1, min(args.scholar_max_queries or defaults["scholar_routes"], 5))
    args.scholar_api_budget = max(1, min(args.scholar_api_budget or defaults["scholar_budget"], 25))
    invalid_cites_ids = [value for value in args.scholar_cites_id if not str(value).isdigit()]
    if invalid_cites_ids:
        parser.error("--scholar-cites-id must contain digits only")
    args.abstract_limit = max(1, args.abstract_limit or args.limit)
    args.verification_limit = max(1, args.verification_limit or min(args.limit, 50))

    sources = resolve_sources(args.sources, args.profile, args.journal)
    invalid_sources = [source for source in sources if source not in SOURCE_FUNCTIONS]
    if invalid_sources:
        parser.error(f"unsupported sources: {', '.join(invalid_sources)}")
    if args.scholar_cites_id and "google-scholar" not in sources:
        parser.error("--scholar-cites-id requires --sources google-scholar")

    vocabulary = load_vocabulary(args.vocabulary)
    domain = detect_domain(args.query or "", vocabulary) if args.domain == "auto" else args.domain
    concepts = [parse_concept(spec) for spec in args.concept]
    concepts_origin = "explicit" if concepts else "automatic"
    if not concepts and args.query:
        concepts = automatic_concepts(args.query, domain, vocabulary)

    base_query = args.query or " ".join(concept["terms"][0] for concept in concepts)
    max_variants = min(args.max_variants, 5) if args.mode == "quick" else args.max_variants
    queries = generate_query_variants(base_query, concepts, args.mode, max_variants)
    source_query_plan = build_source_query_plan(
        base_query,
        concepts,
        queries,
        args.mode,
        args.profile,
        sources,
        args.scholar_max_queries,
    )
    source_roles = effective_source_roles(args.profile)
    resource_policy = {
        "api": {
            "quota_check": "at_provider_route_runtime",
            "strategy": "source_specific_bounded_routes_then_staged_enrichment",
        },
        "agent": {
            "preflight_quota_check": False,
            "strategy": "deduplicate_before_disjoint_title_abstract_fulltext_batches",
            "retrieval_control": "deterministic_script_not_agent",
        },
    }

    plan = {
        "query": base_query,
        "domain": domain,
        "mode": args.mode,
        "profile": args.profile,
        "concepts": concepts,
        "concepts_origin": concepts_origin,
        "concept_review_required": args.profile == "rigorous" and concepts_origin != "explicit",
        "query_variants": queries,
        "wos_queries": [
            {"route": item.get("route"), "query": item.get("query")}
            for item in source_query_plan.get("wos", [])
        ],
        "source_roles": source_roles,
        "source_query_plan": source_query_plan,
        "sources_resolved": sources,
        "target_journals": args.journal,
        "abstract_mode": args.fetch_abstracts,
        "crossref_policy": {
            "role": "post_deduplication_identity_verification",
            "mode": args.crossref_verify,
            "record_limit": args.verification_limit,
        },
        "scholar_policy": {
            "role": "parallel_broad_discovery",
            "records_per_query": args.scholar_per_query,
            "max_query_routes": args.scholar_max_queries,
            "api_call_budget": args.scholar_api_budget,
            "cited_by_routes": len(args.scholar_cites_id),
        },
        "resource_policy": resource_policy,
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    raw_records = []
    import_stats = []
    for file_path in args.import_file:
        imported = import_records(file_path)
        raw_records.extend(imported)
        import_stats.append({
            "file": str(file_path),
            "source": "unlabeled_import",
            "records": len(imported),
        })
    valid_wos_routes = {
        item.get("route") for item in source_query_plan.get("wos", []) if item.get("route")
    }
    for import_spec in args.wos_import:
        import_route, file_path = parse_wos_import_spec(import_spec, valid_wos_routes)
        imported = relabel_import_records(
            import_records(file_path),
            "wos",
            file_path,
            import_route,
        )
        raw_records.extend(imported)
        import_stats.append({
            "file": str(file_path),
            "source": "wos",
            "route": import_route,
            "records": len(imported),
        })

    query_stats = []
    source_errors = []
    if sources and (queries or args.scholar_cites_id):
        for stage in STAGE_ORDER:
            stage_sources = [
                source for source in sources
                if source_query_plan.get(source)
                and source_query_plan[source][0].get("stage") == stage
            ]
            if not stage_sources:
                continue
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(stage_sources)) as executor:
                futures = [
                    executor.submit(run_source, source, source_query_plan[source], args)
                    for source in stage_sources
                ]
                for future in concurrent.futures.as_completed(futures):
                    _source, records, stats, errors = future.result()
                    raw_records.extend(records)
                    query_stats.extend(stats)
                    source_errors.extend(errors)

    unique_records, duplicate_count = deduplicate(raw_records)
    rank_records(unique_records, concepts, base_query)

    crossref_enabled = (
        args.crossref_verify == "all"
        or (
            args.crossref_verify == "auto"
            and args.profile == "rigorous"
            and "crossref" in sources
        )
    )
    crossref_report = {
        "mode": "none",
        "attempted": 0,
        "verified": 0,
        "failed": 0,
        "errors": [],
    }
    if crossref_enabled:
        crossref_report = verify_crossref_records(
            unique_records,
            args.verification_limit,
            args.mailto,
            args.timeout,
            args.retries,
        )
        if crossref_report["verified"]:
            rank_records(unique_records, concepts, base_query)

    should_enrich = (
        args.fetch_abstracts == "scopus"
        or (
            args.fetch_abstracts == "auto"
            and {"sciencedirect", "scopus"}.intersection(sources)
        )
    )
    abstract_report = {
        "mode": "none",
        "attempted": 0,
        "succeeded": 0,
        "missing": 0,
        "errors": [],
    }
    if should_enrich:
        abstract_report = enrich_abstracts(
            unique_records,
            args.abstract_limit,
            args.timeout,
            args.retries,
            elsevier_only=args.fetch_abstracts == "auto",
        )
        if abstract_report["succeeded"]:
            rank_records(unique_records, concepts, base_query)
    assign_verification_status(unique_records)

    source_counts, query_counts = source_contributions(unique_records)
    overlap = source_overlap_report(unique_records)
    tier_counts = {}
    verification_counts = {}
    for record in unique_records:
        tier = record.get("relevance_tier", "broad_background")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        level = record.get("verification_level", "V4")
        verification_counts[level] = verification_counts.get(level, 0) + 1

    final_records = select_records(unique_records, args.limit, args.mode, args.profile)
    wos_imported_routes = {
        item.get("route")
        for item in import_stats
        if item.get("source") == "wos" and item.get("route")
    }
    wos_unmapped_imports = sum(
        item.get("source") == "wos" and not item.get("route")
        for item in import_stats
    )
    status_sources = list(sources)
    if crossref_enabled and "crossref" not in status_sources:
        status_sources.append("crossref")
    source_status = source_status_report(
        status_sources,
        source_query_plan,
        query_stats,
        source_errors,
        source_roles,
        wos_imported_routes,
        wos_unmapped_imports,
        crossref_report,
    )
    core_sources = ["wos", "scopus", "google-scholar"]
    core_search_complete = all(
        source_status[source]["status"] in {"completed", "completed_by_import"}
        for source in core_sources
    )
    scholar_stats = [
        item for item in query_stats if item.get("source") == "google-scholar"
    ]
    audit = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan": plan,
        "sources_requested": sources,
        "source_roles": source_roles,
        "source_status": source_status,
        "core_search_complete": core_search_complete,
        "imports": import_stats,
        "query_stats": sorted(query_stats, key=lambda item: (item["source"], item["query"])),
        "source_issues": source_errors,
        "source_errors": source_errors,
        "raw_record_count": len(raw_records),
        "unique_record_count": len(unique_records),
        "screening_pool_count": len(unique_records),
        "duplicates_merged": duplicate_count,
        "returned_record_count": len(final_records),
        "ranked_preview_count": len(final_records),
        "selection": "ranked_preview_not_a_screening_decision",
        "source_record_counts": source_counts,
        "source_exclusive_contribution": overlap["exclusive"],
        "source_unique_contribution": source_counts,
        "source_overlap": overlap,
        "query_record_counts": query_counts,
        "query_exclusive_contribution": query_exclusive_contributions(unique_records),
        "query_unique_contribution": query_counts,
        "relevance_tier_counts": tier_counts,
        "evidence_tier_counts": tier_counts,
        "verification_level_counts": verification_counts,
        "claim_eligible_count": sum(bool(record.get("claim_eligible")) for record in unique_records),
        "scholar_retrieval": {
            "exact_retrieved_across_routes_before_deduplication": sum(
                item.get("exact_retrieved_count", 0) for item in scholar_stats
            ),
            "unique_works_after_global_deduplication": sum(
                "google-scholar" in record.get("sources", []) for record in unique_records
            ),
            "provider_estimates_are_route_level_not_additive": [
                {
                    "route": item.get("route"),
                    "estimate": item.get("provider_estimated_total"),
                }
                for item in scholar_stats
            ],
        },
        "crossref_verification": crossref_report,
        "abstract_enrichment": abstract_report,
        "api_limits": rate_limit_snapshot(),
        "resource_policy": resource_policy,
        "deprecated_field_notes": {
            "source_unique_contribution": "alias of source_record_counts; use source_exclusive_contribution for exclusive works",
            "query_unique_contribution": "alias of query_record_counts; use query_exclusive_contribution for exclusive works",
            "evidence_tier": "alias of relevance_tier; verification_level is separate",
            "source_errors": "compatibility alias of source_issues; inspect severity to distinguish warnings",
        },
    }
    if args.benchmark_dois:
        audit["benchmark"] = benchmark_report(unique_records, final_records, read_benchmark(args.benchmark_dois))

    payload = {
        "audit": audit,
        "results": final_records,
        "results_role": "ranked_preview",
    }
    output_handle = None
    try:
        if args.output:
            output_handle = Path(args.output).open("w", encoding="utf-8", newline="")
        else:
            output_handle = sys.stdout

        if args.format == "json":
            json.dump(payload, output_handle, ensure_ascii=False, indent=2)
            output_handle.write("\n")
        elif args.format == "csv":
            write_csv(final_records, output_handle)
        else:
            for rank, record in enumerate(final_records, 1):
                print(
                    f"{rank:>3}. [{record['relevance_tier']}/{record['verification_level']}] "
                    f"{record.get('year') or 'n.d.'} | "
                    f"{terminal_safe(record['title'])} | DOI:{terminal_safe(record.get('doi') or '-')} | "
                    f"score:{record['relevance_score']:.3f} | {','.join(record['sources'])}",
                    file=output_handle,
                )
    finally:
        if args.output and output_handle:
            output_handle.close()

    if args.audit_output:
        Path(args.audit_output).write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.screening_output:
        screening_path = Path(args.screening_output)
        if screening_path.suffix.lower() == ".csv":
            with screening_path.open("w", encoding="utf-8", newline="") as handle:
                write_csv(unique_records, handle)
        else:
            screening_path.write_text(
                json.dumps(unique_records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    if args.format != "json":
        print(
            f"AUDIT raw={len(raw_records)} unique={len(unique_records)} "
            f"duplicates={duplicate_count} errors={len(source_errors)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
