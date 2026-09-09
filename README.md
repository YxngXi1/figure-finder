# figure-finder

Give it jot notes for a slide. It finds a good figure for that slide on the web,
grades the candidates against a topic-specific checklist, and hands you a ranked
list with links.

Each suitable result includes a direct image link. The terminal report ends with the
judge's recommended image link, or the top-ranked image link when there is no
judge pick, plus its source page when available.

Candidates must clearly match the subject (at least 4/5), cover the requested
elements (at least 3/5), and score at least 50/100. Book covers, unrelated images,
page captures, and photos returned for diagram requests are excluded. If nothing
passes, the tool reports "No suitable figure found" and exits with status 1.
JSON keeps excluded candidates under `rejected_results` for troubleshooting;
`results` and the HTML contact sheet contain only suitable figures.

Labelled diagrams must have readable labels in **English** by default. Missing,
unverifiable, unreadable, or wrong-language required labels exclude a candidate,
regardless of its other scores. The vision model checks the image itself; an
English filename or source page is not enough. Use `--language` to request a
different language explicitly.

```
$ ./figure-finder "ruminant digestion; four stomach chambers; microbial fermentation of cellulose"

Concept  Ruminant digestive system (four-chambered stomach)
A labelled cutaway showing all four chambers and the path feed takes through them.

Must show
  • all four chambers named individually: rumen, reticulum, omasum, abomasum
  • the path feed takes between chambers, including regurgitation
  • the rumen identified as the site of microbial fermentation
Watch out for
  ✗ photographs of cows rather than anatomical diagrams
  ✗ generic monogastric (single-stomach) diagrams

Searching
    10 results  ·  ruminant four chambered stomach labelled diagram
    10 results  ·  rumen reticulum omasum abomasum anatomy site:.edu
    ...
    kept 31 candidates  (dropped: 8 blocked domain, 11 too small)

Fetching
    19 usable images  (dropped: 5 unusable, 7 duplicates)

Grading 19 candidates (gpt-5.6-terra)

Top 5 of 19 scored

1.  91.4 ██████████████████████·· ★
   Cutaway of a cow's abdomen with all four stomach chambers labelled in
   English, arrows for feed flow, and the rumen marked as the fermentation vat
   1400×1050 · openstax.org
   image https://...
   page  https://...
   subject_match:5  checklist_coverage:5  scientific_accuracy:5  legibility:4 ...
```

---

## Setup

Clone or download this repository and open a terminal in its folder. Python
3.10 or newer is required; development checks use Python 3.13.

```bash
git clone https://github.com/YxngXi1/figure-finder.git
cd figure-finder
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` instead, and
use `python figure-finder` in place of `./figure-finder` in the examples below.

Edit `.env` and replace `[YOUR_OPENAI_API_KEY]` with your own key. The optional
search keys can remain commented out when using `--sources wikimedia,openverse`.
Your `.env`, virtual environment, downloaded images, and generated reports are
excluded from Git. Keep custom output folders outside the repository too.

`.env.example` marks every value you need to replace in `[SQUARE BRACKETS]`.
Replace the whole placeholder, brackets included. The tool checks for leftover
brackets and tells you which line you missed.

**OpenAI key** — <https://platform.openai.com/api-keys>. Starts with `sk-proj-`
and is shown exactly once. The account also needs credit on it
(<https://platform.openai.com/settings/organization/billing>) — a key on a
zero-balance account fails with a confusing 429.

**Google is optional.** Two of the three sources need no keys at all, so you
can run the tool the moment your OpenAI key is in:

```bash
./figure-finder "ruminant digestion; four stomach chambers" --sources wikimedia,openverse
```

### Sources

| source | key needed | quota | good for |
|---|---|---|---|
| `wikimedia` | none | none | Wikimedia Commons. Where a great many good scientific and anatomical diagrams actually live. Lots of SVG, which scales perfectly on a slide. |
| `openverse` | none¹ | throttled anonymously | Aggregates Flickr, museums, science orgs. Best-effort — if it throttles, the run continues without it. |
| `google` | 2 keys | 100/day free | Broadest reach, but see the caveat below. |

¹ Optional: register a free Openverse token and set `OPENVERSE_TOKEN` to lift the
anonymous rate limit.

Default is all three (`wikimedia,openverse,google`); results are merged and
deduplicated across them, so the same figure found in two places is graded once.

### Setting up Google (optional)

Two separate things, both free:

1. **API key** — <https://console.cloud.google.com/apis/credentials>. Create a
   project, then APIs & Services → Library → "Custom Search API" → **Enable**.
2. **Search engine ID** —
   <https://programmablesearchengine.google.com/controlpanel/create>, then turn
   on **Image search**.

> **The "Search the entire web" option is gone.** Google restricts it to engines
> created before **2026-01-20**, and switches it off for everyone on
> **2027-01-01**. A new engine can only search sites you list.

That's less bad than it sounds — for hunting textbook figures a curated list is
arguably *better* than the whole web, since the whole web mostly contributed
stock photos. Paste these into **Sites to search** (wildcards work):

```
*.edu/*                    libretexts.org/*           britannica.com/*
*.ac.uk/*                  openstax.org/*             msdvetmanual.com/*
*.gov/*                    pressbooks.pub/*           merckvetmanual.com/*
*.wikipedia.org/*          khanacademy.org/*          *.extension.org/*
commons.wikimedia.org/*    ncbi.nlm.nih.gov/*         fao.org/*
nature.com/*               frontiersin.org/*          teachmeanatomy.info/*
sciencedirect.com/*        researchgate.net/*
```

What you lose is the long tail — a perfect diagram on some lecturer's personal
site won't be found. That's what `wikimedia` and `openverse` are there to cover.

Free tier is 100 queries/day; one run uses 5.

### Which model grades the images

Set in `config.py` under `MODELS`, or per-run with `FF_SCORER_MODEL`.

| | default | why |
|---|---|---|
| planner | `gpt-5.6-terra` | cheap text-only call |
| scorer | `gpt-5.6-terra` | $2/$12 per Mtok — the cost/judgement sweet spot |
| `--fast` | `gpt-5.6-luna` | $0.20/$1.20 — ~10x cheaper, blunter on accuracy |
| judge | `gpt-5.6-terra` | one small call on the finalists |

`gpt-6-astra` is available if you want it (`FF_SCORER_MODEL=gpt-6-astra`), but
at $10/$50 it's 5x the cost for judgement that's rarely better *at this task* —
grading a figure against an explicit checklist isn't a reasoning-hard problem.

## Use

```bash
./figure-finder "krebs cycle; ATP yield per turn; where it happens in the cell"

./figure-finder --notes-file slide7.txt \
                --audience "second-year vet students" \
                --style "clean modern schematic, minimal colour" \
                --top 8 --html --open

./figure-finder "action potential phases" --dry-run   # plan + queries only; one paid text call
./figure-finder "photosynthesis light reactions" --fast   # cheaper model
```

| flag | what it does |
|---|---|
| `--dry-run` | print the figure brief and search queries, then stop. Costs one cheap text call, no image spend. Use it while you're tuning the rubric. |
| `--audience` | shapes how much detail the figure should carry |
| `--style` | "clean labelled textbook diagram", "flowchart", "photograph", … |
| `--language` | required label language (default English); non-matching or missing required labels exclude a diagram |
| `--fast` | grade with `gpt-5.6-luna` — roughly 10x cheaper, noticeably blunter on scientific accuracy |
| `--provider` | `openai` (default) or `anthropic`. Same rubric, same maths — useful for A/B-ing the two on one topic. |
| `--sources` | `wikimedia,openverse,google` — any comma-separated subset. Drop `google` to run with no search keys at all. |
| `--max-scored` | cap on how many images get a (paid) vision look. Default 20. |
| `--html --open` | writes a contact sheet so you can eyeball the picks side by side. Much the fastest way to tell whether the rubric is working. |
| `--no-judge` | skip the final head-to-head comparison |

Cost per full run is roughly 3–6¢ on `gpt-5.6-terra`: ~20 images at ~1.5k tokens
each (`detail: high`), plus two small text calls. `--fast` brings that under a
cent. Setting `FF_IMAGE_DETAIL=low` is cheaper again but downsamples every image
to 512px, which destroys the label text the legibility score depends on — fine
for smoke tests, useless for real grading.

---

## How it works

```
jot notes
   │
   ├─ 1. PLAN      the model turns the notes into a concept name, a concrete
   │               must-show checklist, a list of wrong-but-plausible results
   │               to watch for, and 5 search queries attacking different angles
   │
   ├─ 2. SEARCH    every enabled source, one call per query per source
   │
   ├─ 3. PREFILTER free, deterministic: min dimensions, aspect ratio, stock-photo
   │               domain blocklist, URL dedupe. Cuts the candidate pool ~60%
   │               before anything costs money
   │
   ├─ 4. FETCH     download, verify it's a real image, drop perceptual duplicates
   │               (same figure re-hosted on five sites), resize to slide scale
   │
   ├─ 5. GRADE     the vision model scores each image on six criteria against
   │               the checklist from step 1, and raises veto flags separately
   │
   ├─ 6. RANK      weighted score → 0-100, veto multipliers applied, small
   │               source-reputation nudge, deterministic resolution term
   │
   └─ 7. JUDGE     head-to-head on the top 4 for the final pick
```

### The four ideas that make it work

**The checklist is written before the search.** Step 1 commits to *"all four
chambers named individually: rumen, reticulum, omasum, abomasum"* before any
image is seen. Step 5 then grades against that concrete list. This is why it can
tell you a figure is missing the omasum instead of vaguely liking it.

**Every score is anchored.** "Rate 0–5, higher is better" produces mush — models
cluster everything at 3 and 4. Each criterion in `rubric.py` spells out what a 0,
a 2, a 4 and a 5 look like. This is the single biggest lever on output quality;
if results feel wrong, edit the anchors first.

**Vetoes are separate from scores.** A watermark isn't "slightly less pretty",
it's disqualifying. If you fold it into an aesthetics score, a gorgeous unusable
image still wins. Flags apply a multiplier instead: a watermarked figure that
scores a perfect 5 on every criterion lands at ~15/100 and sinks out of sight.

**Resolution is measured, not judged.** The model only ever sees a 1024px copy,
so it can't know the original's resolution — asking it to guess produces noise.
The pipeline scores resolution arithmetically from the real dimensions and asks
the model only about legibility *at the size it's looking at*, which is roughly
the size the figure will be on the slide.

---

## Tuning the criteria

This is the part worth your time. `figurefinder/rubric.py` is the whole
judgement layer.

**To change what "good" means** — edit `CRITERIA`. Each entry has a weight and a
set of anchored descriptions. The prompt text is *generated* from this list, so
a weight change and a prompt change are one edit and can't drift apart.

Current weights:

| criterion | weight | |
|---|---|---|
| `subject_match` | 30 | is it this concept, at this scope |
| `checklist_coverage` | 20 | does it show the must-show items |
| `scientific_accuracy` | 15 | is it correct / trustworthy |
| `legibility` | 15 | readable from the back of a room |
| `slide_fit` | 12 | drops on a slide without editing |
| `visual_quality` | 8 | doesn't look like clip-art |
| *resolution* | 8 | computed, not judged |

Some edits worth trying:

- Making decks for a conference talk rather than a lecture? Push `slide_fit` and
  `legibility` up, `checklist_coverage` down — a busy complete figure is worse
  than a clean partial one when nobody can pause the slide.
- Getting figures that are right but ugly? Raise `visual_quality` to ~15.
- Getting pretty but wrong figures? Raise `scientific_accuracy` and tighten its
  anchors with the specific misconceptions your field attracts.

**To add a new dimension**, append a `Criterion` to the list. The prompt, the
JSON schema and the scoring math all pick it up automatically — nothing else to
change.

**To kill a category of bad result**, add a `VETO_FLAGS` entry. Multiplier `0.0`
is a hard kill, `0.3` is "only if nothing else exists". The existing set — stock
watermarks, wrong subject, photo-when-a-diagram-was-asked-for, unreadable
labels, wrong language, page screenshots, AI-generated-looking — covers the
common failure modes; yours will differ.

**To change how it searches**, edit `PLANNER_PROMPT`. The "different angles"
instruction matters more than it looks: five rewordings of one query return the
same twelve images, and you end up grading duplicates.

`figurefinder/config.py` holds the cheap stuff — size floors, the stock-photo
domain blocklist, the `.edu`/OpenStax reputation bonuses, batch sizes.

### A tuning loop that works

```bash
./figure-finder "your topic" --dry-run          # is the checklist right?
./figure-finder "your topic" --html --open      # do the scores match your eye?
```

Look for the disagreements between the ranking and your own judgement, and fix
the anchor that caused each one. Three or four topics through this loop and it
tends to settle.

---

## Known limits

- **Licensing is reported, not enforced.** Wikimedia and Openverse results carry
  a real license string (shown in the output); Google results carry nothing.
  Fine for a lecture, not for anything published. To enforce it, filter on
  `Candidate.license` before scoring.
- **SVG from Wikimedia works without `cairosvg`** — Commons rasterises SVGs for
  us on request, which is what `WIKIMEDIA_RASTER_WIDTH` is for. SVGs from
  *other* sources still need `pip install cairosvg`, and are skipped without it.
- **Google CSE image search is thinner than google.com/images**, and now only
  covers the sites on your engine. Compensate with more query angles and the
  other two sources, not more results per query.
- **Openverse throttles anonymous callers.** If it drops out mid-run you'll see
  `skipped — Openverse rate limit hit` and the run continues on the others.
- The perceptual dedupe uses an 8×8 dhash. It catches the same figure re-hosted
  and rescaled; it won't catch a figure that's been recoloured or cropped.

## Notes on the port

Provider-specific code is confined to `llm.py` — two small classes behind one
`structured(model, system, blocks, schema, name)` method. Everything else (the
rubric, the scoring maths, the filters, the report) is shared.

The schemas in `rubric.py` are written to OpenAI's *strict* structured-output
rules, which are the stricter of the two: every property listed in `required`,
`additionalProperties: false` on every object, and integer `enum`s instead of
`minimum`/`maximum`. Anthropic accepts those same schemas as tool schemas
unchanged. `tests/mock_run.py` validates them — a schema mistake otherwise
surfaces as an opaque HTTP 400.

## Testing

```bash
python -m unittest discover -s tests -p 'test_*.py'
python tests/mock_run.py
```

Runs the whole pipeline against canned API responses and a fake model — checks the
prefilters, duplicate collapsing, veto multipliers, score normalisation, ranking
and both report formats. It also pins the Google, Wikimedia and Openverse
response parsers to canned payloads, checks that a dead source is skipped rather
than fatal, and builds a real OpenAI client to assert the exact request body is
well formed (json_schema + strict, base64 data URL, `max_completion_tokens`,
system role). No API keys, no quota, no cost.
