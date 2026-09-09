"""
Offline end-to-end exercise: canned API responses, real everything else.

Verifies the parts that are easy to get silently wrong:
  · the Google / Wikimedia / Openverse response parsers
  · prefilters, and that unknown-dimension results survive them
  · a dead source being skipped rather than killing the run
  · perceptual duplicate collapsing
  · veto multipliers, score normalisation, ranking
  · schemas being valid under OpenAI strict mode
  · the exact request body sent to OpenAI
  · both report formats

No API keys, no quota, no cost.

    python3 tests/mock_run.py
"""

import io
import os
import shutil
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from figurefinder import config, fetch, llm, report, rubric, search  # noqa: E402

OUT = "/tmp/ff_mock"

FAKE_PLAN = {
    "concept": "Ruminant digestive system (four-chambered stomach)",
    "figure_brief": "A labelled cutaway of the ruminant stomach showing all four "
                    "chambers and the path of feed.",
    "must_show": [
        "all four chambers named: rumen, reticulum, omasum, abomasum",
        "the path feed takes between chambers",
        "the site of microbial fermentation",
    ],
    "nice_to_have": ["rumination / cud regurgitation loop"],
    "should_avoid": ["photographs of cows", "generic monogastric stomach diagrams"],
    "wants_diagram": True,
    "queries": [
        {"q": "ruminant four chambered stomach labelled diagram", "angle": "technical term"},
        {"q": "rumen reticulum omasum abomasum anatomy chart", "angle": "names the labels"},
    ],
}


def make_image(w, h, seed, scale=1.0):
    """
    Structurally distinct images, so dhash treats different seeds as different
    figures. Layout uses *relative* coordinates, so the same seed at a different
    scale really is the same picture rescaled — which is what dedupe must catch.
    """
    w, h = int(w * scale), int(h * scale)
    img = Image.new("RGB", (w, h), (240 + seed % 15, 250 - seed % 20, 245))
    d = ImageDraw.Draw(img)
    rng = seed * 7919
    for i in range(6):
        fx, fy = ((rng * (i + 3)) % 70) / 100, ((rng * (i + 5)) % 65) / 100
        fw, fh = 0.10 + ((rng + i * 31) % 18) / 100, 0.12 + ((rng + i * 17) % 20) / 100
        box = [fx * w, fy * h, (fx + fw) * w, (fy + fh) * h]
        if (seed + i) % 2:
            d.ellipse(box, fill=((rng + i * 40) % 256, (i * 53) % 256, 128))
        else:
            d.rectangle(box, fill=((i * 61) % 256, (rng + i * 23) % 256, 90))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# --- fake image corpus -------------------------------------------------------
# mirror.io/good.png is the SAME figure as ex.edu/good.png, rescaled — the
# duplicate detector must collapse them and keep the higher-resolution copy.
IMAGES = {
    "https://ex.edu/good.png":      make_image(1800, 1200, seed=1),
    "https://ex.org/ok.png":        make_image(1000, 750, seed=2),
    "https://ex.net/watermark.png": make_image(1600, 1100, seed=3),
    "https://ex.net/photo.png":     make_image(1500, 1000, seed=4),
    "https://mirror.io/good.png":   make_image(1800, 1200, seed=1, scale=0.62),
    "https://ex.net/tiny.png":      make_image(200, 150, seed=6),
    "https://ex.net/wide.png":      make_image(3000, 300, seed=7),
    "https://ex.net/broken.png":    b"not an image at all",
}

CSE_ITEMS = [
    {"link": u, "title": u.rsplit("/", 1)[-1], "mime": "image/png",
     "image": {"contextLink": u.replace(".png", ".html"),
               "width": 1800, "height": 1200}}
    for u in IMAGES
]
# give the CSE metadata for the deliberately-bad ones its real shape
for it in CSE_ITEMS:
    if "tiny" in it["link"]:
        it["image"].update(width=200, height=150)
    if "wide" in it["link"]:
        it["image"].update(width=3000, height=300)

FAKE_SCORES = {
    "GOOD DIAGRAM": dict(subject_match=5, checklist_coverage=5, scientific_accuracy=5,
                         legibility=5, slide_fit=5, visual_quality=4),
    "OK DIAGRAM": dict(subject_match=4, checklist_coverage=3, scientific_accuracy=4,
                       legibility=3, slide_fit=3, visual_quality=3),
    "WATERMARK": dict(subject_match=5, checklist_coverage=5, scientific_accuracy=5,
                        legibility=5, slide_fit=4, visual_quality=5),
    "PHOTO OF A COW": dict(subject_match=3, checklist_coverage=1, scientific_accuracy=4,
                           legibility=2, slide_fit=3, visual_quality=4),
}


class FakeProvider:
    """Stands in for OpenAIProvider/AnthropicProvider at the same interface."""
    name = "openai"
    env_var = "OPENAI_API_KEY"

    def __init__(self):
        self.calls = []

    def structured(self, model, system, blocks, schema, schema_name, max_tokens):
        self.calls.append((schema_name, model, schema))
        if schema_name == "submit_plan":
            return FAKE_PLAN
        if schema_name == "submit_pick":
            return {"winner_id": "c1", "why": "cleanest.", "runner_up_id": None}

        rows = []
        for blk in blocks:
            if blk["kind"] != "text" or not blk["text"].startswith("\n--- CANDIDATE "):
                continue
            cid = blk["text"].split()[2]
            title = blk["text"].split("page title: ")[-1].split(" ---")[0].rstrip(")")
            key = next((k for k in FAKE_SCORES if k.split()[0].lower() in title.lower()), None)
            scores = FAKE_SCORES.get(key or "", dict(
                subject_match=2, checklist_coverage=2, scientific_accuracy=2,
                legibility=2, slide_fit=2, visual_quality=2))
            flags = {k: False for k in rubric.VETO_FLAGS}
            if "watermark" in title:
                flags["watermarked"] = True
            if "photo" in title:
                flags["photo_when_diagram_needed"] = True
            rows.append({"id": cid, **scores, **flags,
                         "elements_found": ["rumen", "reticulum"],
                         "elements_missing": [] if key == "GOOD DIAGRAM" else ["omasum"],
                         "verdict": f"Mock verdict for {title}",
                         "caveat": None})
        return {"candidates": rows}


def check_strict_schema(schema, path="root", failures=None):
    """
    OpenAI strict structured outputs reject a schema unless every object sets
    additionalProperties:false and lists every property in required. Getting
    this wrong fails at request time with an opaque 400, so check it here.
    """
    failures = failures if failures is not None else []
    if not isinstance(schema, dict):
        return failures
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            failures.append(f"{path}: missing additionalProperties:false")
        props, req = set(schema.get("properties", {})), set(schema.get("required", []))
        if props != req:
            failures.append(f"{path}: required != properties (missing {props - req})")
        for k, v in schema.get("properties", {}).items():
            check_strict_schema(v, f"{path}.{k}", failures)
    if "items" in schema:
        check_strict_schema(schema["items"], f"{path}[]", failures)
    return failures


def fake_download(url):
    return IMAGES.get(url)


def _google_from_items(items):
    """Run the real Google parser over canned CSE JSON."""
    class R:
        status_code, text = 200, "{}"
        @staticmethod
        def json(): return {"items": items}
    with mock.patch.object(search.requests, "get", lambda *a, **k: R()), \
         mock.patch.object(search.config, "env", lambda n, required=True: "x"):
        return search.google_cse("q", 10)


# --- canned API responses, shaped as the real services return them ----------

WIKIMEDIA_JSON = {"query": {"pages": {
    "1": {"title": "File:Ruminant digestive system.svg", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Rumin.svg",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Rumin.svg/1600px-Rumin.svg.png",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Rumin.svg",
        "width": 2400, "height": 1800, "mime": "image/svg+xml",
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}]},
    "2": {"title": "File:Cow.pdf", "imageinfo": [{
        "url": "https://upload.wikimedia.org/x/Cow.pdf",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cow.pdf",
        "width": 1200, "height": 900, "mime": "application/pdf",
        "extmetadata": {}}]},
    "3": {"title": "File:NoInfo.jpg"},
}}}

OPENVERSE_JSON = {"result_count": 2, "results": [
    {"id": "abc", "title": "Ruminant stomach chart",
     "url": "https://live.staticflickr.com/1/abc_b.jpg",
     "foreign_landing_url": "https://www.flickr.com/photos/x/1",
     "width": 1800, "height": 1200, "license": "by-sa",
     "filetype": "jpg", "thumbnail": "https://api.openverse.org/t/abc"},
    # Openverse very often returns null dimensions — must not crash, and must
    # not be discarded before download.
    {"id": "def", "title": "Cud diagram",
     "url": "https://example.org/cud.png",
     "foreign_landing_url": "https://example.org/page",
     "width": None, "height": None, "license": "cc0", "filetype": "png"},
]}


def check_source_parsers():
    """
    Exercise the Wikimedia and Openverse parsers against canned responses.
    These two APIs couldn't be reached from the dev sandbox, so the parsers are
    pinned to the documented response shapes here instead.
    """
    failures = []

    class R:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def json(self): return self._p
        def raise_for_status(self): pass

    with mock.patch.object(search.requests, "get", lambda *a, **k: R(WIKIMEDIA_JSON)):
        wm = search.wikimedia("ruminant", 10)
    if len(wm) != 1:
        failures.append(f"wikimedia: expected 1 image (PDF + missing-info dropped), got {len(wm)}")
    else:
        c = wm[0]
        if "1600px" not in c.download_url:
            failures.append("wikimedia: SVG not routed to the rasterised thumb")
        if c.image_url.endswith(".png"):
            failures.append("wikimedia: canonical URL should stay the original file")
        if (c.width, c.height) != (2400, 1800):
            failures.append("wikimedia: lost the ORIGINAL dimensions (resolution score needs them)")
        if c.license != "CC BY-SA 4.0":
            failures.append(f"wikimedia: license not parsed ({c.license!r})")
        if c.title != "Ruminant digestive system":
            failures.append(f"wikimedia: title not cleaned ({c.title!r})")

    with mock.patch.object(search.requests, "get", lambda *a, **k: R(OPENVERSE_JSON)), \
         mock.patch.object(search.config, "env", lambda n, required=True: ""):
        ov = search.openverse("ruminant", 10)
    if len(ov) != 2:
        failures.append(f"openverse: expected 2 results, got {len(ov)}")
    else:
        if ov[0].host != "flickr.com":
            failures.append(f"openverse: host not derived from landing url ({ov[0].host!r})")
        if (ov[1].width, ov[1].height) != (0, 0):
            failures.append("openverse: null dimensions not normalised to 0")
        if ov[1].mime != "image/png":
            failures.append(f"openverse: filetype not mapped to mime ({ov[1].mime!r})")

    # A null-dimension candidate must survive the prefilter and be judged after
    # download, not thrown away for having no metadata.
    with mock.patch.dict(search.SOURCE_FUNCS, {"openverse": lambda q, n: ov}):
        kept = search.collect_candidates(["q"], ["openverse"], verbose=False)
    if len(kept) != 2:
        failures.append(f"prefilter discarded unknown-dimension candidates ({len(kept)}/2 kept)")

    # A source that is down must be skipped, not fatal.
    def boom(q, n):
        raise search.SourceUnavailable("openverse is throttled")
    with mock.patch.dict(search.SOURCE_FUNCS, {"openverse": boom, "wikimedia": lambda q, n: wm}):
        kept = search.collect_candidates(["q"], ["openverse", "wikimedia"], verbose=False)
    if len(kept) != 1:
        failures.append("a failing source took down the whole run")

    return failures


def check_openai_payload():
    """
    Build a real OpenAIProvider and intercept the HTTP call, so the exact
    request body we'd send to OpenAI gets inspected without a key or a network
    round trip. Catches shape mistakes that would otherwise surface as a 400.
    """
    failures = []
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
    try:
        provider = llm.OpenAIProvider()
    except llm.ProviderError as e:
        return [f"could not build OpenAIProvider: {e}"]

    seen = {}

    class Msg:
        content = '{"winner_id":"c1","why":"ok","runner_up_id":null}'
        refusal = None

    class Choice:
        message, finish_reason = Msg(), "stop"

    class Resp:
        choices = [Choice()]

    def spy(**kw):
        seen.update(kw)
        return Resp()

    provider._client.chat.completions.create = spy
    provider.structured(
        model="gpt-5.6-terra", system="sys",
        blocks=[llm.text_block("hello"), llm.image_block("/tmp/ff_mock/images/c1.jpg")],
        schema=rubric.PICK_SCHEMA, schema_name="submit_pick", max_tokens=100,
    )

    rf = seen.get("response_format", {})
    if rf.get("type") != "json_schema":
        failures.append("response_format is not json_schema")
    if not rf.get("json_schema", {}).get("strict"):
        failures.append("strict mode not set on the json_schema")
    if "max_completion_tokens" not in seen:
        failures.append("token cap not sent as max_completion_tokens")

    content = seen.get("messages", [{}, {}])[1].get("content", [])
    kinds = [b.get("type") for b in content]
    if kinds != ["text", "image_url"]:
        failures.append(f"user content blocks wrong: {kinds}")
    url = content[1]["image_url"]["url"] if len(content) > 1 else ""
    if not url.startswith("data:image/jpeg;base64,"):
        failures.append("image not sent as a base64 data URL")
    if content[1]["image_url"].get("detail") != config.OPENAI_IMAGE_DETAIL:
        failures.append("image detail level not applied")
    if seen.get("messages", [{}])[0].get("role") != "system":
        failures.append("system prompt missing")
    return failures


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    failures = []

    failures += check_source_parsers()

    with mock.patch.dict(search.SOURCE_FUNCS, {"google": lambda q, n: _google_from_items(CSE_ITEMS)}), \
         mock.patch.object(fetch, "_download", fake_download):

        cands = search.collect_candidates(["mock query"], ["google"])
        got = {c.title for c in cands}
        if "tiny.png" in got:
            failures.append("prefilter did not drop the undersized image")
        if "wide.png" in got:
            failures.append("prefilter did not drop the extreme aspect ratio")

        usable = fetch.hydrate(cands, os.path.join(OUT, "images"))
        titles = [c.title for c in usable]
        if "broken.png" in titles:
            failures.append("hydrate accepted a non-image")
        if len(usable) != 4:
            failures.append(
                f"expected 4 survivors after dedupe, got {len(usable)}: {titles}")
        kept_good = [c for c in usable if c.title == "good.png"]
        if not kept_good:
            failures.append("dedupe kept the mirror copy instead of the original")
        elif kept_good[0].real_width != 1800:
            failures.append("dedupe kept the lower-resolution copy of a duplicate")

    for name, schema in (("PLAN_SCHEMA", rubric.PLAN_SCHEMA),
                         ("score schema", rubric.build_score_schema()),
                         ("PICK_SCHEMA", rubric.PICK_SCHEMA)):
        for problem in check_strict_schema(schema, name):
            failures.append(f"OpenAI strict mode would reject {problem}")

    provider = FakeProvider()
    plan = llm.make_plan("ruminant digestion", "students", "diagram", "English",
                         provider=provider)
    results = llm.score_candidates(usable, plan, "students", "diagram", "English",
                                   provider=provider)
    pick = llm.judge(results[:3], plan, provider=provider)
    if not pick or "winner_id" not in pick:
        failures.append("judge returned nothing")
    if not any(c[0] == "submit_scores" for c in provider.calls):
        failures.append("scorer never called through the provider interface")

    by_title = {c.title: c for c in results}
    good, wm = by_title.get("good.png"), by_title.get("watermark.png")
    if not good or not wm:
        failures.append("scoring lost candidates")
    else:
        if wm.final_score >= good.final_score:
            failures.append(
                f"watermark veto not applied (wm={wm.final_score} good={good.final_score})")
        if results[0].title != "good.png":
            failures.append(f"ranking wrong, top is {results[0].title}")
        if not (0 <= good.final_score <= 100):
            failures.append("score out of 0-100 range")

    photo = by_title.get("photo.png")
    if photo and photo.final_score > 45:
        failures.append(f"photo veto too weak ({photo.final_score})")

    failures += check_openai_payload()

    report.print_results(results, plan, 5, {"winner_id": results[0].id, "why": "mock"})
    report.write_json(os.path.join(OUT, "results.json"), "notes", plan, results, None)
    report.write_html(os.path.join(OUT, "candidates.html"), plan, results, 10)
    for f in ("results.json", "candidates.html"):
        if os.path.getsize(os.path.join(OUT, f)) < 200:
            failures.append(f"{f} looks empty")

    print("\n" + "=" * 60)
    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print(f"PASS — {len(results)} scored, ranking + vetoes + reports all behaved.")
    print("score spread: " + ", ".join(f"{c.title}={c.final_score}" for c in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
