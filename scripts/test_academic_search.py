#!/usr/bin/env python3

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("academic_search.py")
SPEC = importlib.util.spec_from_file_location("mse_academic_search", MODULE_PATH)
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)


def record(title, doi="", abstract="", source="openalex", rank=1):
    return search.common_record(
        title, doi, ["A. Author"], 2024, "2024-01-01", 10,
        "Journal", abstract, "https://example.org", "article", "en",
        source, "test query", rank, 10,
    )


class SearchCoreTests(unittest.TestCase):
    def test_normalize_doi(self):
        self.assertEqual(
            search.normalize_doi("https://doi.org/10.1000/ABC.1."),
            "10.1000/abc.1",
        )

    def test_csv_output_neutralizes_formula_prefixes(self):
        item = record("=HYPERLINK(\"https://example.org\")")
        handle = io.StringIO()
        search.write_csv([item], handle)
        self.assertIn("'=HYPERLINK", handle.getvalue())

    def test_terminal_output_removes_control_sequences(self):
        self.assertEqual(search.terminal_safe("safe\x1b[31m red"), "safe [31m red")

    def test_parse_concept(self):
        concept = search.parse_concept("method:causal=panel data|difference in differences")
        self.assertEqual(concept["role"], "method")
        self.assertEqual(concept["name"], "causal")
        self.assertEqual(len(concept["terms"]), 2)

    def test_parse_concept_preserves_structured_index_wildcards(self):
        concept = search.parse_concept(
            "topic:innovation=process innovation*|product innovation*"
        )
        self.assertEqual(
            concept["terms"],
            ["process innovation*", "product innovation*"],
        )

    def test_comprehensive_queries_include_leave_one_out(self):
        concepts = [
            search.parse_concept("topic:innovation=technology adoption|process innovation"),
            search.parse_concept("context:industry=manufacturing firms|service firms"),
            search.parse_concept("method:causal=panel data|difference in differences"),
        ]
        variants = search.generate_query_variants("", concepts, "comprehensive", 20)
        self.assertIn("technology adoption manufacturing firms", variants)
        self.assertIn("manufacturing firms panel data", variants)
        self.assertIn("technology adoption panel data", variants)

    def test_wos_query_plan_contains_core_and_supplements(self):
        concepts = [
            search.parse_concept("topic:innovation=technology adoption|process innovation"),
            search.parse_concept("context:industry=manufacturing firms|service firms"),
            search.parse_concept("method:causal=panel data|difference in differences"),
        ]
        queries = search.build_wos_queries(concepts)
        self.assertEqual(queries[0]["route"], "core_intersection")
        self.assertIn('TS=(("technology adoption" OR "process innovation")', queries[0]["query"])
        self.assertEqual(len(queries), 4)

    def test_deduplicate_merges_doi_and_provenance(self):
        first = record("A Useful Paper", "10.1000/test", source="openalex")
        second = record("A useful paper", "https://doi.org/10.1000/TEST", source="crossref")
        unique, duplicates = search.deduplicate([first, second])
        self.assertEqual(len(unique), 1)
        self.assertEqual(duplicates, 1)
        self.assertEqual(unique[0]["sources"], ["crossref", "openalex"])

    def test_deduplicate_prefers_verified_abstract_over_longer_scholar_snippet(self):
        snippet = record("A Useful Paper", "10.1000/test", "S" * 300, source="google-scholar")
        snippet["abstract_kind"] = "search_snippet"
        abstract = record("A Useful Paper", "10.1000/test", "Verified abstract.", source="crossref")
        unique, _duplicates = search.deduplicate([snippet, abstract])
        self.assertEqual(unique[0]["abstract"], "Verified abstract.")
        self.assertEqual(unique[0]["abstract_kind"], "abstract")

    def test_rank_prioritizes_full_concept_coverage(self):
        concepts = [
            search.parse_concept("topic:innovation=technology adoption|process innovation"),
            search.parse_concept("method:causal=panel data|difference in differences"),
        ]
        core = record(
            "Panel analysis of technology adoption",
            "10.1000/core",
            "This study examines process innovation using panel data.",
        )
        background = record(
            "Manufacturing innovation policy",
            "10.1000/background",
            "This study examines process innovation and regulation.",
        )
        ranked = search.rank_records([background, core], concepts, "technology adoption panel data")
        self.assertEqual(ranked[0]["doi"], "10.1000/core")
        self.assertEqual(ranked[0]["evidence_tier"], "core_intersection")
        self.assertEqual(ranked[1]["evidence_tier"], "field_background")

    def test_bibtex_parser_handles_nested_braces(self):
        text = r"""@article{key,
          title = {A {Panel} Study of Innovation \& Performance},
          author = {Li, A and Wang, B},
          year = {2024},
          journal = {Management Science},
          doi = {10.1000/nested\_id}
        }"""
        entries = search.parse_bibtex_entries(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "A Panel Study of Innovation & Performance")
        self.assertEqual(entries[0]["doi"], "10.1000/nested_id")

    def test_csv_import_supports_wos_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "savedrecs.csv"
            path.write_text(
                "Article Title,Authors,Publication Year,Source Title,DOI,Times Cited,Abstract\n"
                "Innovation Performance,Li A; Wang B,2024,Management Science,10.1000/wos,12,Panel analysis\n",
                encoding="utf-8",
            )
            records = search.parse_csv_file(path)
        self.assertEqual(records[0]["doi"], "10.1000/wos")
        self.assertEqual(records[0]["cited_by_count"], 12)
        self.assertEqual(records[0]["year"], 2024)

    def test_benchmark_reports_rank(self):
        records = [record("Known", "10.1000/known"), record("Other", "10.1000/other")]
        report = search.benchmark_report(records, records[:1], ["10.1000/known", "10.1000/missing"])
        self.assertEqual(report["found_count"], 1)
        self.assertEqual(report["top_limit_found_count"], 1)
        self.assertEqual(report["missing"], ["10.1000/missing"])

    def test_comprehensive_selection_preserves_evidence_tiers(self):
        records = []
        for index in range(30):
            item = record(f"Theory {index}", f"10.1000/theory{index}")
            item["evidence_tier"] = "theory_support"
            item["relevance_score"] = 1.0 - index / 100
            records.append(item)
        field = record("Direct field evidence", "10.1000/field")
        field["evidence_tier"] = "field_background"
        field["relevance_score"] = 0.1
        records.append(field)
        selected = search.select_records(records, 10, "comprehensive")
        self.assertIn("10.1000/field", {item["doi"] for item in selected})

    def test_vocabulary_is_valid(self):
        vocabulary = search.load_vocabulary(Path(__file__).parent.parent / "references" / "domain-vocabulary.json")
        self.assertIn("innovation-management", vocabulary["profiles"])
        self.assertIn("innovation", vocabulary["groups"])

    def test_automatic_concepts_keep_unmatched_sector_anchor(self):
        vocabulary = search.load_vocabulary(Path(__file__).parent.parent / "references" / "domain-vocabulary.json")
        concepts = search.automatic_concepts(
            "technology adoption panel data manufacturing sector",
            "innovation-management",
            vocabulary,
        )
        anchors = [concept for concept in concepts if concept["name"] == "anchor"]
        self.assertEqual(anchors[0]["terms"], ["manufacturing sector"])

    def test_sciencedirect_source_uses_elsevier_key(self):
        payload = {
            "search-results": {
                "opensearch:totalResults": "1",
                "entry": [{
                    "dc:title": "Innovation management study",
                    "prism:doi": "10.1000/scidir",
                    "prism:coverDate": "2024-01-01",
                    "prism:publicationName": "Management Science",
                    "authors": {"author": [{"$": "A. Author"}]},
                }],
            }
        }
        with mock.patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload):
                records, total = search.search_sciencedirect(
                    "innovation management", 5, None, None, None, None,
                    "test@example.com", 10, 0,
                )
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["doi"], "10.1000/scidir")
        self.assertEqual(records[0]["sources"], ["sciencedirect"])

    def test_semantic_scholar_key_uses_environment(self):
        with mock.patch.dict(
            os.environ,
            {"SEMANTIC_SCHOLAR_API_KEY": "environment-key"},
            clear=False,
        ):
            self.assertEqual(search.semantic_scholar_api_key(), "environment-key")

    def test_semantic_scholar_key_is_empty_when_environment_is_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(search.semantic_scholar_api_key(), "")

    def test_semantic_scholar_environment_key_allows_all_query_variants(self):
        captured = []

        def fake_source(query, *_args):
            captured.append(query)
            return [], 0

        options = SimpleNamespace(
            journal=[], per_query=5, year_from=None, year_to=None,
            doc_type=None, language=None, mailto="test@example.com",
            timeout=10, retries=0,
        )
        with mock.patch.object(search, "semantic_scholar_api_key", return_value="environment-key"):
            with mock.patch.dict(search.SOURCE_FUNCTIONS, {"semantic-scholar": fake_source}):
                _source, _records, _stats, notices = search.run_source(
                    "semantic-scholar", ["one", "two"], options
                )
        self.assertEqual(captured, ["one", "two"])
        self.assertEqual(notices, [])

    def test_scopus_abstract_retrieval(self):
        payload = {
            "abstracts-retrieval-response": {
                "coredata": {
                    "dc:description": "A complete abstract for testing.",
                    "citedby-count": "12",
                    "prism:url": "https://example.org/abstract",
                }
            }
        }
        with mock.patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload):
                metadata = search.fetch_scopus_abstract("10.1000/abstract", 10, 0)
        self.assertEqual(metadata["abstract"], "A complete abstract for testing.")
        self.assertEqual(metadata["cited_by_count"], 12)

    def test_abstract_enrichment_updates_record(self):
        item = record("ScienceDirect paper", "10.1000/enrich", source="sciencedirect")
        item["abstract"] = ""
        with mock.patch.object(search, "fetch_scopus_abstract", return_value={
            "abstract": "Retrieved abstract.",
            "cited_by_count": 7,
            "url": "https://example.org/enrich",
        }):
            report = search.enrich_abstracts([item], 1, 10, 0)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(item["abstract"], "Retrieved abstract.")

    def test_sciencedirect_journal_route_builds_source_title_query(self):
        captured = []

        def fake_source(query, *_args):
            captured.append(query)
            return [], 0

        options = SimpleNamespace(
            journal=["Management Science"], per_query=5, year_from=None,
            year_to=None, doc_type=None, language=None,
            mailto="test@example.com", timeout=10, retries=0,
        )
        with mock.patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False):
            with mock.patch.dict(search.SOURCE_FUNCTIONS, {"sciencedirect": fake_source}):
                search.run_source("sciencedirect", ["technology adoption"], options)
        self.assertEqual(captured, ["SRCTITLE(Management Science) AND (technology adoption)"])

    def test_scopus_query_and_result_parsing(self):
        payload = {
            "search-results": {
                "opensearch:totalResults": "1",
                "entry": [{
                    "dc:title": "Technology adoption and performance",
                    "prism:doi": "10.1000/scopus",
                    "prism:coverDate": "2025-01-01",
                    "prism:publicationName": "Management Science",
                    "citedby-count": "9",
                    "dc:creator": "Li, A.",
                    "subtypeDescription": "Article",
                }],
            }
        }
        with mock.patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload) as request:
                records, total = search.search_scopus(
                    "technology adoption", 5, 2020, 2025, "journal-article", None,
                    "test@example.com", 10, 0,
                )
        self.assertEqual(total, 1)
        self.assertEqual(records[0]["doi"], "10.1000/scopus")
        self.assertEqual(records[0]["cited_by_count"], 9)
        params = request.call_args.args[1]
        self.assertIn("TITLE-ABS-KEY(technology adoption)", params["query"])
        self.assertEqual(request.call_args.kwargs["rate_key"], "scopus-search")

    def test_elsevier_empty_result_marker_is_not_a_record(self):
        payload = {
            "search-results": {
                "opensearch:totalResults": "0",
                "entry": [{"error": "Result set was empty"}],
            }
        }
        with mock.patch.dict(os.environ, {"ELSEVIER_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload):
                scopus_records, _total = search.search_scopus(
                    "no result", 5, None, None, None, None,
                    "test@example.com", 10, 0,
                )
                sd_records, _total = search.search_sciencedirect(
                    "no result", 5, None, None, None, None,
                    "test@example.com", 10, 0,
                )
        self.assertEqual(scopus_records, [])
        self.assertEqual(sd_records, [])

    def test_google_scholar_serpapi_result_is_marked_as_snippet(self):
        payload = {
            "search_information": {"total_results": 200},
            "organic_results": [{
                "position": 1,
                "title": "Employee training and productivity",
                "link": "https://example.org/paper",
                "snippet": "This paper estimates the effect using panel data.",
                "publication_info": {
                    "summary": "A Li, B Wang - Management Science, 2024 - Publisher",
                    "authors": [{"name": "A Li"}, {"name": "B Wang"}],
                },
                "result_id": "scholar-result-1",
                "inline_links": {
                    "cited_by": {"total": 18, "cites_id": "123456"},
                    "versions": {"total": 3, "cluster_id": "654321"},
                },
            }],
        }
        with mock.patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload) as request:
                records, total = search.search_google_scholar(
                    "employee training productivity", 20, 2020, 2025,
                    None, "en", "test@example.com", 10, 0,
                )
        self.assertEqual(total, 200)
        self.assertEqual(records[0]["year"], 2024)
        self.assertEqual(records[0]["work_type"], "search-result-snippet")
        self.assertEqual(records[0]["cited_by_count"], 18)
        self.assertEqual(records[0]["scholar_cites_id"], "123456")
        self.assertEqual(records[0]["scholar_cluster_id"], "654321")
        self.assertEqual(request.call_args.kwargs["rate_key"], "google-scholar")

    def test_google_scholar_route_caps_query_variants(self):
        captured = []

        def fake_source(query, *_args):
            captured.append(query)
            return [], 0

        options = SimpleNamespace(
            mode="quick", journal=[], per_query=5, year_from=None, year_to=None,
            doc_type=None, language=None, mailto="test@example.com",
            timeout=10, retries=0, scholar_cites_id=[],
        )
        with mock.patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "serpapi_account_status", return_value={"remaining": 250}):
                with mock.patch.dict(search.SOURCE_FUNCTIONS, {"google-scholar": fake_source}):
                    _source, _records, _stats, notices = search.run_source(
                        "google-scholar", ["one", "two", "three"], options
                    )
        self.assertEqual(captured, ["one", "two"])
        self.assertEqual(notices, [])

    def test_google_scholar_paginates_to_requested_depth(self):
        def item(index):
            return {
                "title": f"Scholar paper {index}",
                "link": f"https://example.org/{index}",
                "snippet": "Relevant abstract snippet.",
                "publication_info": {"summary": f"A Author - Journal, 202{index % 5}"},
            }

        first_page = {
            "search_information": {"total_results": 1000},
            "organic_results": [item(index) for index in range(20)],
        }
        second_page = {
            "search_information": {"total_results": 1000},
            "organic_results": [item(index) for index in range(20, 25)],
        }
        with mock.patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(
                search, "request_json", side_effect=[first_page, second_page]
            ) as request:
                records, total = search.search_google_scholar(
                    "resource management", 25, None, None, None, "en",
                    "test@example.com", 10, 0,
                )
        self.assertEqual(len(records), 25)
        self.assertEqual(total, 1000)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1]["start"], 0)
        self.assertEqual(request.call_args_list[1].args[1]["start"], 20)

    def test_google_scholar_citation_route_uses_cites_parameter(self):
        payload = {"organic_results": []}
        with mock.patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload) as request:
                search.search_google_scholar(
                    "cites:123456", 20, None, None, None, "en",
                    "test@example.com", 10, 0,
                )
        params = request.call_args.args[1]
        self.assertEqual(params["cites"], "123456")
        self.assertNotIn("q", params)

    def test_deep_scholar_route_has_bounded_500_record_plan(self):
        options = SimpleNamespace(
            mode="deep", timeout=10, retries=0, scholar_cites_id=[],
        )
        with mock.patch.object(search, "serpapi_account_status", return_value={"remaining": 250}):
            routes, per_query, notices = search.prepare_google_scholar_routes(
                [f"query {index}" for index in range(10)], options
            )
        self.assertEqual(len(routes), 5)
        self.assertEqual(per_query, 100)
        self.assertEqual(notices, [])
        self.assertEqual(search.rate_limit_snapshot()["google-scholar"]["planned_max_calls"], 25)

    def test_scholar_budget_respects_account_remaining(self):
        options = SimpleNamespace(
            mode="comprehensive", timeout=10, retries=0, scholar_cites_id=[],
        )
        with mock.patch.object(search, "serpapi_account_status", return_value={"remaining": 3}):
            routes, per_query, _notices = search.prepare_google_scholar_routes(
                ["one", "two", "three", "four"], options
            )
        self.assertEqual(routes, ["one"])
        self.assertEqual(per_query, 40)

    def test_explicit_citation_route_is_prioritized_under_small_budget(self):
        options = SimpleNamespace(
            mode="comprehensive", timeout=10, retries=0,
            scholar_cites_id=["123456"],
        )
        with mock.patch.object(search, "serpapi_account_status", return_value={"remaining": 2}):
            routes, _per_query, _notices = search.prepare_google_scholar_routes(
                ["topic one", "topic two"], options
            )
        self.assertEqual(routes, ["cites:123456"])

    def test_scholar_cluster_id_deduplicates_versions(self):
        first = record("Working paper title", source="google-scholar")
        second = record("Published paper title", source="google-scholar")
        first["scholar_cluster_id"] = second["scholar_cluster_id"] = "cluster-1"
        unique, duplicates = search.deduplicate([first, second])
        self.assertEqual(len(unique), 1)
        self.assertEqual(duplicates, 1)

    def test_source_overlap_reports_scholar_only_and_shared(self):
        scholar_only = record("Scholar only", source="google-scholar")
        shared = record("Shared", "10.1000/shared", source="google-scholar")
        scopus_copy = record("Shared", "10.1000/shared", source="scopus")
        unique, _duplicates = search.deduplicate([scholar_only, shared, scopus_copy])
        report = search.source_overlap_report(unique)
        self.assertEqual(report["google_scholar"]["only"], 1)
        self.assertEqual(report["google_scholar"]["with_other_sources"], 1)
        self.assertEqual(report["pairwise"]["google-scholar & scopus"], 1)

    def test_abstract_enrichment_stops_when_weekly_quota_is_exhausted(self):
        first = record("First", "10.1000/first", source="scopus")
        second = record("Second", "10.1000/second", source="scopus")
        first["abstract"] = second["abstract"] = ""
        with mock.patch.object(
            search, "fetch_scopus_abstract",
            side_effect=search.QuotaExceeded("weekly quota exhausted"),
        ) as fetch:
            report = search.enrich_abstracts([first, second], 2, 10, 0)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(report["errors"]), 1)

    def test_rate_limit_snapshot_excludes_empty_headers(self):
        with search.API_LIMITS_LOCK:
            search.API_LIMITS.clear()
        search.record_rate_limit(
            "sciencedirect-search",
            {
                "X-RateLimit-Limit": "20000",
                "X-RateLimit-Remaining": "19999",
                "X-RateLimit-Reset": "1893456000",
            },
            200,
        )
        snapshot = search.rate_limit_snapshot()["sciencedirect-search"]
        self.assertEqual(snapshot["remaining"], 19999)
        self.assertNotIn("provider_status", snapshot)

    def test_serpapi_account_audit_excludes_api_key(self):
        payload = {
            "api_key": "must-not-enter-audit",
            "plan_name": "Free",
            "searches_per_month": 250,
            "total_searches_left": 240,
            "this_month_usage": 10,
            "account_rate_limit_per_hour": 50,
        }
        with mock.patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}, clear=False):
            with mock.patch.object(search, "request_json", return_value=payload):
                status = search.serpapi_account_status(10, 0, stage="before")
        audit = search.rate_limit_snapshot()["serpapi-account"]
        self.assertEqual(status["remaining"], 240)
        self.assertEqual(audit["remaining_before"], 240)
        self.assertNotIn("api_key", audit)

    def test_rigorous_source_plan_routes_by_database_role(self):
        concepts = [
            search.parse_concept("topic:phenomenon=technology adoption|process innovation"),
            search.parse_concept("context:industry=manufacturing firms|service firms"),
            search.parse_concept("method:method=panel data|difference in differences"),
        ]
        generic = search.generate_query_variants("technology adoption manufacturing panel data", concepts, "comprehensive", 24)
        sources = [
            "scopus", "google-scholar", "sciencedirect",
            "openalex", "semantic-scholar", "crossref",
        ]
        plan = search.build_source_query_plan(
            "technology adoption manufacturing panel data",
            concepts,
            generic,
            "comprehensive",
            "rigorous",
            sources,
        )
        self.assertIn("wos", plan)
        self.assertIn(" OR ", plan["scopus"][0]["query"])
        self.assertLessEqual(len(plan["scopus"]), 4)
        self.assertLessEqual(len(plan["google-scholar"]), 4)
        self.assertLessEqual(len(plan["sciencedirect"]), 2)
        self.assertLessEqual(len(plan["openalex"]), 2)
        self.assertEqual(len(plan["semantic-scholar"]), 1)
        self.assertEqual(plan["crossref"], [])
        self.assertEqual(plan["openalex"][0]["stage"], "coverage_audit")

    def test_rigorous_dry_run_is_network_free_and_rejects_bad_source(self):
        output = io.StringIO()
        with mock.patch.object(search, "request_json") as request:
            with contextlib.redirect_stdout(output):
                result = search.main([
                    "energy transition investment",
                    "--profile", "rigorous",
                    "--mode", "comprehensive",
                    "--sources", "auto",
                    "--dry-run",
                ])
        self.assertEqual(result, 0)
        request.assert_not_called()
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["profile"], "rigorous")
        self.assertEqual(plan["source_roles"]["scopus"]["role"], "structured_core")
        self.assertFalse(plan["resource_policy"]["agent"]["preflight_quota_check"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                search.main(["test query", "--sources", "bogus", "--dry-run"])

    def test_query_stats_separate_exact_retrieval_from_provider_estimate(self):
        def fake_source(query, *_args):
            return [record(f"Paper {index}", f"10.1000/{index}") for index in range(3)], 1000

        options = SimpleNamespace(
            journal=[], per_query=5, year_from=None, year_to=None,
            doc_type=None, language=None, mailto="test@example.com",
            timeout=10, retries=0,
        )
        with mock.patch.dict(search.SOURCE_FUNCTIONS, {"crossref": fake_source}):
            _source, _records, stats, _notices = search.run_source(
                "crossref", ["test query"], options
            )
        self.assertEqual(stats[0]["exact_retrieved_count"], 3)
        self.assertEqual(stats[0]["provider_reported_total"], 1000)
        self.assertEqual(stats[0]["provider_total_kind"], "reported_result_count")

    def test_verification_levels_are_conservative(self):
        core = record("Core", "10.1000/core", source="scopus")
        publisher = record("Publisher", "10.1000/publisher", source="sciencedirect")
        gray = record("Thesis", source="google-scholar")
        gray["work_type"] = "doctoral thesis"
        auxiliary = record("Auxiliary", "10.1000/aux", source="crossref")
        search.assign_verification_status([core, publisher, gray, auxiliary])
        self.assertEqual(core["verification_level"], "V1")
        self.assertEqual(publisher["verification_level"], "V2")
        self.assertEqual(gray["verification_level"], "V3")
        self.assertEqual(auxiliary["verification_level"], "V4")
        self.assertFalse(auxiliary["claim_eligible"])

        scopus_copy = record("Auxiliary", "10.1000/aux", source="scopus")
        merged, _duplicates = search.deduplicate([auxiliary, scopus_copy])
        search.assign_verification_status(merged)
        self.assertEqual(merged[0]["verification_level"], "V1")

    def test_title_fallback_does_not_merge_different_dois(self):
        first = record("A shared title", "10.1000/first")
        second = record("A shared title", "10.1000/second")
        unique, duplicates = search.deduplicate([first, second])
        self.assertEqual(len(unique), 2)
        self.assertEqual(duplicates, 0)

    def test_title_fallback_requires_compatible_first_author_surname(self):
        first = record("A shared title")
        second = record("A shared title")
        first["authors"] = ["John Wang"]
        second["authors"] = ["John Zhang"]
        unique, duplicates = search.deduplicate([first, second])
        self.assertEqual(len(unique), 2)
        self.assertEqual(duplicates, 0)

        reordered = record("A shared title")
        reordered["authors"] = ["Wang, John"]
        unique, duplicates = search.deduplicate([first, reordered])
        self.assertEqual(len(unique), 1)
        self.assertEqual(duplicates, 1)

    def test_scientific_reports_is_not_gray_literature(self):
        article = record("Journal article", source="google-scholar")
        article["journal"] = "Scientific Reports"
        article["work_type"] = "journal article"
        search.assign_verification_status([article])
        self.assertEqual(article["verification_level"], "V4")

    def test_route_contributions_keep_source_identity(self):
        left = record("Left", source="scopus")
        right = record("Right", source="google-scholar")
        left["hits"][0]["route"] = "core_intersection"
        right["hits"][0]["route"] = "core_intersection"
        _source_counts, route_counts = search.source_contributions([left, right])
        self.assertEqual(route_counts["scopus::core_intersection"], 1)
        self.assertEqual(route_counts["google-scholar::core_intersection"], 1)

    def test_core_status_requires_every_planned_route(self):
        roles = search.effective_source_roles("rigorous")
        plan = {
            "wos": [
                {"route": "core", "query": "TS=(a)"},
                {"route": "supplement", "query": "TS=(b)"},
            ],
            "scopus": [
                {"route": "core", "query": "a"},
                {"route": "supplement", "query": "b"},
            ],
            "google-scholar": [
                {"route": "core", "query": "a"},
                {"route": "supplement", "query": "b"},
            ],
        }
        stats = [
            {"source": "scopus", "route": "core"},
            {"source": "google-scholar", "route": "core"},
        ]
        status = search.source_status_report(
            ["scopus", "google-scholar"],
            plan,
            stats,
            [],
            roles,
            {"core"},
            0,
            {"mode": "none"},
        )
        self.assertEqual(status["wos"]["status"], "partial_import")
        self.assertEqual(status["scopus"]["status"], "partial")
        self.assertEqual(status["google-scholar"]["status"], "partial")

        complete_stats = stats + [
            {"source": "scopus", "route": "supplement"},
            {"source": "google-scholar", "route": "supplement"},
        ]
        complete = search.source_status_report(
            ["scopus", "google-scholar"],
            plan,
            complete_stats,
            [],
            roles,
            {"core", "supplement"},
            0,
            {"mode": "none"},
        )
        self.assertEqual(complete["wos"]["status"], "completed_by_import")
        self.assertEqual(complete["scopus"]["status"], "completed")
        self.assertEqual(complete["google-scholar"]["status"], "completed")

    def test_legacy_crossref_role_is_explicit_topic_discovery(self):
        roles = search.effective_source_roles("legacy")
        plan = search.build_source_query_plan(
            "test query", [], ["test query"], "quick", "legacy", ["crossref"]
        )
        self.assertEqual(roles["crossref"]["role"], "legacy_topic_discovery")
        self.assertEqual(plan["crossref"][0]["stage"], "legacy_discovery")

    def test_wos_import_route_mapping_is_explicit(self):
        valid = {"core_intersection", "leave_out:method"}
        route, path = search.parse_wos_import_spec(
            "core_intersection=/tmp/core.bib", valid
        )
        self.assertEqual(route, "core_intersection")
        self.assertEqual(path, "/tmp/core.bib")
        route, path = search.parse_wos_import_spec("/tmp/combined.bib", valid)
        self.assertEqual(route, "")
        self.assertEqual(path, "/tmp/combined.bib")

    def test_crossref_verification_does_not_become_discovery_source(self):
        candidate = record("Candidate", "10.1000/candidate", source="openalex")
        metadata = record("Candidate", "10.1000/candidate", source="crossref")
        metadata["crossref_verified"] = True
        metadata["verification_sources"] = ["crossref"]
        with mock.patch.object(search, "fetch_crossref_metadata", return_value=metadata):
            report = search.verify_crossref_records(
                [candidate], 1, "test@example.com", 10, 0
            )
        self.assertEqual(report["verified"], 1)
        self.assertEqual(candidate["sources"], ["openalex"])
        self.assertEqual(candidate["verification_sources"], ["crossref"])
        search.assign_verification_status([candidate])
        self.assertEqual(candidate["verification_level"], "V4")

    def test_rigorous_main_emits_auditable_screening_pool(self):
        def fake_run(source, routes, _options):
            item = record(
                f"{source} paper",
                f"10.1000/{source.replace('-', '')}",
                source=source,
            )
            route = search.route_record(source, routes[0])
            stats = [{
                "source": source,
                "query": route["query"],
                "returned": 1,
                "estimated_total": 100,
                "exact_retrieved_count": 1,
                "provider_estimated_total": 100,
                "route": route["route"],
                "stage": route["stage"],
                "purpose": route["purpose"],
            }]
            return source, [item], stats, []

        output = io.StringIO()
        with mock.patch.object(search, "run_source", side_effect=fake_run):
            with contextlib.redirect_stdout(output):
                result = search.main([
                    "energy transition investment",
                    "--profile", "rigorous",
                    "--sources", "scopus,google-scholar",
                    "--crossref-verify", "none",
                    "--limit", "10",
                ])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        audit = payload["audit"]
        self.assertEqual(payload["results_role"], "ranked_preview")
        self.assertEqual(audit["raw_record_count"], 2)
        self.assertEqual(audit["unique_record_count"], 2)
        self.assertEqual(audit["screening_pool_count"], 2)
        self.assertFalse(audit["core_search_complete"])
        self.assertEqual(sum(audit["verification_level_counts"].values()), 2)
        self.assertFalse(audit["resource_policy"]["agent"]["preflight_quota_check"])


if __name__ == "__main__":
    unittest.main()
