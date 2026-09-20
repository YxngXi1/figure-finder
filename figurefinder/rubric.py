"""
THE RUBRIC — this is the file you tune.

READING MAP — the three AI stages:

  PART 1: PLAN THE SEARCH
    PLANNER_SYSTEM sets the AI's role.
    PLANNER_PROMPT receives your notes and asks for a checklist and queries.
    PLAN_SCHEMA defines the fields the AI must return.

  PART 2: GRADE THE IMAGES
    CRITERIA defines what earns each score; VETO_FLAGS defines problems.
    SCORER_SYSTEM sets the grader's role; SCORER_PROMPT gives its instructions.
    render_scorer_prompt inserts Part 1's checklist and the grading rules.
    build_score_schema defines the scores, flags, and explanations to return.

  PART 3: PICK THE WINNER
    JUDGE_PROMPT asks the AI to compare the finalists and explain its choice.
    PICK_SCHEMA defines the winner, runner-up, and explanation fields.
    This stage reuses SCORER_SYSTEM as its role instruction.

This file defines instructions and response formats. llm.py fills the planner
and judge templates, attaches images for grading/judging, and calls the AI.
cli.py runs the stages; search.py and fetch.py find and prepare the images
between Parts 1 and 2. Numeric weights and penalties are applied by Python in
llm.compute_final_score, not sent to the AI as part of the grading prompt.

Design notes, because they matter more than the code:

  * Scores are anchored. Each criterion spells out examples for specific
    scores, so the model has concrete guidance beyond "higher is better".

  * Problem flags are separate from scores. Some zero the score; others apply
    penalties. selection.py also excludes unsuitable candidates from the picks.

  * The planner writes a *topic-specific* checklist (must_show) before anything
    is searched. The scorer then grades against that concrete checklist rather
    than the vague notion of "a good diagram". This is what makes it notice
    that a ruminant diagram is missing the omasum.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Criterion:
    # PART 2: One grading dimension. Anchors explain scores to the AI;
    # weight controls how Python combines that score with the other dimensions.
    key: str
    weight: int          # contribution to the final 0-100 score
    max_score: int       # scale the model grades on
    label: str
    anchors: Dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# PART 2 — GRADING RULES: subject, coverage, accuracy, legibility, fit, appearance.
# The graded dimensions. Weights are relative — they're normalised against each
# other plus config.RESOLUTION_WEIGHT, so you can change one without rebalancing
# the rest. They happen to sum to 100 here purely for readability.
# ---------------------------------------------------------------------------

CRITERIA: List[Criterion] = [
    Criterion(
        key="subject_match",
        weight=30,
        max_score=5,
        label="Subject match",
        anchors={
            0: "not this concept at all, or a decorative/stock image of the general topic",
            2: "adjacent concept, or the right organism but the wrong process/system",
            4: "clearly this concept, with some scope drift (covers much more or much less)",
            5: "precisely the concept described, at the scope asked for",
        },
    ),
    Criterion(
        key="checklist_coverage",
        weight=20,
        max_score=5,
        label="Checklist coverage",
        anchors={
            0: "shows none of the required elements",
            2: "shows some required elements but omits ones central to the point",
            4: "shows nearly all required elements",
            5: "shows every required element, labelled and identifiable",
        },
    ),
    Criterion(
        key="scientific_accuracy",
        weight=15,
        max_score=5,
        label="Accuracy / trustworthiness",
        anchors={
            0: "visibly wrong, mislabelled, or a known misconception",
            2: "oversimplified in a way that would teach something false",
            4: "correct, with minor simplification appropriate to teaching",
            5: "correct and appropriately precise; looks like it came from a real reference",
        },
    ),
    Criterion(
        key="legibility",
        weight=15,
        max_score=5,
        label="Legibility at slide size",
        anchors={
            0: "text unreadable, image blurry, JPEG-mangled, or a photo of a page",
            2: "readable only if the audience squints; thin strokes, tiny labels",
            4: "labels readable from the back of a room with minor strain",
            5: "large clean type, strong contrast, crisp linework",
        },
    ),
    Criterion(
        key="slide_fit",
        # Slide readiness is useful, but it should not make a simplified image
        # outrank a more informative textbook figure on presentation alone.
        weight=6,
        max_score=5,
        label="Slide fit",
        anchors={
            0: "cluttered, cropped, embedded in surrounding page text, or heavily branded",
            2: "usable only after cropping or cleanup",
            4: "clean background, self-contained, sensible shape for a slide",
            5: "drops straight onto a slide with no editing at all",
        },
    ),
    Criterion(
        key="visual_quality",
        weight=8,
        max_score=5,
        label="Visual quality",
        anchors={
            0: "clip-art, meme, amateur hand drawing, garish colours",
            2: "dated or ugly but functional",
            4: "clean modern illustration",
            5: "publication-grade; would not look out of place in a textbook",
        },
    ),
]

# PART 2 — PROBLEM FLAGS: the AI reports true/false for each problem below.
# Flags the model raises independently of the scores. Multiplier is applied to
# the final score. 0.0 = hard kill.
VETO_FLAGS: Dict[str, Dict] = {
    "book_cover_or_title_page": {
        "multiplier": 0.0,
        "prompt": "it is a book cover, title page, table of contents, or publication "
                  "advertisement rather than the requested figure, even if its title matches",
    },
    "watermarked": {
        "multiplier": 0.15,
        "prompt": "an intrusive stock-photo watermark, repeated/tiling watermark, or "
                  "'SAMPLE'/'PREVIEW' overlay obscures the usable figure. Do not flag a "
                  "small institutional logo, ordinary source credit, copyright line, or "
                  "publisher attribution",
    },
    "wrong_subject": {
        "multiplier": 0.0,
        "prompt": "the image is simply not about the requested concept",
    },
    "photo_when_diagram_needed": {
        "multiplier": 0.45,
        "prompt": "it is a photograph or micrograph where an explanatory diagram was asked for",
    },
    "unreadable_text": {
        "multiplier": 0.0,
        "prompt": "labels exist but are too small or degraded to read at this size",
    },
    "foreign_language_labels": {
        "multiplier": 0.0,
        "prompt": "required labels are not available in the requested language "
                  "on the image itself; translations in filenames or page text do not count",
    },
    "missing_required_labels": {
        "multiplier": 0.0,
        "prompt": "a labelled diagram is requested but one or more required structures "
                  "lack visible readable labels in the requested language; numbers alone "
                  "without an on-image labelled key do not count. Also raise this if "
                  "you cannot verify the required labels or their language",
    },
    "screenshot_or_page_capture": {
        "multiplier": 0.50,
        "prompt": "visible browser/PDF/slide controls, surrounding page text, or other "
                  "interface clutter materially reduces usability. Do not flag a cleanly "
                  "cropped or extracted figure merely because it came from a webpage, "
                  "slide, or PDF",
    },
    "ai_generated_looking": {
        "multiplier": 0.25,
        "prompt": "the image itself contains clear generative failures such as garbled or "
                  "melted labels, duplicated structures, or impossible anatomy. Do not "
                  "infer AI generation from illustration style, age, unusual colours, or "
                  "low resolution alone",
    },
}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _criteria_block() -> str:
    # PART 2: Turn CRITERIA's names, score ranges, and anchors into prompt text.
    out = []
    for c in CRITERIA:
        lines = [f"- **{c.key}** ({c.label}) — integer 0-{c.max_score}"]
        for score in sorted(c.anchors):
            lines.append(f"    {score} = {c.anchors[score]}")
        out.append("\n".join(lines))
    return "\n".join(out)


def _veto_block() -> str:
    # PART 2: Turn each problem's description into an instruction to flag it.
    return "\n".join(
        f'- **{name}** — true if {spec["prompt"]}' for name, spec in VETO_FLAGS.items()
    )


# PART 1 — ROLE: tell the AI it is planning searches for teaching figures.
PLANNER_SYSTEM = """\
You plan image searches for someone building teaching slides. You are good at \
two things: knowing what a genuinely useful figure for a concept looks like, \
and knowing the exact words that surface it on the open web.

You never guess at what a figure "probably" shows. You state concretely what \
must appear in it for it to earn a place on the slide."""

# PART 1 — TASK: llm.make_plan replaces the {...} placeholders with your inputs.
# The notes/context describe the need; checklist guidance defines a useful image;
# query guidance controls how the AI searches for it. PLAN_SCHEMA below specifies
# the response fields. The generated queries are later sent to search APIs.
PLANNER_PROMPT = """\
Here are the presenter's jot notes for one slide:

<notes>
{notes}
</notes>

<context>
Audience: {audience}
Preferred visual style: {style}
Label language: {language}
</context>

Work out what figure this slide needs, then produce search queries that will \
find it.

Guidance for the checklist:
- `must_show` is the pass/fail list — the specific structures, stages, axes, \
labels or relationships without which the figure fails this slide. Be concrete \
and name things ("the four chambers named individually: rumen, reticulum, \
omasum, abomasum"), not vague ("digestive anatomy"). Keep it to 3-6 items.
- `should_avoid` is what a wrong-but-plausible result looks like for this \
topic — the trap a naive image search falls into.
- For a labelled diagram, explicitly require visible, readable labels in \
{language} for the requested structures. Unlabelled and wrong-language versions \
belong in `should_avoid`.

Guidance for the queries:
- Start with a short query containing only the core subject and visual type, \
such as "animal cell diagram" or "ruminant stomach diagram". Keep at least \
half the queries to 2-5 words; leave the detailed checklist for visual grading.
- Search for the figure itself. Do not add "textbook", "book", "for students", \
or publisher names as quality hints: these can surface book covers.
- Write {n_queries} queries that attack the topic from different angles, not \
{n_queries} rewordings of one. Useful angles: the precise technical term; the \
term a textbook index would use; the specialist field's name for it \
(`veterinary`, `histology`); a query naming the specific labels you want to \
see; the term as a non-specialist would type it.
- Use plain keywords only. No search operators — no `site:`, no quotes, no \
minus signs. These queries are sent to several different image APIs, most of \
which treat operators as literal text and return nothing.
- Include words that bias toward explanatory artwork rather than photographs \
when that is what is wanted — "diagram", "labelled", "schematic", "cross \
section", "anatomy of".
- Never include the word "free" or "stock". They pull in watermarked junk.
- Include a query for a labelled {language} version while keeping the first \
query short and broad.
"""

# PART 2 — ROLE (also reused in Part 3): be strict and judge visible legibility.
SCORER_SYSTEM = """\
You grade candidate figures for teaching slides. You are strict, specific, and \
you never inflate a score to be agreeable — a mediocre figure that gets a 4 \
wastes the presenter's time more than a bad figure that gets a 1.

You are looking at each image at roughly the size it will appear on a slide. \
Judge legibility at exactly the size you see it. Do not speculate about the \
original file's resolution — that is measured separately."""

# PART 2 — TASK: carry forward Part 1's concept, required elements, and pitfalls.
# The next paragraphs require evidence from the image and readable labels.
# {criteria} and {vetoes} receive the generated rules from the lists above.
# The final instructions request observed/missing elements and an explanation.
# llm.score_candidates appends candidate IDs, source hints, and actual images.
SCORER_PROMPT = """\
The presenter needs one figure for a slide.

<concept>{concept}</concept>

<what_the_figure_must_show>
{must_show}
</what_the_figure_must_show>

<what_would_be_nice>
{nice_to_have}
</what_would_be_nice>

<common_wrong_results_for_this_topic>
{should_avoid}
</common_wrong_results_for_this_topic>

<context>
Audience: {audience}
Preferred visual style: {style}
Label language: {language}
</context>

Grade every candidate image below on each criterion:

Judge the visible image, not its filename, source reputation, or book title. \
A cover, title page, or contents page is not a diagram of the requested concept. \
For these, raise book_cover_or_title_page and score subject_match and \
checklist_coverage as 0. Never infer that the image shows a figure inside a book.

For a labelled diagram, verify each required label on the image itself in \
{language}. English search text, filenames, and captions do not establish that \
the diagram's labels are English. List the label text you actually read in \
elements_found. Raise missing_required_labels for absent or unverifiable \
required labels, foreign_language_labels for labels lacking the requested \
language, and unreadable_text when labels cannot be read. A bilingual diagram \
is acceptable only if all required labels are also readable in {language}. \
Standard scientific names, abbreviations, and symbols used in that language \
are acceptable; do not mistake these alone for foreign-language labels.

{criteria}

Then raise any of these flags that apply:

{vetoes}

For each candidate also return:
- `elements_found`: which items from the must-show list you can actually see in \
the image. Only list an item if you can point at it.
- `elements_missing`: must-show items that are absent.
- `verdict`: one sentence, concrete, naming what is actually in the picture. \
"Cutaway of a cow's abdomen with all four stomach chambers labelled in English, \
plus arrows for feed flow" — not "a good diagram of ruminant digestion".
- `caveat`: the single thing that would stop you using it, or null.

Grade every candidate. Do not skip any. Use the candidate ids exactly as given.
"""

# PART 3 — TASK: compare eligible finalists for practical use on the slide.
# llm.judge inserts the concept/checklist and appends each finalist's image.
# This is a separate comparison after scoring; it returns a pick via PICK_SCHEMA.
JUDGE_PROMPT = """\
These are the top-scoring candidates for the slide concept below. Pick the one \
you would actually put on the slide, and say why it beats the runner-up in one \
or two sentences. Weigh usability on a slide as heavily as correctness — a \
slightly less complete figure that is clean and readable usually wins over a \
dense, correct one.

<concept>{concept}</concept>
<what_the_figure_must_show>
{must_show}
</what_the_figure_must_show>
"""


# ---------------------------------------------------------------------------
# Output schemas.
#
# Written to satisfy OpenAI's *strict* structured-output rules, which are the
# stricter of the two providers:
#   · every property must be listed in "required" (use ["type","null"] for
#     genuinely optional values rather than omitting them)
#   · every object needs "additionalProperties": false
#   · numeric minimum/maximum aren't reliably supported — an integer enum is,
#     and constrains the model harder anyway
# Anthropic accepts these same schemas as tool input_schemas unchanged.
# ---------------------------------------------------------------------------

def _obj(properties: dict) -> dict:
    # SHARED: require every declared response field and disallow extra fields.
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


# PART 1 — RESPONSE FORMAT: the search plan the AI returns as structured data.
# concept/must_show/nice_to_have/should_avoid feed Part 2; queries feed search.
# figure_brief describes the ideal image; wants_diagram records the desired type.
PLAN_SCHEMA = _obj({
    "concept": {
        "type": "string",
        "description": "The concept in one precise noun phrase, as a textbook would name it.",
    },
    "figure_brief": {
        "type": "string",
        "description": "One or two sentences describing the ideal figure for this slide.",
    },
    "must_show": {
        "type": "array",
        "items": {"type": "string"},
        "description": "3-6 concrete elements the figure must contain.",
    },
    "nice_to_have": {"type": "array", "items": {"type": "string"}},
    "should_avoid": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Wrong-but-plausible results this topic attracts.",
    },
    "wants_diagram": {
        "type": "boolean",
        "description": "True if an explanatory diagram is wanted rather than a photograph.",
    },
    "queries": {
        "type": "array",
        "items": _obj({
            "q": {"type": "string"},
            "angle": {
                "type": "string",
                "description": "Why this query is different from the others.",
            },
        }),
    },
})


def build_score_schema() -> dict:
    """Built from CRITERIA/VETO_FLAGS so the schema can never drift from the prompt."""
    # PART 2 — RESPONSE FORMAT: one entry per image with its ID, integer grades,
    # boolean problem flags, observed/missing elements, verdict, and caveat.
    props = {"id": {"type": "string", "description": "The candidate id exactly as given."}}
    for c in CRITERIA:
        props[c.key] = {
            "type": "integer",
            "enum": list(range(c.max_score + 1)),
            "description": c.label,
        }
    for name, spec in VETO_FLAGS.items():
        props[name] = {"type": "boolean", "description": spec["prompt"]}
    props.update({
        "elements_found": {"type": "array", "items": {"type": "string"}},
        "elements_missing": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string"},
        "caveat": {"type": ["string", "null"]},
    })
    return _obj({"candidates": {"type": "array", "items": _obj(props)}})


# PART 3 — RESPONSE FORMAT: chosen image ID, explanation, and optional runner-up.
PICK_SCHEMA = _obj({
    "winner_id": {"type": "string"},
    "why": {"type": "string"},
    "runner_up_id": {"type": ["string", "null"]},
})


def render_scorer_prompt(plan: dict, audience: str, style: str, language: str) -> str:
    # PART 2 — ASSEMBLY: combine the fixed template, Part 1's generated checklist,
    # your context, and the shared grading rules into the text sent to the AI.
    def bullets(items):
        return "\n".join(f"- {i}" for i in items) or "- (none specified)"

    return SCORER_PROMPT.format(
        concept=plan.get("concept", ""),
        must_show=bullets(plan.get("must_show", [])),
        nice_to_have=bullets(plan.get("nice_to_have", [])),
        should_avoid=bullets(plan.get("should_avoid", [])),
        audience=audience,
        style=style,
        language=language,
        criteria=_criteria_block(),
        vetoes=_veto_block(),
    )
