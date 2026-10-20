"""Explain recorded selection decisions without inventing extra AI reasoning."""

import base64
import io
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

from PIL import Image


def safe_url(value):
    try:
        parsed = urlparse(value or "")
        return value if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username else ""
    except ValueError:
        return ""


def summarize(payload):
    pick = payload.get("pick") or {}
    selection = payload.get("slides_selection") or {}
    chosen = selection.get("selected_id")
    failures = selection.get("unavailable_ids", [])
    eligible = payload.get("results", [])
    ranked_ids = [row["id"] for row in eligible]
    finalists = payload.get("judge_finalist_ids", ranked_ids[:4] if pick else [])
    candidates = payload.get("considered_candidates")
    if candidates is None:  # Older saved search results.
        candidates = eligible + payload.get("rejected_results", [])
    rows = []
    for item in candidates:
        cid = str(item.get("id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", cid):
            continue
        rejected = item.get("rejected")
        if cid in selection.get("duplicate_ids", []):
            status, reason = "duplicate", "This image was already selected for another figure on the same slide."
        elif cid in failures:
            status, reason = "unavailable", "Passed image checks, but its source could not be fetched in a Google Slides-compatible format."
        elif cid == chosen:
            status = "selected"
            reason = ("Selected by the final AI comparison." if cid == pick.get("winner_id") else
                      "First usable candidate in selection order (AI winner first, then score ranking).")
        elif rejected:
            status = "not graded" if not item.get("scores") else "rejected"
            reason = str(rejected).replace("_", " ")
        elif cid in ranked_ids:
            status = "alternative"
            if pick and cid in finalists and cid != pick.get("winner_id"):
                reason = "Passed quality checks; the final AI comparison preferred another candidate."
            elif pick and cid not in finalists:
                reason = "Passed quality checks; ranked outside the finalists sent to the AI comparison."
            elif chosen:
                reason = "Passed quality checks; an earlier candidate in selection order was usable."
            else:
                reason = "Passed quality checks; no final image selection was recorded."
        else:
            status, reason = "not graded", "No grading result was recorded."
        score = item.get("final_score")
        rows.append(dict(id=cid, title=str(item.get("title") or cid)[:300],
            source_url=safe_url(item.get("page_url")) or safe_url(item.get("image_url")),
            status=status, reason=reason[:600],
            score=score if item.get("scores") and type(score) in (float, int) and math.isfinite(score) else None,
            verdict=str(item.get("verdict") or "")[:800], caveat=str(item.get("caveat") or "")[:500],
            missing=[str(x)[:150] for x in item.get("elements_missing", [])[:20]],
            flags=[str(k).replace("_", " ") for k, v in (item.get("flags") or {}).items() if v],
            scores={str(k): v for k, v in (item.get("scores") or {}).items()
                    if type(v) in (int, float) and math.isfinite(v)}))
    rows.sort(key=lambda item: (item["id"] != chosen, item["score"] is None, -(item["score"] or 0)))
    return dict(candidates=rows[:60], total=len(rows),
        comparison=str(pick.get("why") or "")[:1200], winner_id=pick.get("winner_id"),
        runner_up_id=pick.get("runner_up_id"),
        note="Candidates retained after initial search filters. Reasons reflect recorded checks; ungraded images were not evaluated by AI.")


def read_search(directory):
    try:
        payload = json.loads((Path(directory) / "results.json").read_text())
        return summarize(payload)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def with_previews(debug, directory):
    """Embed small local previews before temporary job files are deleted.

    Never load remote URLs in the debug browser or expose filesystem paths.
    Maximum 12 previews, each <= 35 KB of JPEG data per slide.
    """
    debug = {**debug, "candidates": [dict(row) for row in debug["candidates"]]}
    count = 0
    root = Path(directory).resolve()
    for row in debug["candidates"]:
        if count >= 12 or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", row["id"]):
            continue
        path = (root / "images" / (row["id"] + ".jpg")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                image.thumbnail((280, 180))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, "JPEG", quality=65)
            if len(buffer.getvalue()) <= 35000:
                row["preview"] = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                count += 1
        except (OSError, ValueError):
            continue
    return debug
