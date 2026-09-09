"""
THE RUBRIC — this is the file you tune.

Two things live here:

  1. CRITERIA  — the dimensions a figure is graded on, their weights, and the
     anchored descriptions the model is given. The prompt text is *generated*
     from this list, so a weight change and a prompt change are the same edit
     and can never drift apart.

  2. The prompts themselves — planner (notes -> queries + topic-specific
     requirements) and scorer (image -> graded JSON).

Design notes, because they matter more than the code:

  * Scores are anchored. "0-5, higher is better" produces mush; every criterion
    below spells out what a 0, a 3 and a 5 look like. This is the single
    biggest lever on output quality.

  * Vetoes are separate from scores. A watermark isn't "slightly less pretty",
    it's disqualifying. Mixing those into one number lets a gorgeous unusable
    image win.

  * The planner writes a *topic-specific* checklist (must_show) before anything
    is searched. The scorer then grades against that concrete checklist rather
    than the vague notion of "a good diagram". This is what makes it notice
    that a ruminant diagram is missing the omasum.
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Criterion:
    key: str
    weight: int          # contribution to the final 0-100 score
    max_score: int       # scale the model grades on
    label: str
    anchors: Dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
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
        weight=12,
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
        "prompt": "a stock-photo watermark, tiling logo, or 'SAMPLE'/'PREVIEW' overlay",
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
        "prompt": "it is a screenshot of a webpage/slide/PDF rather than the figure itself",
    },
    "ai_generated_looking": {
        "multiplier": 0.25,
        "prompt": "it has the hallmarks of AI image generation — garbled text, "
                  "impossible anatomy, melted labels. These are near-useless for teaching.",
    },
}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _criteria_block() -> str:
    out = []
    for c in CRITERIA:
        lines = [f"- **{c.key}** ({c.label}) — integer 0-{c.max_score}"]
        for score in sorted(c.anchors):
            lines.append(f"    {score} = {c.anchors[score]}")
        out.append("\n".join(lines))
    return "\n".join(out)


def _veto_block() -> str:
    return "\n".join(
        f'- **{name}** — true if {spec["prompt"]}' for name, spec in VETO_FLAGS.items()
    )


PLANNER_SYSTEM = """\
You plan image searches for someone building teaching slides. You are good at \
two things: knowing what a genuinely useful figure for a concept looks like, \
and knowing the exact words that surface it on the open web.

You never guess at what a figure "probably" shows. You state concretely what \
must appear in it for it to earn a place on the slide."""

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

SCORER_SYSTEM = """\
You grade candidate figures for teaching slides. You are strict, specific, and \
you never inflate a score to be agreeable — a mediocre figure that gets a 4 \
wastes the presenter's time more than a bad figure that gets a 1.

You are looking at each image at roughly the size it will appear on a slide. \
Judge legibility at exactly the size you see it. Do not speculate about the \
original file's resolution — that is measured separately."""

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
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


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


PICK_SCHEMA = _obj({
    "winner_id": {"type": "string"},
    "why": {"type": "string"},
    "runner_up_id": {"type": ["string", "null"]},
})


def render_scorer_prompt(plan: dict, audience: str, style: str, language: str) -> str:
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
