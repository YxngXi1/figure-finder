"""
Tunable knobs for the whole pipeline.

Everything here is cheap, deterministic filtering and weighting. The *judgement*
lives in rubric.py. Start by tuning rubric.py; come here when you want to change
how aggressively candidates are thrown away before they cost you a vision call.
"""

import os

# --- Provider and models ----------------------------------------------------

# "openai" or "anthropic". Override with FF_PROVIDER or --provider.
PROVIDER = os.environ.get("FF_PROVIDER", "openai").lower()

# planner : turns your jot notes into search queries + a per-topic checklist
# scorer  : grades each candidate image — where nearly all the spend goes
# fast    : the --fast alternative to scorer; cheaper and blunter
# judge   : final head-to-head on the top few
MODELS = {
    "openai": {
        "planner": os.environ.get("FF_PLANNER_MODEL", "gpt-5.6-terra"),
        "scorer": os.environ.get("FF_SCORER_MODEL", "gpt-5.6-terra"),
        "fast": os.environ.get("FF_FAST_MODEL", "gpt-5.6-luna"),
        "judge": os.environ.get("FF_JUDGE_MODEL", "gpt-5.6-terra"),
    },
    "anthropic": {
        "planner": os.environ.get("FF_PLANNER_MODEL", "claude-sonnet-5"),
        "scorer": os.environ.get("FF_SCORER_MODEL", "claude-sonnet-5"),
        "fast": os.environ.get("FF_FAST_MODEL", "claude-haiku-4-5-20251001"),
        "judge": os.environ.get("FF_JUDGE_MODEL", "claude-sonnet-5"),
    },
}

# OpenAI image fidelity: "high" or "low". "low" downsamples every image to
# 512px, which is cheap but destroys the small label text you need to read to
# judge legibility. Keep this on "high" unless you're just smoke-testing.
OPENAI_IMAGE_DETAIL = os.environ.get("FF_IMAGE_DETAIL", "high")

# --- Search sources ---------------------------------------------------------

def env(name: str, required: bool = True) -> str:
    v = os.environ.get(name, "")
    if not v and required:
        raise RuntimeError(f"{name} is not set — put it in .env")
    return v


# Which sources to query, in order. Override with FF_SOURCES or --sources.
#   google     broad, but needs 2 keys and is capped at 100 queries/day. Note
#              that "search the entire web" is unavailable on engines created
#              after 2026-01-20 and is switched off for everyone on 2027-01-01,
#              so it now means "the sites configured on your engine".
#   wikimedia  no key, no quota, no expiry. Excellent for science diagrams.
#   openverse  no key needed, throttled when anonymous. Best-effort.
SOURCES = [s for s in os.environ.get(
    "FF_SOURCES", "wikimedia,openverse,google").split(",") if s.strip()]

GOOGLE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
WIKIMEDIA_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_ENDPOINT = "https://api.openverse.org/v1/images/"

# Wikimedia will rasterise SVGs to this width for us, which is what lets the
# pipeline use Commons' (many, excellent) vector diagrams without cairosvg.
WIKIMEDIA_RASTER_WIDTH = 1600

# Wikimedia's API policy requires a descriptive User-Agent; generic ones get
# blocked. Put your own contact in here if you run this heavily.
API_USER_AGENT = os.environ.get(
    "FF_API_USER_AGENT",
    "figure-finder/0.2 (slide figure search; https://github.com/local/figure-finder)",
)

MAX_QUERIES = 5
RESULTS_PER_QUERY = {"google": 10, "wikimedia": 20, "openverse": 20}

# Google's safesearch: "off" | "active"
SAFE_SEARCH = "active"

# --- Cheap prefilters (applied to CSE metadata, before downloading) ----------

MIN_WIDTH = 500
MIN_HEIGHT = 400
MIN_PIXELS = 350_000          # ~700x500; below this text will mush on a slide
MAX_ASPECT_RATIO = 3.2        # wider than this won't lay out on a 16:9 slide
MIN_ASPECT_RATIO = 0.30

# Sites that will hand you a watermarked or paywalled image. Not a moral
# judgement, just: the file you can actually download is unusable on a slide.
DOMAIN_BLOCKLIST = {
    "shutterstock.com", "istockphoto.com", "gettyimages.com", "alamy.com",
    "dreamstime.com", "123rf.com", "depositphotos.com", "vectorstock.com",
    "canstockphoto.com", "stock.adobe.com", "agefotostock.com",
    "pinterest.com", "pinimg.com",   # dead-end links, usually re-hosted + cropped
    "slideshare.net", "slideplayer.com",  # low-res screenshots of other decks
    "lookaside.fbsbx.com", "scontent.xx.fbcdn.net",
}

# Domains worth a small nudge up: primary sources, textbooks, .edu, museums.
DOMAIN_BONUS = {
    "wikimedia.org": 4, "wikipedia.org": 4, "commons.wikimedia.org": 4,
    "ncbi.nlm.nih.gov": 5, "nature.com": 5, "sciencedirect.com": 4,
    "britannica.com": 4, "khanacademy.org": 3, "openstax.org": 6,
    "biologydictionary.net": 2, "researchgate.net": 3, "frontiersin.org": 4,
    "usda.gov": 4, "extension.org": 3, "merckvetmanual.com": 5,
}
EDU_TLD_BONUS = 3  # any .edu / .ac.uk / .gov host

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 15
DOWNLOAD_WORKERS = 8
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Perceptual near-duplicate threshold (Hamming distance on a 64-bit dhash).
# <= 6 means "same figure, different host/rescale". Raise to be more aggressive.
DHASH_DUPLICATE_DISTANCE = 6

# --- Vision scoring ---------------------------------------------------------

# Images per Claude call. Batching gives the model comparative context (it
# calibrates better when it sees several at once) and cuts prompt overhead.
# Above ~6 the model starts blurring candidates together.
SCORE_BATCH_SIZE = 5

# Long edge the image is resized to before sending. This roughly matches how
# large the figure will render on a slide, which is exactly the scale at which
# you want legibility judged.
VISION_MAX_EDGE = 1024
VISION_JPEG_QUALITY = 82

# Hard ceiling on how many candidates get a (paid) vision look.
MAX_SCORED_CANDIDATES = 20

# --- Deterministic resolution term ------------------------------------------
# The model only ever sees a 1024px version, so it cannot judge true resolution.
# We score that ourselves from the original dimensions.
# Full-bleed on a 1080p slide wants ~1600px wide; half-slide wants ~900px.
RESOLUTION_IDEAL_WIDTH = 1600
RESOLUTION_FLOOR_WIDTH = 700
RESOLUTION_WEIGHT = 8  # out of 100 total

# --- Output -----------------------------------------------------------------

DEFAULT_TOP_N = 5
OUTPUT_DIR = os.environ.get("FF_OUTPUT_DIR", "./figurefinder_out")
