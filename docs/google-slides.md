# Private Google Slides agent

This local command-line workflow reads an existing Google Slides deck, asks the
AI which text blocks would benefit from a photo or figure, searches and grades
images with the existing Figure Finder pipeline, and inserts the chosen images.
There is no public hosting, marketplace installation, or multi-user service.

Each newly selected figure receives a visible caption beneath it using:
**Figure #: Title, basic context and image content**. For example:
`Figure 1: Heart chambers, a cutaway showing the chambers involved in circulation.`
The caption model sees the selected image and relevant slide text. Captions are
short, use the requested language (with the literal `Figure N:` prefix), and are
numbered in selection order, starting after the highest existing `Figure N:`
caption found in the deck's editable text. Skipped slides consume no number.
New captions follow existing numbers; existing figures/captions are not renumbered.

Caption generation uses one extra AI call per selected image. Image and caption
are inserted together in the same batch; space is reserved for both. A slide is
skipped if they cannot fit readably. Captions also appear in `plan.json` and the
optional HTML review. To change caption wording later, edit `CAPTION_SYSTEM` in
`figurefinder/captions.py`. Previously saved version-1 plans retain their original
image-only behavior; create a fresh plan/run to include captions.

Invalid or overlong caption responses are automatically rewritten, with up to
three attempts in total. Caption and citation wrapping uses measured font widths
with padding, rather than assuming every character occupies a full font-size
square. Terminal output reports each slide's ready/skipped/error reason.
If a valid caption still leaves too little room for the image, the agent requests
a compact caption (20-character title, 70-character description) and retries
placement. This preserves complete wording instead of cutting a sentence off.

Each new figure also gets a visible, clickable source citation beneath the
caption. It uses retrieved creator/custom-attribution metadata, image title,
source website, and license when available. The source page is the link target;
the direct image URL is the fallback. Missing authors, dates, and publication
details are never invented. This is a compact image-source credit, not an
automatically researched bibliography. Full metadata and license URLs stay in
the plan and image alt text. Long titles are shortened only in the visible credit.

The image, caption, and citation share the same left and right edges. Images
retain their aspect ratio, with an 8-point gap before the caption and a 4-point
gap before the citation. The block aligns with the top of its related text when
space permits. The layout preserves existing slide content and checks every
new element for overlap and text fit. Slides without enough room are skipped.
Caption text is 12-point Arial; linked citations are 10-point Arial. Both use
white text-box backgrounds for contrast. New plans use version 3; version-2
plans retain their original image-and-caption behavior.

## Run without a preview pause

In the web beta, open **Your runs → Slide-by-slide results → Debug: image
candidates** under each figure after a new run. Each searched figure shows the selected image and
alternatives with local thumbnails (when available), source links, scores,
quality flags, missing elements, and recorded rejection or run-limit reasons.
The final AI comparison is displayed separately; it is not a per-candidate
explanation for images the judge never compared. This adds no AI calls.
Candidates discarded by the initial search filters are not included.
The panel shows up to 60 candidate records and 12 small previews per slide.
Debug data shares the run's session ownership and six-hour retention.
Older runs do not gain debug data retroactively.

The web setting **Maximum figures per slide** is a limit for each individual
slide, with no separate deck-wide figure cap. The default is 1. The AI can choose
zero, one or several distinct figures up to that limit; title, transition and
other slides that do not need visuals are skipped. Existing figures from this
agent cause that slide to be skipped on rerun. Each new image has its own caption,
citation and debug comparison. Placement reserves room for additional figures
when possible and checks every image/text box against all other new and existing
elements. Available space may reduce the count. The existing runtime and deck-size
limits still apply.

The website's upper dropdown limit is configured with
`FF_BETA_MAX_FIGURES_PER_SLIDE` (default 3; the old `FF_BETA_MAX_FIGURES` is accepted
as a fallback). CLI users can opt into the same behavior with
`--figures-per-slide 2`, which overrides the legacy `--max-figures` total cap.
New per-slide plans use version 4. Versions 1–3 remain supported.

For CLI runs, the same explanations are saved in `plan.json`; full per-figure
search records are in `slide-N-figure-M/results.json` in per-slide mode, or
`slide-N/results.json` in legacy total-cap mode.

Once the credentials file and `FF_GOOGLE_PRESENTATION` in `.env` are set, run:

```sh
.venv/bin/python -m figurefinder.slides run \
  --sources brave,wikimedia \
  --out figurefinder_slides_out_run1
```

Complete Google sign-in using the printed URL on the first run. The command
then reviews the deck, searches for figures, and inserts them automatically.
It modifies the specified deck without a manual preview step. The default cap
is five figures; use `--max-figures 1` for a smaller test. Logs and the plan are
still saved for troubleshooting. Use a new output folder for each run. The
separate `plan` and `apply` commands below remain available for manual review.

## What you need to provide

1. A Google Slides URL for a test deck you can edit. Use a copy for the first run.
2. A Google Cloud project with **Google Slides API** enabled, a configured OAuth
   consent screen, and a **Desktop app** OAuth client. Download its JSON file to
   `.secrets/google-oauth.json`. This is separate from a Google image-search key.
3. Sign in as the deck's editor using the authorization URL printed on first run.
   Tokens stay in `.secrets/google-token.json`, which is excluded from Git.
4. Your existing AI and image-search credentials in `.env`. The defaults use
   `OPENAI_API_KEY` and `BRAVE_API_KEY`; `--sources wikimedia,openverse` avoids
   needing a search key. Google credentials are not needed for these sources.

Google's [Python quickstart](https://developers.google.com/workspace/slides/api/quickstart/python)
walks through API setup and creating a desktop OAuth client. For a personal Gmail
account, configure the OAuth audience as External, leave it in Testing, and add
your email as a test user. Workspace users can use Internal when their organization
allows it. A school/work account may require its administrator's authorization.
The app requests `https://www.googleapis.com/auth/presentations` to read and edit
Slides. This Google scope covers your presentations, although this program only
operates on the deck you specify. It does not request Drive access or change sharing.

Keep credentials on your machine; do not paste secrets into chat. OAuth testing
tokens may require reauthorization; if refresh fails, use a new `--token` path.

## First-test walkthrough

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create
   a project. Open **APIs & Services → Library**, find **Google Slides API**, and
   click **Enable**.
2. Open **Google Auth platform → Branding** and complete the app setup (for
   example, name it `Figure Finder Local`). For personal Gmail, choose **External**,
   keep the app in **Testing**, and add your sign-in email under **Audience → Test
   users**. External describes which Google accounts can sign in; you do not need
   to publish this app. In **Data Access → Add or remove scopes**, add
   `https://www.googleapis.com/auth/presentations` and save. Workspace users can
   choose Internal if their organization supports it.
3. Under **Google Auth platform → Clients → Create client**, choose **Desktop app**,
   create it, and download its JSON. Rename the downloaded file to
   `google-oauth.json` and put it in the project's `.secrets` folder. That folder
   has already been created. On macOS, **Command+Shift+G** in Finder lets you open
   `/Users/yang/Downloads/figure-finder/.secrets` directly.
4. In Google Slides, make a small test deck with two or three slides. On one,
   put a title and a short explanation of a visual topic (for example, the four
   chambers of the heart) on the left, leaving the right half empty. Resize the
   text box itself so it occupies only the left half. A full-width text box counts
   as occupied even if its right side contains no text. Include a title/agenda
   slide to see whether the AI elects to skip it.
5. At the bottom of the project's `.env`, fill in this section:

   ```dotenv
   FF_GOOGLE_CREDENTIALS=.secrets/google-oauth.json
   FF_GOOGLE_TOKEN=.secrets/google-token.json
   FF_GOOGLE_PRESENTATION=https://docs.google.com/presentation/d/YOUR_DECK_ID/edit
   ```

   Leave the first two paths unchanged if you use the suggested location. Paste
   the test deck's URL on the third line. Keep the downloaded JSON as a separate
   file; `FF_GOOGLE_CREDENTIALS` is its path. The token file is generated by sign-in,
   so you do not create it or enter a token yourself. Existing API keys are reused.
6. In Terminal, run the following (the dependencies are already installed on
   this machine):

   ```sh
   cd /Users/yang/Downloads/figure-finder
   .venv/bin/python -m figurefinder.slides plan \
     --sources brave,wikimedia \
     --max-figures 1 \
     --out figurefinder_slides_out_test1
   ```

   Open the printed authorization URL on this computer, sign in as the test
   deck's editor, and grant access. Finish within three minutes; rerun if it
   times out. This step sends slide content to the AI and makes paid AI/search
   calls, but leaves the presentation unchanged.
7. Open `figurefinder_slides_out_test1/review.html` in your browser. Inspect the
   target text, reason, selected figure, and status. `ready` means an insertion is
   planned. `skipped` includes an explanation; `error` indicates a failed step.
   The maximum of one figure is a spending control for this test, not a promise
   that the AI will select a slide.
8. If the plan looks right, run:

   ```sh
   .venv/bin/python -m figurefinder.slides apply figurefinder_slides_out_test1/plan.json
   ```

   Refresh Google Slides and check the figure's relevance, position, and readability.
   Avoid editing the deck between planning and applying. Running the same apply
   command again should report zero new figures. For another test, create a new
   plan in a new folder such as `figurefinder_slides_out_test2`.

If sign-in says access is denied, confirm your email is listed as a test user.
If Google returns HTTP 403, check that the Slides API is enabled in the OAuth
client's project and that your signed-in account can edit the deck. If the output
says no blank area was available, shrink/move the text box and generate a new
plan. If Google cannot fetch a selected image during apply, the batch is rejected;
you can mark that entry `skipped` in `plan.json`, or generate a new plan.

Google references: [desktop OAuth setup](https://developers.google.com/workspace/slides/api/quickstart/python),
[consent screen and test users](https://developers.google.com/workspace/guides/configure-oauth-consent).

## Install and run

```sh
.venv/bin/pip install -r requirements-slides.txt

.venv/bin/python -m figurefinder.slides plan \
  --audience 'undergraduate students' \
  --max-figures 5 \
  --out figurefinder_slides_out
```

The deck defaults to `FF_GOOGLE_PRESENTATION` in `.env`. You can also pass an
explicit URL or ID after `plan` to override it for one run.

The command prints a Google authorization URL when necessary. Open it in your
browser on the same computer and sign in. Planning makes paid AI/search calls
but does not edit the deck. Each selected slide gets up to 3 search queries,
16 downloads, and 8 vision-scored candidates by default; flags adjust those
limits. `--max-figures` limits selected figures, not slide-review calls.

Open `figurefinder_slides_out/review.html` to inspect the slide screenshot,
target text, selection reason, image, source/license metadata, and insertion
coordinates. The preview shows the original slide and candidate separately.
The machine-readable plan is `plan.json`; per-slide search reports and logs
are saved alongside it. The plan is saved after every slide, including errors.
An error on one slide does not stop other slides; a nonzero exit status signals
that some slides had errors. To exclude a planned addition, change its `status`
from `ready` to `skipped` in `plan.json`.

Then insert the ready figures with:

```sh
.venv/bin/python -m figurefinder.slides apply figurefinder_slides_out/plan.json
```

This modifies the specified deck. It adds at most one image per slide, keeps
existing text and objects, and records source, license metadata, and rationale
in the image's alt text. There is no automatic layout rewrite. Crowded slides
are reported and skipped. Stable image IDs prevent duplicate additions when
applying the same plan twice or planning a deck already processed by the agent.

Use the same Google account/token for plan and apply. Google revision IDs are
short-lived (guaranteed for 24 hours); if the deck changes or the revision expires,
generate a fresh plan with a new output directory. All additions are submitted
in one atomic batch guarded by that revision, so an unavailable image or a
concurrent edit fails the batch instead of leaving half a deck changed.

## CHANGE THIS LATER: figure-need criteria

**`figurefinder/slide_policy.py` → `FIGURE_NEED_POLICY`** is the explicitly
replaceable policy for deciding **which text/context deserves a figure**.
It currently delegates that judgment to the AI using:

- The current slide's editable text blocks, with IDs (including groups and tables).
- A rendered slide screenshot, so existing visuals are considered.
- The previous and next slides' text, plus audience and language.

Replace that clearly marked prompt when the real criteria arrive and bump
`POLICY_VERSION`. The policy and its version are recorded in each plan. The
structured response includes the selected text block IDs, explanation, search
brief, and visual style. IDs are checked against the actual slide before search.

The existing **`figurefinder/rubric.py`** continues to decide **which candidate
image is suitable**. Placement is a separate constraint in `slide_layout.py`:
the figure must fit a sufficiently large blank area without overlapping existing
element bounds. A slide can therefore need a figure but still be skipped for layout.

## Current limits

- This first version is a local CLI; it is not connected to the web app.
- Text, screenshots, and adjacent-slide context are sent to the configured AI
  provider; generated topic queries go to the configured search providers.
  Plans and screenshots contain deck content and stay in the local output folder.
- Editable text is extracted; speaker notes and OCR-only text are not targets.
  Rotated/grouped elements use conservative bounding boxes; inherited layout and
  master decorations can cause a slide to be skipped. Background artwork is
  considered by the vision model but cannot be collision-checked as an object.
- The Slides API imports images from publicly fetchable URLs. This version uses
  the selected source's raster URL and checks its bytes for a supported format;
  it does not upload or publicly share the deck. Some image hosts still block
  Google's fetch, and source content may change between preview and insertion.
  An image-hosting/upload fallback is not implemented.
- Source/license metadata is retained, not independently verified. No image is
  generated by AI; photos and diagrams come from the existing search pipeline.

API references: [image insertion](https://developers.google.com/workspace/slides/api/guides/add-image),
[batch updates and revision checks](https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/batchUpdate),
[OAuth testing](https://developers.google.com/identity/protocols/oauth2#expiration).
