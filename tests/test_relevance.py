"""Offline regressions for book covers being returned as diagrams."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from figurefinder import cli, fetch, llm, report, rubric, search, selection


def candidate(**overrides):
    values = dict(id="diagram", image_url="https://example.org/cell.svg",
                  scores={c.key: 5 for c in rubric.CRITERIA}, final_score=90,
                  elements_found=["nucleus", "mitochondria", "cell membrane"])
    values.update(overrides)
    return search.Candidate(**values)


class RelevanceTests(unittest.TestCase):
    def test_document_files_are_not_images(self):
        for mime in ("image/vnd.djvu", "image/x-djvu", "application/pdf"):
            with self.subTest(mime=mime):
                response = mock.Mock()
                response.json.return_value = {"query": {"pages": {"1": {
                    "title": "File:Zoology.djvu", "imageinfo": [{
                        "url": "https://example.org/book.djvu", "mime": mime,
                        "thumburl": "https://example.org/cover.jpg"}]}}}}
                with mock.patch.object(search.requests, "get", return_value=response):
                    self.assertEqual(search.wikimedia("animal cell", 10), [])
        book = candidate(image_url="https://example.org/book.DJVU?download=1", mime="")
        with mock.patch.dict(search.SOURCE_FUNCS, {"wikimedia": lambda q, n: [book]}):
            self.assertEqual(search.collect_candidates(["cell"], ["wikimedia"], False), [])

    def test_commons_svg_preview_survives_without_svg_converter(self):
        data = io.BytesIO()
        Image.new("RGB", (800, 600)).save(data, format="PNG")
        c = candidate(mime="image/svg+xml", width=1600, height=1200)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(fetch, "_download", return_value=data.getvalue()), \
                mock.patch.object(fetch, "_svg_to_png", side_effect=AssertionError("PNG is already rasterised")):
            usable = fetch.hydrate([c], directory, verbose=False)
            self.assertEqual(usable, [c])
            self.assertTrue(c.is_vector)
            self.assertEqual((c.real_width, c.real_height), (1600, 1200))
            self.assertTrue(Path(c.local_path).is_file())

    def test_hard_veto_stays_zero_even_on_reputable_source(self):
        for flag in ("wrong_subject", "book_cover_or_title_page"):
            c = candidate(host="commons.wikimedia.org", flags={flag: True})
            self.assertEqual(llm.compute_final_score(c), 0)
            self.assertEqual(selection.suitable_candidates([c]), [])

    def test_all_rejected_outputs_have_no_recommendation(self):
        # Mirrors the saved incident: model said wrong_subject; score was 1.0.
        cover = candidate(id="cover", image_url="https://example.org/cover.jpg",
                          scores={c.key: 0 for c in rubric.CRITERIA},
                          flags={"wrong_subject": True}, final_score=1.0)
        output = io.StringIO()
        pick = {"winner_id": "cover", "why": "invalid pick"}
        with contextlib.redirect_stdout(output):
            report.print_results([cover], {}, 5, pick)
        self.assertIn("No suitable figure found", output.getvalue())
        self.assertNotIn(cover.image_url, output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            html_path, json_path = Path(directory) / "report.html", Path(directory) / "results.json"
            report.write_html(html_path, {}, [cover], 5)
            self.assertNotIn(cover.image_url, html_path.read_text())
            report.write_json(json_path, "cell", {}, [cover], pick)
            payload = json.loads(json_path.read_text())
            self.assertEqual(payload["results"], [])
            self.assertIsNone(payload["pick"])
            self.assertEqual(payload["rejected_results"][0]["rejected"], "wrong_subject")

    def test_good_diagram_survives_and_invalid_pick_cannot_override_it(self):
        good = candidate()
        cover = candidate(id="cover", image_url="https://example.org/cover.jpg",
                          flags={"book_cover_or_title_page": True}, final_score=99)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report.print_results([cover, good], {}, 5, {"winner_id": "cover"})
        self.assertIn(good.image_url, output.getvalue())
        self.assertNotIn(cover.image_url, output.getvalue())

    def test_quality_cannot_compensate_for_wrong_subject_or_visual_type(self):
        for changes in (
            {"scores": {**candidate().scores, "subject_match": 2}},
            {"scores": {**candidate().scores, "checklist_coverage": 0}},
            {"flags": {"photo_when_diagram_needed": True}},
            {"final_score": 49},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(selection.suitable_candidates([candidate(**changes)]), [])

    def test_page_capture_is_a_penalty_not_an_automatic_rejection(self):
        c = candidate(flags={"screenshot_or_page_capture": True}, final_score=60)
        self.assertEqual(selection.suitable_candidates([c]), [c])

    def test_label_failures_cannot_be_recommended_despite_perfect_scores(self):
        self.assertEqual(cli.build_parser().parse_args(["animal cell"]).language, "English")
        for flag in ("foreign_language_labels", "missing_required_labels", "unreadable_text"):
            with self.subTest(flag=flag):
                c = candidate(host="commons.wikimedia.org", flags={flag: True}, final_score=100)
                self.assertEqual(llm.compute_final_score(c), 0)
                self.assertEqual(selection.suitable_candidates([c]), [])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    report.print_results([c], {}, 5, {"winner_id": c.id})
                self.assertIn("No suitable figure found", output.getvalue())
                self.assertNotIn(c.image_url, output.getvalue())

    def test_cli_skips_judge_and_exits_unsuccessfully_when_all_rejected(self):
        cover = candidate(flags={"wrong_subject": True})
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_load_dotenv"))
            stack.enter_context(mock.patch.object(cli, "_require_env", return_value=0))
            stack.enter_context(mock.patch.object(llm, "get_provider", return_value=mock.Mock(name="provider")))
            stack.enter_context(mock.patch.object(llm, "models_for", return_value={"scorer": "test"}))
            stack.enter_context(mock.patch.object(llm, "make_plan", return_value={"queries": [{"q": "animal cell diagram", "angle": "core"}]}))
            stack.enter_context(mock.patch.object(search, "collect_candidates", return_value=[cover]))
            stack.enter_context(mock.patch.object(fetch, "hydrate", return_value=[cover]))
            stack.enter_context(mock.patch.object(llm, "score_candidates", return_value=[cover]))
            judge = stack.enter_context(mock.patch.object(llm, "judge"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            rc = cli.main(["animal cell", "--sources", "wikimedia", "--out", directory, "--html"])
            self.assertEqual(rc, 1)
            judge.assert_not_called()
            self.assertEqual(json.loads((Path(directory) / "results.json").read_text())["results"], [])


if __name__ == "__main__":
    unittest.main()
