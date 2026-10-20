"""Per-slide limits, multi-image geometry, skip decisions and rerun protection."""

import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from figurefinder import captions, slide_layout, slide_policy, slides
from test_slides import deck, decision, element


def wide_deck(count=3):
    current = deck()
    current["pageSize"] = {axis: {"magnitude": value, "unit": "PT"}
                           for axis, value in (("width", 1280), ("height", 720))}
    current["slides"] = [{"objectId": f"slide{i}", "pageElements": [
        element(f"body{i}", x=30, y=130, w=260, h=400, text="Heart chambers and blood circulation")]}
        for i in range(1, count + 1)]
    return current


def needs(index, count):
    return dict(reason="Different structures need different views.", figures=[
        dict(decision(), target_block_ids=[f"body{index}"], figure_brief=f"View {n}")
        for n in range(1, count + 1)])


def fake_find(decision, blocks, args, directory, excluded_urls=()):
    return dict(url=f"https://example.org/{directory.name}.png", width=1200, height=800,
                title="Heart", preview=f"{directory.name}/images/heart.jpg")


class PerSlideTests(unittest.TestCase):
    def plan(self, current, limit, decisions):
        client = mock.Mock()
        client.get.return_value = current
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(slide_policy, "decide_many", side_effect=decisions), \
             mock.patch.object(slides, "find_figure", side_effect=fake_find), \
             mock.patch.object(captions, "generate", side_effect=lambda number, *a, **k:
                               f"Figure {number}: Heart, a labelled anatomical view."):
            args = SimpleNamespace(out=directory, figures_per_slide=limit, audience="students",
                                   language="English", max_slides=15)
            plan = slides.create_plan(client, "deck_123", mock.Mock(), args)
            self.assertTrue((Path(directory) / "plan.json").exists())
            self.assertTrue((Path(directory) / "review.html").exists())
        return plan

    def test_limit_one_applies_to_each_slide_and_title_is_skipped(self):
        current = wide_deck()
        plan = self.plan(current, 1, [needs(1, 1), dict(reason="Title / transition slide.", figures=[]), needs(3, 1)])
        self.assertEqual([s["status"] for s in plan["slides"]], ["ready", "skipped", "ready"])
        self.assertEqual(len(slides.ready_figures(plan)), 2)
        requests = slides.build_requests(plan, current)
        self.assertEqual(sum("createImage" in r for r in requests), 2)
        self.assertIn("Title / transition", plan["slides"][1]["reason"])

    def test_two_per_slide_are_distinct_and_all_boxes_do_not_overlap(self):
        current = wide_deck(2)
        plan = self.plan(current, 2, [needs(1, 2), needs(2, 2)])
        ready = slides.ready_figures(plan)
        self.assertEqual(len(ready), 4)
        self.assertEqual([r["caption"].split(":")[0] for r in ready],
                         ["Figure 1", "Figure 2", "Figure 3", "Figure 4"])
        for parent in plan["slides"]:
            boxes = [figure[key] for figure in parent["figures"]
                     for key in ("box", "caption_box", "citation_box")]
            for i, box in enumerate(boxes):
                self.assertFalse(any(slide_layout.overlaps(box, other, gap=0) for other in boxes[i+1:]))
        requests = slides.build_requests(plan, current)
        ids = [r["createImage"]["objectId"] for r in requests if "createImage" in r]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(slides.image_id("slide1", 2), ids)

    def test_limit_is_not_a_quota(self):
        current = wide_deck(1)
        plan = self.plan(current, 3, [needs(1, 1)])
        self.assertEqual(len(slides.ready_figures(plan)), 1)

    def test_full_slide_gets_no_figures(self):
        current = wide_deck(1)
        current["slides"][0]["pageElements"] = [element("body1", 0, 0, 1280, 720)]
        plan = self.plan(current, 2, [needs(1, 2)])
        self.assertEqual(slides.ready_figures(plan), [])
        self.assertEqual(plan["slides"][0]["status"], "skipped")

    def test_apply_rejects_inter_figure_overlap_and_duplicate_indices(self):
        current = wide_deck(1)
        plan = self.plan(current, 2, [needs(1, 2)])
        overlap = copy.deepcopy(plan)
        first, second = overlap["slides"][0]["figures"]
        for key in ("box", "caption_box", "citation_box"):
            second[key] = first[key]
        with self.assertRaisesRegex(ValueError, "overlap"):
            slides.build_requests(overlap, current)
        duplicate = copy.deepcopy(plan)
        duplicate["slides"][0]["figures"][1]["figure_index"] = 1
        with self.assertRaisesRegex(ValueError, "duplicate figure"):
            slides.build_requests(duplicate, current)

    def test_repeat_apply_is_noop_and_existing_agent_slide_is_skipped(self):
        current = wide_deck(1)
        plan = self.plan(current, 2, [needs(1, 2)])
        for figure in slides.ready_figures(plan):
            for suffix in ("", "_caption", "_citation"):
                current["slides"][0]["pageElements"].append({"objectId": figure["object_id"] + suffix})
        current["revisionId"] = "new_revision"
        self.assertEqual(slides.build_requests(plan, current), [])
        rerun = self.plan(current, 2, [])
        self.assertEqual(rerun["slides"][0]["status"], "skipped")

    def test_policy_rejects_excess_and_duplicate_proposals(self):
        provider = mock.Mock()
        provider.name = "openai"
        with mock.patch.object(slide_policy.llm, "image_block", return_value={}):
            provider.structured.return_value = needs(1, 2)
            with self.assertRaisesRegex(ValueError, "limit"):
                slide_policy.decide_many("context", "image", {"body1"}, provider, 1)
            provider.structured.return_value = dict(reason="Useful", figures=[needs(1, 1)["figures"][0]] * 2)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                slide_policy.decide_many("context", "image", {"body1"}, provider, 2)
            provider.structured.return_value = dict(reason="Transition slide", figures=[])
            self.assertEqual(slide_policy.decide_many("context", "image", {"body1"}, provider, 3)["figures"], [])

    def test_worker_uses_per_slide_limit_and_groups_figures_in_results(self):
        from figurefinder import beta_worker
        current = wide_deck(2)
        plan = self.plan(current, 2, [needs(1, 2), needs(2, 1)])
        events = []
        payload = dict(token="test-token", deck_id="deck_123", figures_per_slide=2, max_slides=15)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.dict("os.environ", {}, clear=False), \
             mock.patch.object(slides, "create_plan", return_value=plan) as create, \
             mock.patch.object(slides, "apply_plan", return_value={"inserted": 3}), \
             mock.patch("figurefinder.llm.get_provider"), \
             mock.patch.object(beta_worker, "emit", side_effect=lambda kind, **data: events.append((kind, data))):
            self.assertEqual(beta_worker.main(), 0)
        args = create.call_args.args[3]
        self.assertEqual(args.figures_per_slide, 2)
        self.assertFalse(hasattr(args, "max_figures"))
        result = next(data["result"] for kind, data in events if kind == "done")
        self.assertEqual([row["inserted"] for row in result["slides"]], [2, 1])
        self.assertEqual(len(result["slides"][0]["figures"]), 2)
        self.assertEqual(result["errors"], 0)

    def test_web_passes_per_slide_setting_and_rejects_old_total_payload(self):
        from figurefinder.beta import create_app
        with mock.patch("figurefinder.cli._load_dotenv"):
            app = create_app(dict(TESTING=True, SECRET_KEY="test-session-key-" * 3,
                PASSWORD="test-only-password", SESSION_COOKIE_SECURE=False,
                GOOGLE_CLIENT_ID="test-client", MAX_FIGURES=3))
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        client.post("/login", data={"csrf": csrf, "password": "test-only-password"})
        with client.session_transaction() as session:
            csrf = session["csrf"]
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test", "BRAVE_API_KEY": "test"}), \
             mock.patch("figurefinder.beta.verify_google", return_value="Test deck"), \
             mock.patch.object(app.extensions["beta_jobs"], "start", return_value="test-job") as start:
            payload = dict(access_token="test-token-" * 4, deck_id="test_deck_123", figures_per_slide=2)
            response = client.post("/api/jobs", json=payload, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(start.call_args.args[-1], 2)
            payload["figures"] = payload.pop("figures_per_slide")
            self.assertEqual(client.post("/api/jobs", json=payload,
                             headers={"X-CSRF-Token": csrf}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
