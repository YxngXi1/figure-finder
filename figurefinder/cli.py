"""figure-finder CLI."""

from __future__ import annotations

import argparse
import os
import sys

from . import config, fetch, llm, report, search, selection


def _load_dotenv(path=".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _require_env(*names: str) -> int:
    """Return 0 if all present and filled in, else print something useful."""
    for n in names:
        v = os.environ.get(n, "")
        if not v:
            print(f"\nMissing {n}.", file=sys.stderr)
            print("  Copy .env.example to .env and fill it in:  cp .env.example .env",
                  file=sys.stderr)
            return 2
        if v.startswith("[") or "PASTE" in v.upper():
            print(f"\n{n} still has the placeholder in it: {v}", file=sys.stderr)
            print("  Replace the whole thing, square brackets included.", file=sys.stderr)
            return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="figure-finder",
        description="Find a good diagram/figure on the web for a slide concept.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  figure-finder "ruminant digestion; 4 chambers; microbial fermentation of cellulose"
  figure-finder --notes-file slide7.txt --audience "2nd year vet students" --top 8
  figure-finder "krebs cycle" --style "clean modern schematic" --html --open
""",
    )
    p.add_argument("notes", nargs="*", help="jot notes for the slide")
    p.add_argument("--notes-file", help="read notes from a file instead")
    p.add_argument("--audience", default="undergraduate students",
                   help="who the slide is for (shapes depth and labelling)")
    p.add_argument("--style", default="clean labelled textbook diagram",
                   help="what the figure should look like")
    p.add_argument("--language", default="English",
                   help="required label language (default: English); missing, unreadable, "
                        "or wrong-language labels exclude a diagram")
    p.add_argument("--top", type=int, default=config.DEFAULT_TOP_N)
    p.add_argument("--queries", type=int, default=config.MAX_QUERIES,
                   help="search queries to run (each = 1 Google CSE quota unit)")
    p.add_argument("--max-scored", type=int, default=config.MAX_SCORED_CANDIDATES,
                   help="cap on images sent to the vision model")
    p.add_argument("--sources", default=",".join(config.SOURCES),
                   help="comma-separated: wikimedia,openverse,google "
                        f"(default: {','.join(config.SOURCES)}). wikimedia and "
                        "openverse need no keys.")
    p.add_argument("--provider", choices=("openai", "anthropic"), default=config.PROVIDER,
                   help=f"which API to grade with (default: {config.PROVIDER})")
    p.add_argument("--fast", action="store_true",
                   help="score with the cheaper model (roughly 10x cheaper, blunter)")
    p.add_argument("--no-judge", action="store_true",
                   help="skip the final head-to-head pick")
    p.add_argument("--dry-run", action="store_true",
                   help="show the plan and queries, then stop (no search, no spend)")
    p.add_argument("--html", action="store_true", help="also write an HTML contact sheet")
    p.add_argument("--open", dest="open_html", action="store_true",
                   help="open the contact sheet when done")
    p.add_argument("--out", default=config.OUTPUT_DIR)
    return p


def main(argv=None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)

    notes = (
        open(args.notes_file).read() if args.notes_file else " ".join(args.notes)
    ).strip()
    if not notes:
        print("Give me some jot notes. e.g.  figure-finder \"ruminant digestion; "
              "four stomach chambers\"", file=sys.stderr)
        return 2

    # Check the key before building the client — the SDKs raise a wall of text.
    if (rc := _require_env(llm.PROVIDER_ENV[args.provider])):
        return rc
    try:
        provider = llm.get_provider(args.provider)
    except llm.ProviderError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2

    models = llm.models_for(provider.name)
    scorer_model = models["fast"] if args.fast else models["scorer"]

    # 1. Plan -----------------------------------------------------------------
    print("Planning…", end="\r")
    plan = llm.make_plan(notes, args.audience, args.style, args.language,
                         n_queries=args.queries, provider=provider)
    report.print_plan(plan)
    queries = [q["q"] for q in plan.get("queries", [])][: args.queries]
    for q in plan.get("queries", [])[: args.queries]:
        print(f"  {report.DIM}· {q['angle']}{report.RESET}")

    if args.dry_run:
        print("\nQueries:")
        for q in queries:
            print(f"  {q}")
        return 0

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in sources if s not in search.SOURCE_FUNCS]
    if unknown:
        print(f"\nUnknown source(s): {', '.join(unknown)}. "
              f"Pick from {', '.join(search.SOURCE_FUNCS)}.", file=sys.stderr)
        return 2
    # Only Google needs keys; the other two are open.
    if "google" in sources:
        if (rc := _require_env("GOOGLE_API_KEY", "GOOGLE_CSE_ID")):
            print("  (or drop Google: --sources wikimedia,openverse)", file=sys.stderr)
            return rc

    # 2. Search ---------------------------------------------------------------
    print()
    try:
        candidates = search.collect_candidates(queries, sources)
    except search.SearchError as e:
        print(f"\n{report.RED}Search failed:{report.RESET} {e}", file=sys.stderr)
        return 1

    if not candidates:
        print("Nothing survived the prefilters. Try --queries 6, add a source, "
              "or loosen MIN_WIDTH/MIN_PIXELS in config.py.")
        return 1

    # 3. Download + dedupe ----------------------------------------------------
    os.makedirs(args.out, exist_ok=True)
    workdir = os.path.join(args.out, "images")
    print(f"\n{report.BOLD}Fetching{report.RESET}")
    usable = fetch.hydrate(candidates, workdir)
    if not usable:
        print("Every candidate failed to download or was too small.")
        return 1

    # Prefer higher-resolution and better-sourced images for the paid look.
    usable.sort(key=lambda c: (search.domain_bonus(c.host), c.pixels), reverse=True)
    usable = usable[: args.max_scored]

    # 4. Score ----------------------------------------------------------------
    print(f"\n{report.BOLD}Grading {len(usable)} candidates{report.RESET}"
          f" {report.DIM}({scorer_model}){report.RESET}")
    results = llm.score_candidates(
        usable, plan, args.audience, args.style, args.language,
        provider=provider, model=scorer_model,
    )

    # 5. Judge ----------------------------------------------------------------
    suitable = selection.suitable_candidates(results)
    pick = None
    if not args.no_judge:
        finalists = suitable[:4]
        if len(finalists) >= 2:
            pick = llm.judge(finalists, plan, provider=provider)

    # 6. Report ---------------------------------------------------------------
    report.print_results(results, plan, args.top, pick)

    json_path = os.path.join(args.out, "results.json")
    report.write_json(json_path, notes, plan, results, pick)
    print(f"{report.DIM}json → {json_path}{report.RESET}")

    if args.html:
        html_path = os.path.join(args.out, "candidates.html")
        report.write_html(html_path, plan, results, max(args.top, 10))
        print(f"{report.DIM}html → {html_path}{report.RESET}")
        if args.open_html:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(html_path))

    return 0 if suitable else 1


if __name__ == "__main__":
    raise SystemExit(main())
