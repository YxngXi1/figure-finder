"""Terminal output, JSON dump, optional HTML contact sheet and figure PDF."""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from typing import List, Optional

from PIL import Image

from .search import Candidate
from .selection import rejection_reason, suitable_candidates

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, CYAN = "\033[32m", "\033[33m", "\033[31m", "\033[36m"


def _colour(score: float) -> str:
    return GREEN if score >= 70 else YELLOW if score >= 50 else RED


def _bar(score: float, width: int = 24) -> str:
    filled = int(round(score / 100 * width))
    return "█" * filled + "·" * (width - filled)


def print_plan(plan: dict) -> None:
    print(f"\n{BOLD}Concept{RESET}  {plan.get('concept', '')}")
    print(f"{DIM}{plan.get('figure_brief', '')}{RESET}\n")
    print(f"{BOLD}Must show{RESET}")
    for m in plan.get("must_show", []):
        print(f"  • {m}")
    if plan.get("should_avoid"):
        print(f"{BOLD}Watch out for{RESET}")
        for m in plan["should_avoid"]:
            print(f"  ✗ {m}")
    print(f"\n{BOLD}Searching{RESET}")


def print_results(results: List[Candidate], plan: dict, top_n: int,
                  pick: Optional[dict] = None) -> None:
    scored_count = len(results)
    results = suitable_candidates(results)
    if not results:
        print(f"\n{YELLOW}No suitable figure found.{RESET} "
              "None of the candidates passed the relevance and quality checks. "
              "Try a shorter topic or add another source.")
        return

    print(f"\n{BOLD}Top {min(top_n, len(results))} of {len(results)} suitable figures "
          f"({scored_count} scored){RESET}\n")

    for rank, c in enumerate(results[:top_n], 1):
        col = _colour(c.final_score)
        marker = " ★" if pick and pick.get("winner_id") == c.id else ""
        print(f"{BOLD}{rank}. {col}{c.final_score:>5.1f}{RESET} {_bar(c.final_score)}{marker}")
        print(f"   {c.verdict}")
        meta = f"{c.real_width}×{c.real_height} · {c.host} · via {c.source}"
        if c.license:
            meta += f" · {c.license}"
        if c.is_vector:
            meta += " · vector"
        print(f"   {DIM}{meta}{RESET}")
        print(f"   {CYAN}Direct image link:{RESET} {c.image_url}")
        if c.page_url:
            print(f"   {CYAN}page {RESET} {c.page_url}")

        parts = [f"{k}:{v}" for k, v in c.scores.items()]
        print(f"   {DIM}{'  '.join(parts)}{RESET}")

        if c.elements_missing:
            print(f"   {YELLOW}missing:{RESET} {', '.join(c.elements_missing)}")
        raised = [k for k, v in c.flags.items() if v]
        if raised:
            print(f"   {RED}flags:{RESET} {', '.join(raised)}")
        if c.caveat:
            print(f"   {DIM}caveat: {c.caveat}{RESET}")
        print()

    winner = next(
        (c for c in results if pick and c.id == pick.get("winner_id")), None
    )
    selected = winner or results[0]
    label = "Recommended image link" if winner else "Top-ranked image link"
    print(f"{BOLD}{label}:{RESET}\n{selected.image_url}")
    if selected.page_url:
        print(f"{CYAN}Source page:{RESET} {selected.page_url}")
    if winner and pick.get("why"):
        print(f"{pick['why']}")
    print()


def write_json(path: str, notes: str, plan: dict, results: List[Candidate],
               pick: Optional[dict]) -> None:
    suitable = suitable_candidates(results)
    if pick and not any(c.id == pick.get("winner_id") for c in suitable):
        pick = None
    payload = {
        "notes": notes,
        "plan": plan,
        "pick": pick,
        "results": [
            {
                k: v for k, v in asdict(c).items()
                if k not in ("dhash", "local_path", "thumbnail")
            }
            for c in suitable
        ],
        "rejected_results": [
            {**{k: v for k, v in asdict(c).items()
                if k not in ("dhash", "local_path", "thumbnail")},
             "rejected": rejection_reason(c)}
            for c in results if rejection_reason(c)
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)


def write_pdf(path: str, plan: dict, results: List[Candidate],
              pick: Optional[dict] = None) -> bool:
    """Save the recommended, locally reviewed image as a single-page PDF.

    Uses the same slide-scale image the grader saw (up to 1024px by default).
    No image is downloaded again and an unsuitable judge pick cannot be exported.
    """
    suitable = suitable_candidates(results)
    if not suitable:
        return False
    winner = next((c for c in suitable if pick and c.id == pick.get("winner_id")), None)
    selected = winner or suitable[0]
    if not selected.local_path:
        raise ValueError("The selected figure has no downloaded image to export.")
    with Image.open(selected.local_path) as image:
        image.convert("RGB").save(
            path, "PDF", resolution=150.0, quality=95,
            title=plan.get("concept") or "Selected figure",
            subject=f"Source: {selected.page_url or selected.image_url}",
        )
    return True


HTML_TEMPLATE = """<!doctype html>
<meta charset="utf-8"><title>Figure candidates — {concept}</title>
<style>
 body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      margin:0;padding:32px;background:#faf9f7;color:#22201d}}
 h1{{font-size:22px;margin:0 0 4px}} .brief{{color:#6b6560;margin-bottom:28px;max-width:60ch}}
 .card{{display:grid;grid-template-columns:340px 1fr;gap:20px;background:#fff;
       border:1px solid #e6e2dd;border-radius:10px;padding:16px;margin-bottom:16px}}
 .card img{{width:100%;border-radius:6px;background:#f2efeb}}
 .score{{font-size:26px;font-weight:600}} .meta{{color:#8a837c;font-size:13px}}
 .flag{{display:inline-block;background:#fde8e6;color:#a4231a;border-radius:4px;
       padding:1px 7px;font-size:12px;margin-right:5px}}
 .miss{{display:inline-block;background:#fdf3e0;color:#8a5a09;border-radius:4px;
       padding:1px 7px;font-size:12px;margin-right:5px}}
 .crit{{font-size:12px;color:#6b6560;margin-top:8px}}
 a{{color:#3a6ea5}}
 @media (prefers-color-scheme:dark){{
   body{{background:#1a1917;color:#e8e4df}} .card{{background:#232120;border-color:#35322f}}
   .meta,.crit{{color:#9a938c}} .brief{{color:#9a938c}}}}
</style>
<h1>{concept}</h1><div class="brief">{brief}</div>
{cards}
"""


def write_html(path: str, plan: dict, results: List[Candidate], top_n: int) -> None:
    results = suitable_candidates(results)
    cards = []
    for rank, c in enumerate(results[:top_n], 1):
        flags = "".join(
            f'<span class="flag">{html.escape(k)}</span>' for k, v in c.flags.items() if v
        )
        miss = "".join(
            f'<span class="miss">missing: {html.escape(m)}</span>' for m in c.elements_missing
        )
        crits = " · ".join(f"{k} {v}" for k, v in c.scores.items())
        cards.append(f"""
<div class="card">
  <a href="{html.escape(c.image_url)}"><img src="{html.escape(c.image_url)}" loading="lazy"></a>
  <div>
    <div class="score">{rank}. {c.final_score}</div>
    <p>{html.escape(c.verdict)}</p>
    <div>{flags}{miss}</div>
    <div class="crit">{html.escape(crits)}</div>
    <p class="meta">{c.real_width}×{c.real_height} · {html.escape(c.host)} ·
      via {html.escape(c.source)} {html.escape(c.license)}<br>
      <a href="{html.escape(c.page_url or c.image_url)}">source page</a> ·
      <a href="{html.escape(c.image_url)}">direct image</a></p>
  </div>
</div>""")
    with open(path, "w") as f:
        f.write(HTML_TEMPLATE.format(
            concept=html.escape(plan.get("concept", "")),
            brief=html.escape(plan.get("figure_brief", "")),
            cards="".join(cards) or "<p>No suitable figure found. Try a shorter topic or add another source.</p>",
        ))
