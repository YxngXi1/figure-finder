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
    10 results  ·  rumen reticulum omasum abomasum anatomy
    ...
    kept 31 candidates  (dropped: 8 blocked domain, 11 too small)

Fetching
    19 usable images  (dropped: 5 unusable, 7 duplicates)

Grading 19 candidates (gpt-4.1-mini)

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

Edit `.env` and replace `[YOUR_OPENAI_API_KEY]` and `[YOUR_BRAVE_API_KEY]`
with your own keys. The default search uses Brave plus Wikimedia. You can use
`--sources wikimedia,openverse` with only an OpenAI key.
Your `.env`, virtual environment, downloaded images, and generated reports are
excluded from Git. Keep custom output folders outside the repository too.

`.env.example` marks every value you need to replace in `[SQUARE BRACKETS]`.
Replace the whole placeholder, brackets included. The tool checks for leftover
brackets and tells you which line you missed.

**OpenAI key** — <https://platform.openai.com/api-keys>. Starts with `sk-proj-`
and is shown exactly once. The account also needs credit on it
(<https://platform.openai.com/settings/organization/billing>) — a key on a
zero-balance account fails with a confusing 429.

**Brave key** — create a key at <https://api-dashboard.search.brave.com/>
with access to Image Search. Brave searches a broad web image index without
a list of allowed domains. You do not need to install the Brave browser.

**Search keys are optional with the narrower open sources.** With only your
OpenAI key, you can run:

```bash
./figure-finder "ruminant digestion; four stomach chambers" --sources wikimedia,openverse
```

### Sources

| source | key needed | quota | good for |
|---|---|---|---|
| `brave` | `BRAVE_API_KEY` | metered requests | Broad web image search, including independent websites; no configured domain allowlist. |
| `wikimedia` | none | none | Wikimedia Commons. Where a great many good scientific and anatomical diagrams actually live. Lots of SVG, which scales perfectly on a slide. |
| `openverse` | none¹ | throttled anonymously | Aggregates Flickr, museums, science orgs. Best-effort — if it throttles, the run continues without it. |
| `google` | 2 keys | 100/day free | Legacy Google Custom Search; scope depends on the configured engine. |

¹ Optional: register a free Openverse token and set `OPENVERSE_TOKEN` to lift the
anonymous rate limit.

Default is `brave,wikimedia`; results are merged and deduplicated across them,
so the same figure found in two places is graded once. Use `--sources brave`
for just broad web search, or set `FF_SOURCES=brave,wikimedia` in `.env`.
An explicit `--sources wikimedia` still searches only Wikimedia.

Brave's [published Search API pricing](https://brave.com/search/api/) is
$5 per 1,000 requests, with $5 in monthly credits (checked September 20, 2026).
Five Brave queries cost about $0.025 before credits, separately from AI usage.
The app requests 20 images per query and grades at most 20 images per run by
default. This searches an index, not every page on the internet. The existing
stock-image blocklist and image-quality filters still apply.

Missing Brave credentials stop the run before the paid planning call; `--dry-run`
only needs the AI key. API errors are reported instead of silently falling back
to narrower search coverage.

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

The planner, scorer, and judge now default to **`gpt-4.1-mini`**. It supports
image input and structured output and runs without a separate reasoning step.
This is a starting recommendation for inexpensive checklist-based grading;
we have not benchmarked its scientific accuracy against the previous default.

| setting | default | purpose |
|---|---|---|
| `FF_PLANNER_MODEL` | `gpt-4.1-mini` | turn notes into a checklist and queries |
| `FF_SCORER_MODEL` | `gpt-4.1-mini` | inspect the candidate images |
| `FF_JUDGE_MODEL` | `gpt-4.1-mini` | compare the finalists |
| `FF_FAST_MODEL` | `gpt-5.6-luna` | existing alternative scorer, only with `--fast` |

Set these in `.env` or as shell environment variables. Shell variables take
precedence over `.env`. You do not need `--fast` to use the new mini default.

For comparison, the official text-token rates per million tokens are:

| model | input | output | tradeoff |
|---|---|---|---|
| [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) | $0.40 | $1.60 | default; no separate reasoning step |
| [GPT-5 mini](https://developers.openai.com/api/docs/models/gpt-5-mini) | $0.25 | $2.00 | reasoning alternative; reasoning tokens can add cost |

Rates checked September 20, 2026. Both accept images; total cost also depends
on image tokenization, output length, caching, and the number of candidates.
Keep high image detail for reading labels. Review representative results before
relying on a cheaper model for scientific accuracy.

## Use

```bash
./figure-finder "krebs cycle; ATP yield per turn; where it happens in the cell"

./figure-finder --notes-file slide7.txt \
                --audience "second-year vet students" \
                --style "clean modern schematic, minimal colour" \
                --top 8 --html --open

./figure-finder "action potential phases" --dry-run   # plan + queries only; one paid text call
./figure-finder "photosynthesis light reactions" --fast   # alternative lightweight scorer
```

| flag | what it does |
|---|---|
| `--dry-run` | print the figure brief and search queries, then stop. Costs one cheap text call, no image spend. Use it while you're tuning the rubric. |
| `--audience` | shapes how much detail the figure should carry |
| `--style` | "clean labelled textbook diagram", "flowchart", "photograph", … |
| `--language` | required label language (default English); non-matching or missing required labels exclude a diagram |
| `--fast` | use `FF_FAST_MODEL` (default `gpt-5.6-luna`) for scoring; the regular default is already GPT-4.1 mini |
| `--provider` | `openai` (default) or `anthropic`. Same rubric, same maths — useful for A/B-ing the two on one topic. |
| `--sources` | any comma-separated subset of `brave,wikimedia,openverse,google`; default `brave,wikimedia`. Use `wikimedia,openverse` without search keys. |
| `--max-scored` | cap on how many images get a (paid) vision look. Default 20. |
| `--html --open` | writes a contact sheet so you can eyeball the picks side by side. Much the fastest way to tell whether the rubric is working. |
| `--pdf` | save the recommended figure as `figure.pdf` in the `--out` folder |
| `--no-judge` | skip the final head-to-head comparison |

AI charges vary with the images and response length; search charges are separate.
Use `--max-scored` to limit image grading and `--no-judge` to skip the final
comparison. Keep the default high image detail to preserve small labels.

### Password-protected web preview

The web version keeps the main workflow simple: describe the teaching need,
review the recommended image, download it, then compare the other suitable
options and source links at the bottom. Advanced search settings and technical
diagnostics are collapsed by default. It runs the CLI in a background process
and provides a clearly named JPEG download up to 1920 pixels on its longest
edge. The separate 1024-pixel grading copy is still used for the model, so the
sharper export does not increase vision cost.

The web interface uses Brave Image Search only. Its faster default path runs
three Brave query angles concurrently, interleaves their results, downloads at
most 24 candidates, grades eight images in up to two concurrent model calls,
and skips the optional final judge call. The form can re-enable that comparison.
Wikimedia, Openverse, and Google remain available only through the diagnostic
CLI because they provide genuinely different coverage, but they no longer
appear in the end-user web form.

Access requires the shared password in the ignored `.env.web` file. Successful
logins receive a signed, HTTP-only, same-site session cookie lasting 12 hours.
Login attempts are throttled and search/logout forms use CSRF tokens. Exact
repeated searches reuse an in-memory completed result while the server remains
running, avoiding duplicate API calls.

Start it from the repository folder:

```bash
./figure-finder-web
```

Then open <http://localhost:8000>. Searches are queued one at a time by default
to limit accidental API spend. Each job is kept under `figurefinder_web_out/`;
the browser process keeps the recent-job index only until it is restarted.

The default bind address is localhost. You can test from another computer with
`./figure-finder-web --host 0.0.0.0`, but use HTTPS before entering the password
over a network. For a durable public deployment it still needs per-user/request
quotas, a spending cap, automated retention cleanup, persistent jobs, and a
production process manager. Set `FF_WEB_SECURE_COOKIES=1` behind HTTPS. API
credentials stay on the server and their values are never shown in the
interface or child-process command.

Useful diagnostic options:

```bash
./figure-finder-web --port 8080
./figure-finder-web --workers 2 --out /path/to/web-jobs
```

### Example: animal cell diagram as a PDF

```bash
./figure-finder "Animal cell structure; labelled diagram showing the nucleus, cell membrane, cytoplasm, and mitochondria; English labels" \
  --sources brave,wikimedia --out examples --pdf --html
```

The output folder is created automatically. The selected figure is saved locally
as `examples/figure.pdf`; `examples/candidates.html` shows the suitable candidates
and `examples/results.json` keeps the scores and source links. If you have no
Brave API key, use `--sources wikimedia` instead. The `examples/` folder is ignored
by Git because it contains downloaded images and generated reports.

The PDF contains one image: the judge's eligible winner, or the highest-ranked
suitable result when there is no valid judge pick. It uses the same downloaded
slide-scale copy that was graded (up to 1024 pixels on its longest edge), preserves
its aspect ratio, and includes the source URL in the PDF metadata. It is not a
vector or full-resolution original. Source links remain in the JSON/HTML reports.
No PDF is created if no image qualifies. Running again in the same output folder
overwrites the PDF on success; use a different `--out` folder to keep each run.

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
| `slide_fit` | 6 | drops on a slide without editing; a light tie-breaker |
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

**To kill or penalise a category of bad result**, add a `VETO_FLAGS` entry.
Multiplier `0.0` is a hard kill; a non-zero multiplier is a ranking penalty.
The existing set covers stock watermarks, wrong subjects, photos when a diagram
was explicitly requested, unreadable or wrong-language labels, cluttered page
captures, and images with visible generative errors. Clean PDF/web extracts,
small attribution marks, and illustration style alone are not problems.

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
  a license string (shown in the output); Brave and Google results carry none.
  Check the source page for reuse terms. To enforce it, filter on
  `Candidate.license` before scoring.
- **SVG from Wikimedia works without `cairosvg`** — Commons rasterises SVGs for
  us on request, which is what `WIKIMEDIA_RASTER_WIDTH` is for. SVGs from
  *other* sources still need `pip install cairosvg`, and are skipped without it.
- **Google CSE image search is thinner than google.com/images**, and now only
  covers the sites on your engine. Use Brave for broad web image retrieval.
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
and both report formats. Tests also pin the Brave, Google, Wikimedia and Openverse
response parsers to canned payloads, checks that a dead source is skipped rather
than fatal, and builds a real OpenAI client to assert the exact request body is
well formed (json_schema + strict, base64 data URL, `max_completion_tokens`,
system role). No API keys, no quota, no cost.
