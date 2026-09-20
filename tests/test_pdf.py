"""PDF export uses only the eligible recommendation and the local image."""

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from figurefinder import cli, fetch, llm, report, rubric, search


def candidate(path, **overrides):
    values = dict(id="good", local_path=str(path),
                  image_url="https://example.org/cell.jpg",
                  page_url="https://example.org/cell",
                  scores={c.key: 5 for c in rubric.CRITERIA}, final_score=90)
    values.update(overrides)
    return search.Candidate(**values)


class PdfTests(unittest.TestCase):
    def test_export_uses_judge_winner_and_preserves_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cell.jpg"
            Image.new("RGB", (800, 400), "white").save(image)
            # The higher-ranked candidate has no local file: success proves that
            # the eligible judge winner's file was used instead.
            first = candidate("missing.jpg", id="first")
            winner = candidate(image)
            pdf = Path(directory) / "figure.pdf"
            self.assertTrue(report.write_pdf(pdf, {"concept": "Animal cell"},
                                            [first, winner], {"winner_id": winner.id}))
            content = pdf.read_bytes()
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertIn(b"/Count 1", content)
            self.assertIn(b"/MediaBox [ 0 0 384.0 192.0 ]", content)
            self.assertTrue("Animal cell".encode("utf-16-be") in content)
            self.assertTrue("https://example.org/cell".encode("utf-16-be") in content)

    def test_invalid_judge_pick_falls_back_and_all_rejected_creates_no_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cell.jpg"
            Image.new("RGB", (800, 600), "white").save(image)
            good = candidate(image)
            bad = candidate("missing.jpg", id="bad", flags={"wrong_subject": True})
            pdf = Path(directory) / "figure.pdf"
            self.assertTrue(report.write_pdf(pdf, {}, [bad, good], {"winner_id": "bad"}))
            missing = Path(directory) / "rejected.pdf"
            self.assertFalse(report.write_pdf(missing, {}, [bad], {"winner_id": "bad"}))
            self.assertFalse(missing.exists())

    def test_cli_creates_output_folder_and_pdf(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            image = Path(directory) / "cell.jpg"
            Image.new("RGB", (800, 600), "white").save(image)
            good = candidate(image)
            output = Path(directory) / "examples"
            stack.enter_context(mock.patch.object(cli, "_load_dotenv"))
            stack.enter_context(mock.patch.object(cli, "_require_env", return_value=0))
            stack.enter_context(mock.patch.object(llm, "get_provider", return_value=mock.Mock()))
            stack.enter_context(mock.patch.object(llm, "models_for", return_value={"scorer": "test"}))
            stack.enter_context(mock.patch.object(llm, "make_plan", return_value={"concept": "Animal cell", "queries": []}))
            stack.enter_context(mock.patch.object(search, "collect_candidates", return_value=[good]))
            stack.enter_context(mock.patch.object(fetch, "hydrate", return_value=[good]))
            stack.enter_context(mock.patch.object(llm, "score_candidates", return_value=[good]))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(cli.main(["animal cell", "--sources", "wikimedia",
                                       "--out", str(output), "--pdf"]), 0)
            self.assertTrue((output / "figure.pdf").read_bytes().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
