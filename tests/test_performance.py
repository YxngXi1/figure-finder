"""Offline checks for latency-oriented pipeline concurrency."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from figurefinder import config, llm, rubric, search


class ConcurrentProvider:
    name = "openai"

    def __init__(self):
        self.barrier = threading.Barrier(2)

    def structured(self, **kwargs):
        self.barrier.wait(timeout=2)
        ids = [
            block["text"].split()[2]
            for block in kwargs["blocks"]
            if block["kind"] == "text" and block["text"].startswith("\n--- CANDIDATE ")
        ]
        rows = []
        for candidate_id in ids:
            rows.append({
                "id": candidate_id,
                **{criterion.key: 5 for criterion in rubric.CRITERIA},
                **{name: False for name in rubric.VETO_FLAGS},
                "elements_found": [],
                "elements_missing": [],
                "verdict": "good",
                "caveat": None,
            })
        return {"candidates": rows}


class PerformanceTests(unittest.TestCase):
    def test_vision_batches_can_run_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = []
            for index in range(2):
                image = Path(directory) / f"{index}.jpg"
                Image.new("RGB", (800, 600), "white").save(image)
                candidates.append(search.Candidate(
                    id=f"c{index}", local_path=str(image),
                    image_url=f"https://example.org/{index}.jpg",
                    real_width=800, real_height=600,
                ))
            with mock.patch.object(config, "SCORE_BATCH_SIZE", 1), \
                    mock.patch.object(config, "SCORE_WORKERS", 2):
                results = llm.score_candidates(
                    candidates, {"concept": "cell"}, "students", "diagram", "English",
                    provider=ConcurrentProvider(), verbose=False,
                )
        self.assertEqual(len(results), 2)
        self.assertTrue(all(candidate.final_score > 0 for candidate in results))


if __name__ == "__main__":
    unittest.main()
