"""Offline audit trail regressions: real reasons, fallback selection, private previews."""

import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from figurefinder import candidate_debug, report, search, slides


def candidate(cid, **extra):
    return dict(id=cid, title=cid, image_url=f"https://example.org/{cid}.png",
                real_width=800, real_height=600, **extra)


class CandidateDebugTests(unittest.TestCase):
    def test_explains_rejection_and_limits_without_inventing_ai_scores(self):
        data = candidate_debug.summarize(dict(
            results=[candidate("good", final_score=80, scores={"subject_match": 5})],
            considered_candidates=[
                candidate("good", final_score=80, scores={"subject_match": 5}),
                candidate("wrong", rejected="wrong_subject", final_score=0,
                          scores={"subject_match": 0}, flags={"wrong_subject": True}),
                candidate("capped", rejected="Not graded: limit reached.", final_score=0)],
            slides_selection={"selected_id": "good"}))
        rows = {row["id"]: row for row in data["candidates"]}
        self.assertEqual(rows["good"]["status"], "selected")
        self.assertEqual(rows["wrong"]["reason"], "wrong subject")
        self.assertEqual(rows["capped"]["status"], "not graded")
        self.assertIsNone(rows["capped"]["score"])

    def test_judge_winner_can_be_unavailable_and_fallback_selected(self):
        payload = dict(results=[candidate("a", scores={"subject_match": 5}, final_score=90),
                                candidate("b", scores={"subject_match": 4}, final_score=80)],
            pick={"winner_id": "a", "runner_up_id": "b", "why": "A shows clearer labels."},
            slides_selection={"selected_id": "b", "unavailable_ids": ["a"]})
        debug = candidate_debug.summarize(payload)
        self.assertEqual(debug["candidates"][0]["id"], "b")
        self.assertEqual(debug["candidates"][0]["status"], "selected")
        self.assertEqual(debug["candidates"][1]["status"], "unavailable")
        self.assertEqual(debug["comparison"], "A shows clearer labels.")

    def test_safe_links_and_bounded_local_previews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            Image.new("RGB", (1000, 1000), "red").save(root / "images" / "ok.jpg")
            debug = candidate_debug.summarize(dict(results=[
                candidate("ok", page_url="javascript:alert(1)"),
                candidate("../outside")]))
            result = candidate_debug.with_previews(debug, root)
            self.assertEqual(len(result["candidates"]), 1)
            self.assertEqual(result["candidates"][0]["source_url"], "https://example.org/ok.png")
            self.assertTrue(result["candidates"][0]["preview"].startswith("data:image/jpeg;base64,"))
            self.assertNotIn("preview", debug["candidates"][0])
            self.assertLess(len(result["candidates"][0]["preview"]), 47000)

    def test_report_preserves_ungraded_candidates_and_existing_result_contract(self):
        good = search.Candidate(id="a", scores={"subject_match": 5, "checklist_coverage": 5}, final_score=90)
        capped = search.Candidate(id="b", rejected="Not downloaded: limit reached.", local_path="/private/file")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            report.write_json(path, "notes", {}, [good], None, [good, capped])
            payload = json.loads(path.read_text())
            self.assertEqual([c["id"] for c in payload["results"]], ["a"])
            self.assertEqual(payload["considered_candidates"][1]["rejected"], capped.rejected)
            self.assertNotIn("local_path", payload["considered_candidates"][1])

    def test_slides_records_failed_source_and_actual_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results.json").write_text(json.dumps(dict(
                results=[candidate("a"), candidate("b")], pick={"winner_id": "a", "why": "Sharper."})))
            args = SimpleNamespace(audience="students", language="English", provider="openai",
                                   sources="wikimedia", queries=2, max_fetched=8, max_scored=4)
            with mock.patch.object(slides.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 mock.patch.object(slides, "check_remote_image", side_effect=[ValueError("private exception"), None]):
                figure = slides.find_figure(dict(target_block_ids=[], figure_brief="heart", visual_style="diagram"), [], args, root)
            self.assertEqual(figure["candidate_id"], "b")
            debug = candidate_debug.read_search(root)
            self.assertEqual(debug["candidates"][0]["id"], "b")
            self.assertNotIn("private exception", json.dumps(debug))

    def test_all_rejected_legacy_result_still_has_debug(self):
        debug = candidate_debug.summarize(dict(results=[], rejected_results=[
            candidate("wrong", rejected="wrong_subject", scores={"subject_match": 0}, final_score=0)]))
        self.assertEqual(debug["candidates"][0]["status"], "rejected")
        self.assertIsNone(debug["winner_id"])

    def test_debug_is_available_only_to_owner_and_deleted_with_run(self):
        from figurefinder.beta import create_app
        with mock.patch("figurefinder.cli._load_dotenv"):
            app = create_app(dict(TESTING=True, SECRET_KEY="test-session-secret-" * 3,
                PASSWORD="only-a-test-password", SESSION_COOKIE_SECURE=False))
        owner, other = app.test_client(), app.test_client()
        def login(client):
            client.get("/login")
            with client.session_transaction() as session:
                csrf = session["csrf"]
            client.post("/login", data={"csrf": csrf, "password": "only-a-test-password"})
        login(owner)
        login(other)
        with owner.session_transaction() as session:
            owner_id, csrf = session["owner"], session["csrf"]
        jobs = app.extensions["beta_jobs"]
        jobs.rows["private-run"] = dict(id="private-run", owner=owner_id, status="done",
            created=time.time(), result={"slides": [{"candidate_debug": {"comparison": "private result"}}]})
        self.assertIn("private result", owner.get("/api/jobs/private-run").get_data(as_text=True))
        self.assertEqual(other.get("/api/jobs/private-run").status_code, 404)
        self.assertEqual(other.get("/api/jobs").json["jobs"], [])
        self.assertEqual(app.test_client().get("/api/jobs/private-run").status_code, 401)
        self.assertEqual(owner.post("/api/jobs/private-run/delete", json={},
                         headers={"X-CSRF-Token": csrf}).status_code, 200)
        self.assertEqual(owner.get("/api/jobs/private-run").status_code, 404)


if __name__ == "__main__":
    unittest.main()
