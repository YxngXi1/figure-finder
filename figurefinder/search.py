"""
Candidate retrieval from pluggable image sources.

Four sources ship:
  brave      Brave Image Search. Broad web coverage without a configured domain
             allowlist. Requires BRAVE_API_KEY and an image-search subscription.
  google     Google Custom Search. Broad, but needs two keys, is capped at 100
             queries/day on the free tier, and its "search the entire web"
             option is gone for engines created after 2026-01-20 (and dies
             completely on 2027-01-01) — so it now really means "search the
             sites configured on your engine".
  wikimedia  Wikimedia Commons. No key, no quota, no expiry. Where a great many
             good scientific and anatomical diagrams actually live.
  openverse  Openverse (WordPress). No key needed, aggregates Flickr, museums,
             science orgs. Rate-limited when anonymous — treated as best-effort.

Each source returns Candidates; collect_candidates merges, dedupes and applies
the cheap metadata prefilters.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

from . import config


class SearchError(RuntimeError):
    """Fatal — the run can't continue (e.g. bad search API credentials)."""


class SourceUnavailable(RuntimeError):
    """Non-fatal — this source is skipped and the run continues on the others."""


@dataclass
class Candidate:
    id: str = ""
    image_url: str = ""          # canonical URL, shown to the user
    download_url: str = ""       # what we actually fetch (a rasterised thumb for SVG)
    page_url: str = ""
    title: str = ""
    snippet: str = ""
    host: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    byte_size: int = 0
    thumbnail: str = ""
    source: str = ""             # which backend found it
    license: str = ""            # populated by wikimedia/openverse only
    found_by: List[str] = field(default_factory=list)

    # filled in later
    local_path: Optional[str] = None
    real_width: int = 0
    real_height: int = 0
    is_vector: bool = False
    dhash: Optional[int] = None
    scores: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    final_score: float = 0.0
    verdict: str = ""
    caveat: Optional[str] = None
    elements_found: List[str] = field(default_factory=list)
    elements_missing: List[str] = field(default_factory=list)
    rejected: Optional[str] = None

    @property
    def fetch_url(self) -> str:
        return self.download_url or self.image_url

    @property
    def pixels(self) -> int:
        return (self.real_width or self.width) * (self.real_height or self.height)

    @property
    def aspect(self) -> float:
        h = self.real_height or self.height
        return (self.real_width or self.width) / h if h else 0.0


# ---------------------------------------------------------------------------
# Host helpers
# ---------------------------------------------------------------------------

def _host_of(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _blocked(host: str) -> bool:
    return any(host == b or host.endswith("." + b) for b in config.DOMAIN_BLOCKLIST)


def domain_bonus(host: str) -> int:
    for domain, pts in config.DOMAIN_BONUS.items():
        if host == domain or host.endswith("." + domain):
            return pts
    if re.search(r"\.(edu|gov)$|\.ac\.[a-z]{2}$|\.edu\.[a-z]{2}$", host):
        return config.EDU_TLD_BONUS
    return 0


def _is_image_mime(mime: str) -> bool:
    if not mime:
        return True          # unknown; let the download decide
    mime = mime.lower().split(";", 1)[0].strip()
    return mime.startswith("image/") and "tiff" not in mime and "djvu" not in mime


# ---------------------------------------------------------------------------
# Source: Brave Image Search
# ---------------------------------------------------------------------------

def brave(query: str, limit: int) -> List[Candidate]:
    token = config.env("BRAVE_API_KEY", required=False)
    if not token or token.startswith("[") or "PASTE" in token.upper():
        raise SearchError("Set BRAVE_API_KEY in .env to use Brave Image Search.")
    params = {
        "q": query, "count": min(max(limit, 1), 200), "country": "ALL",
        "safesearch": "off" if config.SAFE_SEARCH == "off" else "strict",
        # Preserve specialist terms from the planner rather than auto-correcting.
        "spellcheck": "false",
    }
    try:
        response = requests.get(
            config.BRAVE_ENDPOINT, params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": token},
            timeout=25,
        )
    except requests.RequestException as e:
        # Don't echo raw request details or credentials in terminal errors.
        raise SearchError("Could not reach Brave Image Search; check your connection.") from e
    if response.status_code in (401, 403):
        raise SearchError("Brave rejected the API key. Check BRAVE_API_KEY and "
                          "that its subscription includes image search.")
    if response.status_code == 429:
        raise SearchError("Brave search rate limit or quota reached. Retry later "
                          "or check your Brave API usage and spending limit.")
    if response.status_code != 200:
        raise SearchError(f"Brave Image Search returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as e:
        raise SearchError("Brave Image Search returned invalid JSON.") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise SearchError("Brave Image Search returned an unexpected response.")

    out = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        properties = item.get("properties") or {}
        url = properties.get("url") or ""
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        page_url = item.get("url") or ""
        out.append(Candidate(
            image_url=url, page_url=page_url,
            host=_host_of(page_url) or _host_of(url),
            title=(item.get("title") or "").strip(),
            # Use original dimensions, never the small thumbnail's dimensions.
            # Unknown dimensions survive until fetch.hydrate measures the file.
            width=int(properties.get("width") or 0),
            height=int(properties.get("height") or 0),
            thumbnail=(item.get("thumbnail") or {}).get("src") or "",
            source="brave",
        ))
    return out


# ---------------------------------------------------------------------------
# Source: Google Custom Search
# ---------------------------------------------------------------------------

def google_cse(query: str, limit: int) -> List[Candidate]:
    api_key = config.env("GOOGLE_API_KEY")
    cx = config.env("GOOGLE_CSE_ID")
    params = {
        "key": api_key, "cx": cx, "q": query, "searchType": "image",
        "num": min(limit, 10), "safe": config.SAFE_SEARCH,
    }
    try:
        r = requests.get(config.GOOGLE_ENDPOINT, params=params, timeout=20)
    except requests.RequestException as e:
        raise SearchError(f"network error talking to Google CSE: {e}") from e

    body_lc = r.text.lower()
    if r.status_code == 429 or (
        r.status_code == 403
        and any(w in body_lc for w in ("ratelimitexceeded", "dailylimitexceeded", "quota"))
    ):
        raise SearchError(
            "Google CSE quota exhausted (free tier is 100 queries/day). "
            "Wait for the daily reset, enable billing, or run with "
            "--sources wikimedia,openverse which have no quota."
        )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:
            detail = r.text[:300]
        raise SearchError(f"Google CSE returned {r.status_code}: {detail}")

    out = []
    for it in r.json().get("items", []) or []:
        url = it.get("link", "")
        if not url:
            continue
        img = it.get("image", {}) or {}
        page_url = img.get("contextLink", "")
        out.append(Candidate(
            image_url=url, page_url=page_url,
            host=_host_of(page_url) or _host_of(url),
            title=(it.get("title") or "").strip(),
            snippet=(it.get("snippet") or "").strip(),
            mime=it.get("mime", ""),
            width=int(img.get("width") or 0), height=int(img.get("height") or 0),
            byte_size=int(img.get("byteSize") or 0),
            thumbnail=img.get("thumbnailLink", ""),
            source="google",
        ))
    return out


# ---------------------------------------------------------------------------
# Source: Wikimedia Commons
# ---------------------------------------------------------------------------

def wikimedia(query: str, limit: int) -> List[Candidate]:
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrnamespace": "6",          # 6 = File:
        "gsrlimit": str(min(limit, 50)),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        # Asks Commons to rasterise a 1600px-wide copy. For the many SVG
        # diagrams on Commons this is what makes them usable without cairosvg.
        "iiurlwidth": str(config.WIKIMEDIA_RASTER_WIDTH),
    }
    try:
        r = requests.get(config.WIKIMEDIA_ENDPOINT, params=params,
                         headers={"User-Agent": config.API_USER_AGENT}, timeout=25)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages", {}) or {}
    except (requests.RequestException, ValueError) as e:
        raise SourceUnavailable(f"Wikimedia Commons: {e}") from e

    out = []
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        ii = infos[0]
        mime = ii.get("mime", "")
        if not _is_image_mime(mime):
            continue
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        out.append(Candidate(
            image_url=ii.get("url", ""),
            # Prefer the rasterised thumb: it's smaller, and for SVG it's the
            # only form we can open at all.
            download_url=ii.get("thumburl") or ii.get("url", ""),
            page_url=ii.get("descriptionurl", ""),
            host="commons.wikimedia.org",
            title=(page.get("title", "") or "").replace("File:", "").rsplit(".", 1)[0],
            mime=mime,
            # Original dimensions, not the thumb's — the resolution score wants these.
            width=int(ii.get("width") or 0), height=int(ii.get("height") or 0),
            license=lic, source="wikimedia",
        ))
    return out


# ---------------------------------------------------------------------------
# Source: Openverse
# ---------------------------------------------------------------------------

def openverse(query: str, limit: int) -> List[Candidate]:
    params = {
        "q": query, "page_size": min(limit, 20),
        "size": "large", "mature": "false",
    }
    headers = {"User-Agent": config.API_USER_AGENT}
    if token := config.env("OPENVERSE_TOKEN", required=False):
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(config.OPENVERSE_ENDPOINT, params=params,
                         headers=headers, timeout=25)
        if r.status_code == 429:
            raise SourceUnavailable(
                "Openverse rate limit hit (anonymous access is throttled). "
                "Register a free token and set OPENVERSE_TOKEN to raise it."
            )
        r.raise_for_status()
        results = r.json().get("results", []) or []
    except SourceUnavailable:
        raise
    except (requests.RequestException, ValueError) as e:
        raise SourceUnavailable(f"Openverse: {e}") from e

    out = []
    for it in results:
        url = it.get("url") or ""
        if not url:
            continue
        page_url = it.get("foreign_landing_url") or url
        ext = (it.get("filetype") or "").lower()
        out.append(Candidate(
            image_url=url, page_url=page_url, host=_host_of(page_url) or _host_of(url),
            title=(it.get("title") or "").strip(),
            mime=f"image/{ext}" if ext else "",
            # Openverse often returns null dimensions; 0 means "check after download".
            width=int(it.get("width") or 0), height=int(it.get("height") or 0),
            thumbnail=it.get("thumbnail") or "",
            license=(it.get("license") or "").upper(),
            source="openverse",
        ))
    return out


SOURCE_FUNCS: Dict[str, Callable[[str, int], List[Candidate]]] = {
    "brave": brave,
    "google": google_cse,
    "wikimedia": wikimedia,
    "openverse": openverse,
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect_candidates(queries: List[str], sources: Optional[List[str]] = None,
                       verbose=True) -> List[Candidate]:
    """Run enabled searches concurrently, then dedupe and prefilter.

    Results are consumed round-robin by query rank. This keeps several query
    angles represented when the caller caps downloads later in the pipeline.
    """
    sources = sources or config.SOURCES
    by_url: Dict[str, Candidate] = {}
    dropped = {"blocked domain": 0, "too small": 0, "bad aspect": 0, "not an image": 0}
    n = 0

    for name in sources:
        fn = SOURCE_FUNCS.get(name)
        if fn is None:
            raise SearchError(f"unknown source {name!r}; pick from {list(SOURCE_FUNCS)}")
        per_query = config.RESULTS_PER_QUERY.get(name, 10)
        if verbose:
            print(f"  {name}")
        source_queries = queries[: config.MAX_QUERIES]
        # Brave is the latency-sensitive broad index and tolerates concurrent
        # query angles. Keep the optional public/community APIs serial to avoid
        # turning their tighter anonymous rate limits into failures.
        source_workers = config.SEARCH_WORKERS if name == "brave" else 1
        with ThreadPoolExecutor(
            max_workers=min(source_workers, len(source_queries) or 1),
            thread_name_prefix=f"search-{name}",
        ) as pool:
            futures = [(q, pool.submit(fn, q, per_query)) for q in source_queries]

        query_results = []
        unavailable = None
        for q, future in futures:
            try:
                items = future.result()
            except SourceUnavailable as e:
                unavailable = unavailable or e
                items = []
            if verbose:
                print(f"    {len(items):>2} results  ·  {q}")
            query_results.append((q, items))
        if unavailable and verbose:
            print(f"    skipped unavailable requests — {unavailable}")

        max_results = max((len(items) for _, items in query_results), default=0)
        for rank in range(max_results):
            for q, items in query_results:
                if rank >= len(items):
                    continue
                c = items[rank]
                if not c.image_url:
                    continue
                if c.image_url in by_url:
                    by_url[c.image_url].found_by.append(q)
                    continue
                if _blocked(c.host) or _blocked(_host_of(c.image_url)):
                    dropped["blocked domain"] += 1
                    continue
                if not _is_image_mime(c.mime):
                    dropped["not an image"] += 1
                    continue
                if urlparse(c.image_url).path.lower().endswith((".djvu", ".djv", ".pdf")):
                    dropped["not an image"] += 1
                    continue
                # Dimensions are unknown for some sources — those get checked
                # after download instead of being thrown away here.
                if c.width and c.height:
                    if (c.width < config.MIN_WIDTH or c.height < config.MIN_HEIGHT
                            or c.width * c.height < config.MIN_PIXELS):
                        dropped["too small"] += 1
                        continue
                    ar = c.width / c.height
                    if not (config.MIN_ASPECT_RATIO <= ar <= config.MAX_ASPECT_RATIO):
                        dropped["bad aspect"] += 1
                        continue

                n += 1
                c.id = f"c{n}"
                c.found_by = [q]
                by_url[c.image_url] = c

    if verbose:
        drops = ", ".join(f"{v} {k}" for k, v in dropped.items() if v)
        print(f"    kept {len(by_url)} candidates" + (f"  (dropped: {drops})" if drops else ""))
    return list(by_url.values())
