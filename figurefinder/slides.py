"""Local Slides agent: run end to end, or plan and apply separately."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from . import candidate_debug, captions, citations, cli, config, llm, search, slide_layout, slide_policy, slide_text
from .google_slides import SlidesClient, authorize, check_remote_image, presentation_id, validate_image_url


def image_id(slide_id, figure_index=1):
    # Stable across runs, so the agent cannot repeatedly decorate the same slide.
    base = "ff_figure_" + hashlib.sha256(slide_id.encode()).hexdigest()[:24]
    return base if figure_index == 1 else f"{base}_{figure_index}"


def find_figure(decision, blocks, args, directory, excluded_urls=()):
    directory.mkdir(parents=True, exist_ok=True)
    notes = directory / "notes.txt"
    targets = set(decision["target_block_ids"])
    notes.write_text(decision["figure_brief"] + "\n\nRelevant existing slide text:\n" +
                     "\n".join(block["text"] for block in blocks if block["id"] in targets))
    command = [sys.executable, "-m", "figurefinder.cli", "--notes-file", str(notes),
               "--audience", args.audience, "--language", args.language,
               "--style", decision["visual_style"], "--provider", args.provider,
               "--sources", args.sources, "--queries", str(args.queries),
               "--max-fetched", str(args.max_fetched), "--max-scored", str(args.max_scored),
               "--out", str(directory), "--html"]
    with (directory / "search.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                cwd=Path(__file__).resolve().parents[1], timeout=900)
    if result.returncode:
        raise RuntimeError(f"No successful figure search; see {directory / 'search.log'}")
    payload = json.loads((directory / "results.json").read_text())
    eligible = payload.get("results", [])
    winner_id = (payload.get("pick") or {}).get("winner_id")
    payload["slides_selection"] = {"selected_id": None, "unavailable_ids": [], "duplicate_ids": []}
    def save_selection():
        (directory / "results.json").write_text(json.dumps(payload, indent=2))
    eligible.sort(key=lambda row: row.get("id") != winner_id)
    for candidate in eligible:
        url = candidate.get("download_url") or candidate.get("image_url", "")
        if url in excluded_urls:
            payload["slides_selection"]["duplicate_ids"].append(candidate["id"])
            save_selection()
            continue
        try:
            check_remote_image(url)
        except Exception:
            payload["slides_selection"]["unavailable_ids"].append(candidate["id"])
            save_selection()
            continue
        payload["slides_selection"]["selected_id"] = candidate["id"]
        save_selection()
        return {"candidate_id": candidate["id"], "url": url, "source_page": candidate.get("page_url", ""),
                "license": candidate.get("license", ""), "verdict": candidate.get("verdict", ""),
                "title": candidate.get("title", ""), "creator": candidate.get("creator", ""),
                "attribution": candidate.get("attribution", ""), "license_url": candidate.get("license_url", ""),
                "width": candidate["real_width"], "height": candidate["real_height"],
                "preview": str(Path(directory.name) / "images" / (candidate["id"] + ".jpg"))}
    raise RuntimeError("No eligible figure has a publicly downloadable PNG/JPEG/GIF source.")


def write_plan(plan, directory):
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / "plan.json.tmp"
    temp.write_text(json.dumps(plan, indent=2))
    temp.replace(directory / "plan.json")
    cards = []
    esc = lambda value: html.escape(str(value), quote=True)
    for row in review_rows(plan):
        decision, figure = row.get("decision", {}), row.get("figure", {})
        blocks = "<br>".join(esc(block["text"]) for block in row.get("target_blocks", []))
        image = f'<img src="{esc(figure["preview"])}" alt="Selected figure">' if figure else ""
        screenshot = f'<img src="{esc(row["screenshot"])}" alt="Existing slide">' if row.get("screenshot") else ""
        source = f'<p>Source: <a href="{esc(figure.get("source_page") or figure.get("url", ""))}">{esc(figure.get("source_page") or figure.get("url", ""))}</a></p>' if figure else ""
        if row.get("citation"):
            source = f'<p><a href="{esc(row["citation"]["url"])}">{esc(row["citation"]["text"])}</a></p>'
        cards.append(f'<article><h2>Slide {row["number"]}: {esc(row["status"])}</h2>'
                     f'<p>{esc(decision.get("reason", row.get("reason", "")))}</p>'
                     f'<p>{esc(row.get("reason", ""))}</p><blockquote>{blocks}</blockquote>'
                     f'{screenshot}{image}<p>{esc(row.get("caption", ""))}</p>'
                     f'{source}<p>{esc(figure.get("license", ""))}</p>'
                     f'<p>Placement (x, y, width, height in points): {esc(row.get("box", "—"))}</p></article>')
    (directory / "review.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Slides figure plan</title>'
        '<style>body{font:16px/1.5 system-ui;max-width:1000px;margin:40px auto;padding:20px}'
        'article{border-top:1px solid #ccc;padding:20px 0}img{max-width:46%;max-height:320px;margin:1%}'
        'blockquote{white-space:pre-wrap}a{overflow-wrap:anywhere}</style>'
        f'<h1>{esc(plan["title"])}</h1><p>Policy: {esc(plan["policy_version"])}. '
        'Preview only; the deck has not been edited. Images below show the existing slide and '
        'the selected figure separately; placement is listed in points.</p>' + "".join(cards))


class DeckTooLarge(ValueError):
    """Hosted beta rejects oversized decks before any AI calls."""


def create_plan(client, deck_id, provider, args, progress=None):
    if getattr(args, "figures_per_slide", None) is not None:
        return create_per_slide_plan(client, deck_id, provider, args, progress)
    directory = Path(args.out).resolve()
    if (directory / "plan.json").exists():
        raise ValueError("This output folder already has a plan. Choose a new --out folder.")
    directory.mkdir(parents=True, exist_ok=True)
    deck = client.get(deck_id)
    if not deck.get("revisionId"):
        raise ValueError("Google did not return a revision ID. Sign in with edit access to this deck.")
    width, height = (slide_layout.points(deck["pageSize"][axis]) for axis in ("width", "height"))
    pages = deck.get("slides", [])
    if getattr(args, "max_slides", 0) and len(pages) > args.max_slides:
        raise DeckTooLarge(f"Presentation exceeds the {args.max_slides}-slide limit.")
    texts = [slide_layout.blocks_on(page) for page in pages]
    plan = {"version": 3, "presentation_id": deck_id, "revision_id": deck["revisionId"],
            "title": deck.get("title", deck_id), "policy_version": slide_policy.POLICY_VERSION,
            "policy_text": slide_policy.FIGURE_NEED_POLICY, "slides": []}
    added = 0
    first_figure_number = captions.next_number(deck)
    for index, page in enumerate(pages):
        slide_id = page["objectId"]
        row = {"number": index+1, "slide_id": slide_id, "status": "skipped"}
        plan["slides"].append(row)
        print(f"Slide {index+1}/{len(pages)}…", flush=True)
        if progress:
            progress(index + 1, len(pages))
        try:
            if page.get("slideProperties", {}).get("isSkipped"):
                row["reason"] = "Slide is marked skipped in Google Slides."
            elif any(e["objectId"] == image_id(slide_id) for e in page.get("pageElements", [])):
                row["reason"] = "A figure from this agent is already present."
            elif not texts[index]:
                row["reason"] = "No editable text blocks to ground a figure request."
            elif added >= args.max_figures:
                row["reason"] = "Figure limit reached; increase --max-figures to consider more slides."
            else:
                screenshot = directory / f"slide-{index+1}.jpg"
                client.screenshot(deck_id, slide_id, screenshot)
                row["screenshot"] = screenshot.name
                context = json.dumps({"deck_title": plan["title"], "audience": args.audience,
                    "language": args.language, "current_slide_blocks": texts[index],
                    "previous_slide_blocks": texts[index-1] if index else [],
                    "next_slide_blocks": texts[index+1] if index+1 < len(pages) else []})
                decision = slide_policy.decide(context, str(screenshot),
                                               {b["id"] for b in texts[index]}, provider)
                row["decision"] = decision
                row["target_blocks"] = [b for b in texts[index] if b["id"] in decision["target_block_ids"]]
                if decision["needs_figure"]:
                    slot = slide_layout.free_box(width, height, slide_layout.slide_geometry(deck, page))
                    if slot is None:
                        row["reason"] = "Figure would help, but there is no sufficiently large blank area."
                    else:
                        figure = find_figure(decision, texts[index], args, directory / f"slide-{index+1}")
                        caption = captions.generate(first_figure_number + added,
                            directory / figure["preview"], row["target_blocks"], args.language, provider)
                        citation = citations.make_citation(figure)
                        positions = slide_layout.slide_geometry(deck, page, with_ids=True)
                        targets = [positions[block["id"]] for block in row["target_blocks"] if block["id"] in positions]
                        fitted = None
                        for compact in (False, True):
                            if compact:
                                print("  Shortening caption to fit available space…", flush=True)
                                caption = captions.generate(first_figure_number + added,
                                    directory / figure["preview"], row["target_blocks"], args.language, provider,
                                    max_title_chars=20, max_description_chars=70)
                            for aligned_slot in slide_layout.aligned_slots(slot, targets):
                                candidate_layout = slide_layout.fit_cited_image(aligned_slot, figure["width"],
                                    figure["height"], caption, citation["text"], minimum_width=width*.18)
                                if candidate_layout and candidate_layout[0][3] >= height*.18:
                                    fitted = candidate_layout
                                    break
                            if fitted:
                                break
                        row.update(figure=figure, caption=caption, citation=citation)
                        if fitted is None or fitted[0][2] < width*.18 or fitted[0][3] < height*.18:
                            row["reason"] = "There is not enough room for an aligned image, caption, and citation."
                        else:
                            row.update(status="ready", figure=figure, box=fitted[0], object_id=image_id(slide_id),
                                       caption=caption, caption_box=fitted[1], citation=citation, citation_box=fitted[2])
                            added += 1
        except Exception as exc:
            row.update(status="error", reason=str(exc))
        debug = candidate_debug.read_search(directory / f"slide-{index+1}")
        if debug:
            row["candidate_debug"] = debug
        write_plan(plan, directory)
        explanation = row.get("reason") or row.get("decision", {}).get("reason", "")
        print(f"  {row['status']}: {explanation[:220]}" if row["status"] != "ready"
              else f"  ready: {row['caption']}", flush=True)
    write_plan(plan, directory)
    return plan


def review_rows(plan):
    for slide in plan["slides"]:
        if plan.get("version") == 4 and slide.get("figures"):
            for figure in slide["figures"]:
                yield {**figure, "number": slide["number"], "screenshot": slide.get("screenshot")}
        else:
            yield slide


def placement_slots(slot, targets, remaining):
    """Reserve a share of the blank area before trying the whole area.

    A full-area fallback is allowed: useful readable figures take priority over
    filling the maximum count. Subsequent figures see every occupied box.
    """
    x, y, w, h = slot
    if remaining > 1:
        gap = 12
        shares = [[x, y, (w - gap * (remaining - 1)) / remaining, h],
                  [x, y, w, (h - gap * (remaining - 1)) / remaining]]
        for share in shares:
            if share[2] > 0 and share[3] > 0:
                yield from slide_layout.aligned_slots(share, targets)
    yield from slide_layout.aligned_slots(slot, targets)


def create_per_slide_plan(client, deck_id, provider, args, progress=None):
    limit = args.figures_per_slide
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Figures per slide must be between 1 and 10.")
    directory = Path(args.out).resolve()
    if (directory / "plan.json").exists():
        raise ValueError("This output folder already has a plan. Choose a new --out folder.")
    directory.mkdir(parents=True, exist_ok=True)
    deck = client.get(deck_id)
    if not deck.get("revisionId"):
        raise ValueError("Google did not return a revision ID. Sign in with edit access to this deck.")
    pages = deck.get("slides", [])
    if getattr(args, "max_slides", 0) and len(pages) > args.max_slides:
        raise DeckTooLarge(f"Presentation exceeds the {args.max_slides}-slide limit.")
    width, height = (slide_layout.points(deck["pageSize"][axis]) for axis in ("width", "height"))
    texts = [slide_layout.blocks_on(page) for page in pages]
    plan = dict(version=4, presentation_id=deck_id, revision_id=deck["revisionId"],
                title=deck.get("title", deck_id), policy_version=slide_policy.POLICY_VERSION,
                policy_text=slide_policy.FIGURE_NEED_POLICY, figures_per_slide=limit, slides=[])
    next_number = captions.next_number(deck)
    for index, page in enumerate(pages):
        sid = page["objectId"]
        row = dict(number=index + 1, slide_id=sid, status="skipped", figures=[])
        plan["slides"].append(row)
        if progress:
            progress(index + 1, len(pages))
        print(f"Slide {index + 1}/{len(pages)}…", flush=True)
        try:
            existing = [e["objectId"] for e in page.get("pageElements", [])]
            if page.get("slideProperties", {}).get("isSkipped"):
                row["reason"] = "Slide is marked skipped in Google Slides."
            elif any(oid == image_id(sid) or oid.startswith(image_id(sid) + "_") for oid in existing):
                row["reason"] = "Figures from this agent are already present."
            elif not texts[index]:
                row["reason"] = "No editable text blocks to ground a figure request."
            else:
                screenshot = directory / f"slide-{index+1}.jpg"
                client.screenshot(deck_id, sid, screenshot)
                row["screenshot"] = screenshot.name
                context = json.dumps(dict(deck_title=plan["title"], audience=args.audience,
                    language=args.language, current_slide_blocks=texts[index],
                    previous_slide_blocks=texts[index-1] if index else [],
                    next_slide_blocks=texts[index+1] if index+1 < len(pages) else [],
                    maximum_figures_per_slide=limit))
                needs = slide_policy.decide_many(context, str(screenshot),
                    {b["id"] for b in texts[index]}, provider, limit)
                row["reason"] = needs["reason"]
                occupied = list(slide_layout.slide_geometry(deck, page))
                positions = slide_layout.slide_geometry(deck, page, with_ids=True)
                selected_urls = set()
                for offset, decision in enumerate(needs["figures"]):
                    figure_index = offset + 1
                    attempt = dict(slide_id=sid, figure_index=figure_index, status="skipped",
                        decision=decision, target_blocks=[b for b in texts[index]
                            if b["id"] in decision["target_block_ids"]])
                    row["figures"].append(attempt)
                    search_dir = directory / f"slide-{index+1}-figure-{figure_index}"
                    attempt["search_directory"] = search_dir.name
                    try:
                        slot = slide_layout.free_box(width, height, occupied)
                        if slot is None:
                            attempt["reason"] = "No room remains for another image, caption and citation."
                            break
                        figure = find_figure(decision, texts[index], args, search_dir, excluded_urls=selected_urls)
                        attempt["figure"] = figure
                        citation = citations.make_citation(figure)
                        targets = [positions[b["id"]] for b in attempt["target_blocks"] if b["id"] in positions]
                        fitted = None
                        for compact in (False, True):
                            options = dict(max_title_chars=20, max_description_chars=70) if compact else {}
                            caption = captions.generate(next_number, directory / figure["preview"],
                                attempt["target_blocks"], args.language, provider, **options)
                            for proposed in placement_slots(slot, targets, len(needs["figures"]) - offset):
                                layout = slide_layout.fit_cited_image(proposed, figure["width"], figure["height"],
                                    caption, citation["text"], minimum_width=width * .18)
                                if layout and layout[0][3] >= height * .18:
                                    fitted = layout
                                    break
                            if fitted:
                                break
                        attempt.update(caption=caption, citation=citation)
                        if fitted is None:
                            attempt["reason"] = "Not enough room for a readable image, caption and citation."
                        else:
                            attempt.update(status="ready", box=fitted[0], caption_box=fitted[1],
                                           citation_box=fitted[2], object_id=image_id(sid, figure_index))
                            occupied.extend(fitted)
                            selected_urls.add(figure["url"])
                            next_number += 1
                    except Exception as exc:
                        attempt.update(status="error", reason=str(exc))
                    finally:
                        debug = candidate_debug.read_search(search_dir)
                        if debug:
                            attempt["candidate_debug"] = debug
                statuses = [f["status"] for f in row["figures"]]
                row["status"] = "ready" if "ready" in statuses else "error" if "error" in statuses else "skipped"
                if row["figures"] and row["status"] != "ready":
                    row["reason"] = row["figures"][-1].get("reason", row["reason"])
        except Exception as exc:
            row.update(status="error", reason=str(exc))
        write_plan(plan, directory)
        print(f"  {row['status']}: {sum(f['status'] == 'ready' for f in row['figures'])} figures ready", flush=True)
    return plan


def ready_figures(plan):
    if plan.get("version") != 4:
        return [row for row in plan["slides"] if row["status"] == "ready"]
    result, seen = [], set()
    limit = plan.get("figures_per_slide")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Invalid per-slide figure limit.")
    for row in plan["slides"]:
        sid = row["slide_id"]
        if sid in seen:
            raise ValueError("Duplicate slide in plan.")
        seen.add(sid)
        figures = row.get("figures", [])
        indices = set()
        for figure in figures:
            index = figure.get("figure_index")
            if (figure.get("slide_id") != sid or type(index) is not int
                    or not 1 <= index <= limit or index in indices):
                raise ValueError("Invalid or duplicate figure index on a slide.")
            indices.add(index)
            if figure["status"] == "ready":
                result.append(figure)
    return result


def build_requests(plan, deck):
    if plan.get("version") not in (1, 2, 3, 4) or plan.get("presentation_id") != deck.get("presentationId"):
        raise ValueError("The plan does not match this presentation.")
    ready = ready_figures(plan)
    if not ready:
        return []
    pages = {page["objectId"]: page for page in deck.get("slides", [])}
    existing = {e["objectId"] for page in pages.values() for e in page.get("pageElements", [])}
    def required_ids(row):
        oid = image_id(row["slide_id"], row.get("figure_index", 1) if plan["version"] == 4 else 1)
        ids = {oid}
        if plan["version"] >= 2:
            ids.add(oid + "_caption")
        if plan["version"] >= 3:
            ids.add(oid + "_citation")
        return ids

    if all(required_ids(row) <= existing for row in ready):
        return []
    if not plan.get("revision_id") or plan["revision_id"] != deck.get("revisionId"):
        raise ValueError("The deck changed since planning (or the revision expired). Create a new plan.")
    width, height = (slide_layout.points(deck["pageSize"][axis]) for axis in ("width", "height"))
    result, seen = [], set()
    occupied_by_slide = {}
    for row in ready:
        slide_id = row["slide_id"]
        figure_index = row.get("figure_index", 1) if plan["version"] == 4 else 1
        identity = (slide_id, figure_index)
        if identity in seen or slide_id not in pages:
            raise ValueError("Plan contains a duplicate or nonexistent target slide.")
        seen.add(identity)
        object_id = image_id(slide_id, figure_index)
        if required_ids(row) <= existing:
            continue
        if required_ids(row) & existing:
            raise ValueError("Only part of a figure/caption pair already exists; resolve it before applying.")
        if slide_id not in occupied_by_slide:
            occupied_by_slide[slide_id] = list(slide_layout.slide_geometry(deck, pages[slide_id]))
        box = row["box"]
        if not isinstance(box, list) or len(box) != 4 or not all(
            type(n) in (int, float) and math.isfinite(n) for n in box
        ):
            raise ValueError("Invalid figure coordinates.")
        x, y, w, h = box
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > width or y+h > height:
            raise ValueError("Figure must fit inside the slide.")
        occupied_boxes = occupied_by_slide[slide_id]
        if any(slide_layout.overlaps(box, occupied) for occupied in occupied_boxes):
            raise ValueError("Figure would overlap an existing slide element.")
        figure = row["figure"]
        validate_image_url(figure["url"])
        result.append({"createImage": {"objectId": object_id, "url": figure["url"], "elementProperties": {
            "pageObjectId": slide_id, "size": {"width": {"magnitude": w, "unit": "PT"},
            "height": {"magnitude": h, "unit": "PT"}}, "transform": {
                "scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "PT"}}}})
        result.append({"updatePageElementAltText": {"objectId": object_id,
            "title": "Figure Finder: " + figure.get("verdict", ""),
            "description": f"Source: {figure.get('source_page') or figure['url']}\n"
                           f"Source title: {figure.get('title', '')}\nCreator: {figure.get('creator', '')}\n"
                           f"Attribution: {figure.get('attribution', '')}\nLicense URL: {figure.get('license_url', '')}\n"
                           f"License metadata: {figure.get('license') or 'not supplied'}\n"
                           f"Why added: {row['decision']['reason']}"}})
        if plan["version"] >= 2:
            caption = row.get("caption")
            if not isinstance(caption, str) or not caption.startswith("Figure ") or len(caption) > 220:
                raise ValueError("Missing or invalid figure caption; create a new plan.")
            caption_box = row.get("caption_box")
            if not isinstance(caption_box, list) or len(caption_box) != 4 or not all(
                type(n) in (int, float) and math.isfinite(n) for n in caption_box
            ):
                raise ValueError("Invalid caption coordinates.")
            cx, cy, cw, ch = caption_box
            if cx < 0 or cy < y+h or cw <= 12 or ch <= 0 or cx+cw > width or cy+ch > height:
                raise ValueError("Caption must fit below the figure inside the slide.")
            if any(slide_layout.overlaps(caption_box, occupied) for occupied in
                   occupied_boxes):
                raise ValueError("Caption would overlap an existing slide element.")
            lines = slide_layout.caption_lines(caption, cw)
            if ch < 12 + len(lines) * slide_layout.CAPTION_FONT_SIZE * 1.4:
                raise ValueError("Caption text does not fit its reserved space.")
            result.extend(slide_text.text_box_requests(object_id + "_caption", slide_id,
                          caption_box, caption, slide_layout.CAPTION_FONT_SIZE))
        if plan["version"] >= 3:
            citation = row.get("citation")
            if citation != citations.make_citation(figure):
                raise ValueError("Citation must match the selected image's source metadata.")
            citation_box = row.get("citation_box")
            if not isinstance(citation_box, list) or len(citation_box) != 4 or not all(
                type(n) in (int, float) and math.isfinite(n) for n in citation_box
            ):
                raise ValueError("Invalid citation coordinates.")
            sx, sy, sw, sh = citation_box
            if sx < 0 or sw <= 12 or sh <= 0 or sx+sw > width or sy+sh > height or sy < cy+ch+4:
                raise ValueError("Citation must fit below the caption inside the slide.")
            if not all(math.isclose(a, b, abs_tol=1e-6) for a, b in ((x, cx), (x, sx), (w, cw), (w, sw))):
                raise ValueError("Image, caption, and citation edges must line up.")
            if any(slide_layout.overlaps(citation_box, occupied) for occupied in
                   occupied_boxes):
                raise ValueError("Citation would overlap an existing slide element.")
            if sh < slide_layout.text_height(citation["text"], sw, slide_layout.CITATION_FONT_SIZE):
                raise ValueError("Citation text does not fit its reserved space.")
            result.extend(slide_text.text_box_requests(object_id + "_citation", slide_id,
                          citation_box, citation["text"], slide_layout.CITATION_FONT_SIZE, citation["url"]))
        occupied_boxes.append(box)
        if plan["version"] >= 2:
            occupied_boxes.append(caption_box)
        if plan["version"] >= 3:
            occupied_boxes.append(citation_box)

    return result


def apply_plan(client, plan):
    requests_list = build_requests(plan, client.get(plan["presentation_id"]))
    if not requests_list:
        return {"inserted": 0, "message": "No pending figures to insert."}
    # One atomic batch, guarded against edits between this read and the write.
    result = client.update(plan["presentation_id"], plan["revision_id"], requests_list)
    return {"inserted": sum("createImage" in request for request in requests_list), "google_response": result}


def main(argv=None):
    cli._load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "plan", "apply"):
        p = commands.add_parser(command)
        p.add_argument("--credentials", default=os.getenv("FF_GOOGLE_CREDENTIALS", ".secrets/google-oauth.json"))
        p.add_argument("--token", default=os.getenv("FF_GOOGLE_TOKEN", ".secrets/google-token.json"))
        if command == "apply":
            p.add_argument("plan_file")
            continue
        p.add_argument("presentation", nargs="?", default=os.getenv("FF_GOOGLE_PRESENTATION", ""),
                       help="Google Slides URL or ID; defaults to FF_GOOGLE_PRESENTATION in .env")
        p.add_argument("--out", default="figurefinder_slides_out")
        p.add_argument("--audience", default="undergraduate students")
        p.add_argument("--language", default="English")
        p.add_argument("--provider", choices=("openai", "anthropic"), default=os.getenv("FF_PROVIDER", config.PROVIDER))
        p.add_argument("--sources", default=os.getenv("FF_SOURCES", "brave,wikimedia"))
        p.add_argument("--queries", type=int, default=3)
        p.add_argument("--max-fetched", type=int, default=16)
        p.add_argument("--max-scored", type=int, default=8)
        p.add_argument("--max-figures", type=int, default=5)
        p.add_argument("--figures-per-slide", type=int,
                       help="Up to this many useful figures on each slide; overrides the deck-wide --max-figures limit.")
    args = parser.parse_args(argv)
    try:
        if args.command in ("run", "plan"):
            if not args.presentation.strip():
                raise ValueError("Set FF_GOOGLE_PRESENTATION in .env or pass a Google Slides URL after run/plan.")
            deck_id = presentation_id(args.presentation)
            if args.max_figures < 1 or args.queries < 1 or args.max_scored < 1 or args.max_fetched < args.max_scored:
                raise ValueError("Limits must be positive and --max-fetched must be at least --max-scored.")
            sources = [s.strip() for s in args.sources.split(",") if s.strip()]
            if not sources or any(s not in search.SOURCE_FUNCS for s in sources):
                raise ValueError("Invalid search sources.")
            keys = [llm.PROVIDER_ENV[args.provider]]
            if "brave" in sources:
                keys.append("BRAVE_API_KEY")
            if "google" in sources:
                keys.extend(["GOOGLE_API_KEY", "GOOGLE_CSE_ID"])
            if cli._require_env(*keys):
                return 2
            provider = llm.get_provider(args.provider)
        else:
            plan = json.loads(Path(args.plan_file).read_text())
            presentation_id(plan["presentation_id"])
        client = SlidesClient(authorize(args.credentials, args.token))
        if args.command == "apply":
            result = apply_plan(client, plan)
            Path(args.plan_file).with_name("applied.json").write_text(json.dumps(result, indent=2))
            print(f"Inserted {result['inserted']} figures.")
            return 0
        plan = create_plan(client, deck_id, provider, args)
        ready = len(ready_figures(plan))
        errors = sum(row["status"] == "error" or any(
            figure["status"] == "error" for figure in row.get("figures", []))
            for row in plan["slides"])
        skipped = sum(row["status"] == "skipped" for row in plan["slides"])
        if args.command == "run":
            result = apply_plan(client, plan)
            (Path(args.out) / "applied.json").write_text(json.dumps(result, indent=2))
            print(f"Inserted {result['inserted']} figures; {skipped} skipped; {errors} slide errors. Logs: {args.out}")
            return 1 if errors else 0
        print(f"{ready} figures ready; {errors} slide errors. Review: {Path(args.out) / 'review.html'}")
        print(f"Apply: .venv/bin/python -m figurefinder.slides apply {Path(args.out) / 'plan.json'}")
        return 1 if errors else 0
    except Exception as exc:
        print(f"Slides agent: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
