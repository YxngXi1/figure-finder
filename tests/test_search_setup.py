"""Offline checks for broad image retrieval and runtime configuration."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from figurefinder import cli, config, llm, search


class BraveSearchTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"BRAVE_API_KEY": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_original_images_and_unknown_dimensions_survive_prefilters(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": [
            {"title": "Cow stomach", "url": "https://lecturer.example/lesson",
             "properties": {"url": "https://cdn.example/stomach.png", "width": 1600, "height": 1000},
             "thumbnail": {"src": "https://thumb.example/small.jpg", "width": 160, "height": 100}},
            {"url": "https://another.example/lesson",
             "properties": {"url": "https://another.example/figure.jpg", "width": None}},
            {"url": "https://example.org/no-original", "properties": None},
            {"properties": {"url": "data:image/png;base64,abcd"}},
        ]}
        with mock.patch.object(search.requests, "get", return_value=response) as get:
            candidates = search.collect_candidates(["ruminant stomach diagram"], ["brave"], False)
        self.assertEqual(len(candidates), 2)
        first, second = candidates
        self.assertEqual(first.fetch_url, "https://cdn.example/stomach.png")
        self.assertEqual(first.page_url, "https://lecturer.example/lesson")
        self.assertEqual(first.host, "lecturer.example")
        self.assertEqual((first.width, first.height), (1600, 1000))
        self.assertEqual((second.width, second.height), (0, 0))
        self.assertEqual(first.source, "brave")
        self.assertEqual(first.found_by, ["ruminant stomach diagram"])
        self.assertEqual(get.call_args.args[0], config.BRAVE_ENDPOINT)
        self.assertEqual(get.call_args.kwargs["headers"]["X-Subscription-Token"], "test-token")
        self.assertEqual(get.call_args.kwargs["params"]["country"], "ALL")
        self.assertEqual(get.call_args.kwargs["params"]["q"], "ruminant stomach diagram")

    def test_search_failures_are_actionable_without_echoing_credentials(self):
        for status, message in [(401, "API key"), (403, "subscription"),
                                (429, "quota"), (500, "HTTP 500")]:
            with self.subTest(status=status):
                response = mock.Mock(status_code=status, text="test-token")
                with mock.patch.object(search.requests, "get", return_value=response):
                    with self.assertRaisesRegex(search.SearchError, message) as caught:
                        search.brave("cell diagram", 20)
                self.assertNotIn("test-token", str(caught.exception))

    def test_network_and_malformed_responses_are_search_errors(self):
        with mock.patch.object(search.requests, "get", side_effect=search.requests.Timeout):
            with self.assertRaisesRegex(search.SearchError, "connection"):
                search.brave("cell", 20)
        for payload in ([], {"results": {}}, {"error": "failed"}):
            response = mock.Mock(status_code=200)
            response.json.return_value = payload
            with self.subTest(payload=payload), mock.patch.object(search.requests, "get", return_value=response):
                with self.assertRaisesRegex(search.SearchError, "unexpected response"):
                    search.brave("cell", 20)
        response.json.side_effect = ValueError("invalid JSON")
        with mock.patch.object(search.requests, "get", return_value=response):
            with self.assertRaisesRegex(search.SearchError, "invalid JSON"):
                search.brave("cell", 20)

    def test_valid_empty_result_is_not_an_error(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": []}
        with mock.patch.object(search.requests, "get", return_value=response):
            self.assertEqual(search.brave("cell", 20), [])

    def test_query_requests_are_parallel_and_results_are_interleaved(self):
        barrier = threading.Barrier(3)

        def source(query, limit):
            barrier.wait(timeout=2)
            return [
                search.Candidate(
                    image_url=f"https://example.org/{query}-{rank}.jpg",
                    title=f"{query}-{rank}", width=1200, height=800,
                )
                for rank in range(2)
            ]

        with mock.patch.dict(search.SOURCE_FUNCS, {"brave": source}, clear=True), \
                mock.patch.object(config, "SEARCH_WORKERS", 3):
            candidates = search.collect_candidates(["one", "two", "three"], ["brave"], False)

        self.assertEqual(
            [candidate.title for candidate in candidates],
            ["one-0", "two-0", "three-0", "one-1", "two-1", "three-1"],
        )


class SetupTests(unittest.TestCase):
    def test_dotenv_overrides_models_and_sources_after_import(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("FF_PLANNER_MODEL=planner-test\nFF_SCORER_MODEL=scorer-test\n"
                            "FF_JUDGE_MODEL=judge-test\nFF_SOURCES=brave,openverse\n")
            cli._load_dotenv(path)
            self.assertEqual(cli.build_parser().parse_args(["cell"]).sources, "brave,openverse")
            self.assertEqual(llm.models_for("openai")["planner"], "planner-test")
            self.assertEqual(llm.models_for("openai")["scorer"], "scorer-test")
            self.assertEqual(llm.models_for("openai")["judge"], "judge-test")
            os.environ["FF_SCORER_MODEL"] = "shell-override"
            cli._load_dotenv(path)
            self.assertEqual(llm.models_for("openai")["scorer"], "shell-override")

    def test_missing_brave_key_stops_before_paid_planning(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(cli, "_load_dotenv"), \
                mock.patch.object(llm, "make_plan") as plan, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["cell", "--sources", "brave"]), 2)
        plan.assert_not_called()
        self.assertIn("BRAVE_API_KEY", errors.getvalue())

    def test_invalid_sources_stop_before_paid_planning(self):
        for source in ("", "typo"):
            with self.subTest(source=source), mock.patch.object(cli, "_load_dotenv"), \
                    mock.patch.object(llm, "make_plan") as plan, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["cell", "--sources", source]), 2)
                plan.assert_not_called()

    def test_dry_run_needs_only_ai_key_and_uses_selected_planner(self):
        provider = mock.Mock()
        provider.name = "openai"
        provider.structured.return_value = {"concept": "cell", "queries": []}
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "FF_PLANNER_MODEL": "planner-test"}, clear=True), \
                mock.patch.object(cli, "_load_dotenv"), \
                mock.patch.object(llm, "get_provider", return_value=provider), \
                mock.patch.object(search, "collect_candidates") as collect, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["cell", "--sources", "brave", "--dry-run"]), 0)
        self.assertEqual(provider.structured.call_args.kwargs["model"], "planner-test")
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
