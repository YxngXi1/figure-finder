"""Offline checks for the diagnostic web preview."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from figurefinder import web


def request_data():
    return {
        "notes": "animal cell",
        "audience": "undergraduate students",
        "style": "clean labelled diagram",
        "language": "English",
        "sources": ["brave"],
        "queries": 3,
        "max_fetched": 24,
        "max_scored": 8,
        "top": 5,
        "no_judge": False,
    }


def results_payload():
    return {
        "notes": "animal cell",
        "plan": {
            "concept": "Animal cell",
            "figure_brief": "A labelled cell diagram.",
            "must_show": ["nucleus", "cell membrane"],
        },
        "pick": {"winner_id": "winner"},
        "results": [
            {
                "id": "fallback",
                "final_score": 91,
                "verdict": "Fallback diagram",
                "host": "example.org",
            },
            {
                "id": "winner",
                "final_score": 88,
                "verdict": "Labelled animal cell",
                "host": "commons.wikimedia.org",
                "page_url": "https://commons.wikimedia.org/example",
                "real_width": 1600,
                "real_height": 1000,
                "license": "CC BY 4.0",
            },
        ],
        "rejected_results": [],
    }


class WebHelpersTests(unittest.TestCase):
    def test_selected_result_honours_valid_pick_and_falls_back(self):
        payload = results_payload()
        self.assertEqual(web.selected_result(payload)["id"], "winner")
        payload["pick"] = {"winner_id": "not-eligible"}
        self.assertEqual(web.selected_result(payload)["id"], "fallback")
        self.assertIsNone(web.selected_result({"results": []}))

    def test_form_validation_and_command_are_bounded(self):
        parsed, error = web._parse_job_request(
            b"notes=animal+cell&max_scored=8&top=5"
        )
        self.assertIsNone(error)
        self.assertEqual(parsed["sources"], ["brave"])
        self.assertTrue(parsed["no_judge"])
        command = web.build_command(parsed, Path("/tmp/specific-job"))
        self.assertIn("figurefinder.cli", command)
        self.assertIn("/tmp/specific-job", command)
        self.assertEqual(command[command.index("--sources") + 1], "brave")
        self.assertIn("--no-judge", command)
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))

        parsed, error = web._parse_job_request(b"notes=cell&max_scored=25")
        self.assertIsNone(parsed)
        self.assertIn("1–24", error)

    def test_job_runner_keeps_cli_output_and_result(self):
        class FakeProcess:
            def __init__(self, command, **kwargs):
                output = Path(command[command.index("--out") + 1])
                (output / "images").mkdir(parents=True, exist_ok=True)
                (output / "results.json").write_text(json.dumps(results_payload()))
                (output / "images" / "winner.jpg").write_bytes(b"jpeg")
                self.stdout = io.StringIO("Planning...\nGrading 2 candidates\n")

            def wait(self):
                return 0

        with tempfile.TemporaryDirectory() as directory:
            state = web.AppState(Path(directory), workers=1)
            job = web.WebJob("a" * 32, request_data(), str(Path(directory) / ("a" * 32)))
            state.jobs[job.id] = job
            with mock.patch.object(web.subprocess, "Popen", FakeProcess):
                state._run_job(job)
            self.assertEqual(job.status, "complete")
            self.assertEqual(job.exit_code, 0)
            self.assertIn("Grading 2 candidates", "\n".join(job.logs))
            self.assertTrue((job.directory / "web-job.json").is_file())
            self.assertIs(state.add_job(request_data()), job)
            state.executor.shutdown(wait=True)

    def test_signed_sessions_expire_and_csrf_is_session_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            state = web.AppState(Path(directory), password="test")
            token = state.create_session()
            self.assertTrue(state.valid_session(token))
            csrf = state.csrf_token(token)
            self.assertTrue(state.valid_csrf(token, csrf))
            self.assertFalse(state.valid_csrf(token, "wrong"))
            self.assertFalse(state.valid_session(token + "changed"))
            state.executor.shutdown(wait=True)


class WebRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = web.AppState(Path(self.temp.name), workers=1, password="test-password")
        job_id = "b" * 32
        self.job = web.WebJob(job_id, request_data(), str(Path(self.temp.name) / job_id), status="complete")
        self.job.directory.mkdir(parents=True)
        (self.job.directory / "images").mkdir()
        (self.job.directory / "results.json").write_text(json.dumps(results_payload()))
        (self.job.directory / "images" / "winner.jpg").write_bytes(b"fake-jpeg-data")
        (self.job.directory / "images" / "winner.download.jpg").write_bytes(b"high-res-jpeg-data")
        self.state.jobs[job_id] = self.job
        try:
            self.server = web.create_server("127.0.0.1", 0, self.state)
        except PermissionError:
            self.state.executor.shutdown(wait=True)
            self.temp.cleanup()
            self.skipTest("local socket binding is disabled in this sandbox")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.state.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_home_job_diagnostics_and_download_routes(self):
        with self.opener.open(self.base + "/", timeout=2) as response:
            login = response.read().decode()
        self.assertIn("Private access", login)

        login_request = Request(
            self.base + "/login",
            data=urlencode({"password": "test-password"}).encode(),
        )
        with self.opener.open(login_request, timeout=2) as response:
            home = response.read().decode()
        self.assertIn("Find the right image", home)
        self.assertIn("Brave Image Search", home)
        self.assertNotIn("Wikimedia", home)
        self.assertNotIn("Recent jobs", home)
        secret = os.environ.get("OPENAI_API_KEY")
        if secret:
            self.assertNotIn(secret, home)

        with self.opener.open(self.base + f"/jobs/{self.job.id}", timeout=2) as response:
            page = response.read().decode()
        self.assertIn("Recommended for your slide", page)
        self.assertIn("Download image", page)
        self.assertIn("Technical diagnostics", page)
        self.assertIn("Sources and other strong options", page)
        self.assertIn("Labelled animal cell", page)

        with self.opener.open(self.base + f"/jobs/{self.job.id}/download", timeout=2) as response:
            self.assertEqual(response.read(), b"high-res-jpeg-data")
            self.assertIn("animal-cell.jpg", response.headers["Content-Disposition"])

        with self.opener.open(self.base + f"/jobs/{self.job.id}/images/winner", timeout=2) as response:
            self.assertEqual(response.read(), b"high-res-jpeg-data")

        with self.opener.open(self.base + f"/jobs/{self.job.id}/status.json", timeout=2) as response:
            status = json.loads(response.read())
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["result_count"], 2)

        with self.assertRaises(HTTPError) as caught:
            self.opener.open(Request(
                self.base + "/jobs",
                data=urlencode({"notes": "cell", "csrf": "invalid"}).encode(),
            ), timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(HTTPError) as caught:
            self.opener.open(Request(
                self.base + "/login",
                data=urlencode({"password": "wrong"}).encode(),
            ), timeout=2)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
