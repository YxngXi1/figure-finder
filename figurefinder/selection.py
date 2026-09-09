"""Require an actual relevant figure before recommending a search result."""

from . import rubric
from .search import Candidate


def rejection_reason(candidate: Candidate):
    if candidate.rejected:
        return candidate.rejected
    for name, spec in rubric.VETO_FLAGS.items():
        if candidate.flags.get(name) and (spec["multiplier"] == 0 or name in {
            "photo_when_diagram_needed", "screenshot_or_page_capture",
        }):
            return name
    if candidate.scores.get("subject_match", 0) < 4:
        return "Does not clearly depict the requested concept."
    if candidate.scores.get("checklist_coverage", 0) < 3:
        return "Too few of the required elements are visible."
    if candidate.final_score < 50:
        return "Below the minimum usable figure score (50/100)."
    return None


def suitable_candidates(results):
    return [c for c in results if rejection_reason(c) is None]
