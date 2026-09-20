"""Download candidate images, normalise them, and drop near-duplicates."""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import requests
from PIL import Image, ImageOps

from . import config
from .search import Candidate

Image.MAX_IMAGE_PIXELS = 200_000_000


def _svg_to_png(data: bytes) -> Optional[bytes]:
    try:
        import cairosvg  # optional
    except ImportError:
        return None
    try:
        return cairosvg.svg2png(bytestring=data, output_width=1400)
    except Exception:
        return None


def _download(url: str) -> Optional[bytes]:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "image/*,*/*"}
    try:
        with requests.get(
            url, headers=headers, timeout=config.DOWNLOAD_TIMEOUT, stream=True
        ) as r:
            if r.status_code != 200:
                return None
            buf = io.BytesIO()
            for chunk in r.iter_content(65536):
                buf.write(chunk)
                if buf.tell() > config.MAX_DOWNLOAD_BYTES:
                    return None
            return buf.getvalue()
    except requests.RequestException:
        return None


def _dhash(img: Image.Image, size: int = 8) -> int:
    """64-bit difference hash. Catches the same figure re-hosted and rescaled."""
    small = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return bits


def _prepare(data: bytes, mime: str) -> Optional[Tuple[Image.Image, int, int]]:
    # Commons keeps the original SVG MIME while serving a PNG preview.
    # Decode raster bytes first; only actual SVG data needs conversion.
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        if not (data.lstrip().startswith((b"<?xml", b"<svg")) or "svg" in (mime or "")):
            return None
        png = _svg_to_png(data)
        if png is None:
            return None
        try:
            img = Image.open(io.BytesIO(png))
            img.load()
        except Exception:
            return None
    w, h = img.size
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")   # diagrams are usually on transparency
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    return img, w, h


def hydrate(candidates: List[Candidate], workdir: str, verbose=True) -> List[Candidate]:
    """Download, validate, save a slide-scale copy, hash. Returns survivors."""
    os.makedirs(workdir, exist_ok=True)

    def work(c: Candidate) -> Candidate:
        data = _download(c.fetch_url)
        if not data:
            c.rejected = "download failed"
            return c
        prepared = _prepare(data, c.mime)
        if prepared is None:
            c.rejected = "not a readable image"
            return c
        img, w, h = prepared
        c.real_width, c.real_height = w, h
        c.is_vector = "svg" in (c.mime or "").lower()
        # A source that reported dimensions (Wikimedia gives the ORIGINAL size
        # while handing us a rasterised thumb) is more authoritative than the
        # pixels we just decoded.
        if c.width and c.height and c.width > w:
            c.real_width, c.real_height = c.width, c.height

        if w < config.MIN_WIDTH or h < config.MIN_HEIGHT or w * h < config.MIN_PIXELS:
            c.rejected = f"too small ({w}x{h})"
            return c
        ar = w / h if h else 0
        if not (config.MIN_ASPECT_RATIO <= ar <= config.MAX_ASPECT_RATIO):
            c.rejected = f"bad aspect ({ar:.2f})"
            return c

        c.dhash = _dhash(img)
        if config.EXPORT_MAX_EDGE > 0:
            export = img.copy()
            export.thumbnail((config.EXPORT_MAX_EDGE, config.EXPORT_MAX_EDGE), Image.LANCZOS)
            export_path = os.path.join(workdir, f"{c.id}.download.jpg")
            export.save(
                export_path, "JPEG", quality=config.EXPORT_JPEG_QUALITY,
                optimize=True, progressive=True,
            )
        small = img.copy()
        small.thumbnail((config.VISION_MAX_EDGE, config.VISION_MAX_EDGE), Image.LANCZOS)
        path = os.path.join(workdir, f"{c.id}.jpg")
        small.save(path, "JPEG", quality=config.VISION_JPEG_QUALITY, optimize=True)
        c.local_path = path
        return c

    with ThreadPoolExecutor(max_workers=config.DOWNLOAD_WORKERS) as pool:
        candidates = list(pool.map(work, candidates))

    ok = [c for c in candidates if not c.rejected]
    failed = len(candidates) - len(ok)

    # Near-duplicate removal: keep the highest-resolution copy of each figure.
    ok.sort(key=lambda c: c.pixels, reverse=True)
    kept: List[Candidate] = []
    dupes = 0
    for c in ok:
        if any(
            bin(c.dhash ^ k.dhash).count("1") <= config.DHASH_DUPLICATE_DISTANCE
            for k in kept
        ):
            dupes += 1
            continue
        kept.append(c)

    if verbose:
        bits = []
        if failed:
            bits.append(f"{failed} unusable")
        if dupes:
            bits.append(f"{dupes} duplicates")
        print(
            f"    {len(kept)} usable images"
            + (f"  (dropped: {', '.join(bits)})" if bits else "")
        )
    return kept
