"""One isolated Slides run. Input is a private stdin pipe, output is status JSON."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def emit(kind, **values):
    print(json.dumps(dict(type=kind, **values)), file=sys.__stdout__, flush=True)


def main():
    # Never use the owner's desktop OAuth token or local .env in a hosted job.
    os.environ["FF_HOSTED_WORKER"] = "1"
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials
    from . import candidate_debug, llm, slides
    from .google_slides import SlidesClient

    payload = json.load(sys.stdin)
    client = SlidesClient(AuthorizedSession(Credentials(token=payload.pop("token"))))
    args = SimpleNamespace(out=str(Path.cwd() / "output"), audience="students",
        language="English", provider="openai", sources="brave,wikimedia", queries=2,
        max_fetched=8, max_scored=4, figures_per_slide=payload["figures_per_slide"], max_slides=payload["max_slides"])
    try:
        with open(os.devnull, "w") as sink, redirect_stdout(sink):
            plan = slides.create_plan(client, payload["deck_id"], llm.get_provider("openai"), args,
                progress=lambda current, total: emit("progress", message=f"Reviewing slide {current} of {total}…"))
            emit("progress", message="Adding aligned figures, captions and citations…")
            result = slides.apply_plan(client, plan)
        rows = []
        for row in plan["slides"]:
            figures = []
            for attempt in row.get("figures", []):
                item = dict(index=attempt["figure_index"],
                    status="added" if attempt["status"] == "ready" else attempt["status"],
                    reason=("Figure search or slide analysis failed; try this slide again."
                            if attempt["status"] == "error" else
                            (attempt.get("reason") or attempt.get("decision", {}).get("reason", "")))[:500],
                    caption=attempt.get("caption", ""))
                if attempt.get("candidate_debug"):
                    item["candidate_debug"] = candidate_debug.with_previews(
                        attempt["candidate_debug"], Path(args.out) / attempt["search_directory"])
                figures.append(item)
            added = [f for f in figures if f["status"] == "added"]
            rows.append(dict(number=row["number"], status="added" if row["status"] == "ready" else row["status"],
                reason=("Figure search or slide analysis failed; try this slide again." if row["status"] == "error"
                        else (row.get("reason") or row.get("decision", {}).get("reason", ""))[:500]),
                caption=" | ".join(f["caption"] for f in added) if figures else row.get("caption", ""),
                figures=figures, inserted=len(added),
                has_errors=row["status"] == "error" or any(f["status"] == "error" for f in figures)))
            if row.get("candidate_debug"):
                rows[-1]["candidate_debug"] = candidate_debug.with_previews(
                    row["candidate_debug"], Path(args.out) / f"slide-{row['number']}")
        emit("done", result=dict(inserted=result["inserted"], slides=rows,
             errors=sum(r["has_errors"] for r in rows), skipped=sum(r["status"] == "skipped" for r in rows)))
        return 0
    except slides.DeckTooLarge:
        emit("error", message=f"This free beta accepts up to {args.max_slides} slides per presentation. Make a smaller copy and try again.")
    except Exception:
        emit("error", message="The run could not finish. Check edit access and whether the deck changed during the run, then try again. If it continues, contact the owner.")
    finally:
        client.session.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
