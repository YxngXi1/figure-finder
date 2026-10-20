"""Conservative text extraction and blank-space placement; coordinates in points."""

from __future__ import annotations

import math
from functools import lru_cache
from PIL import ImageFont

CAPTION_FONT_SIZE = 12
CITATION_FONT_SIZE = 10


@lru_cache(maxsize=8)
def _layout_font(font_size):
    # Measure Arial where available; Liberation Sans has compatible metrics.
    # Four pixels per point gives accurate wrapping even at small citation sizes.
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf",
                 "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, round(font_size * 4))
        except OSError:
            continue
    return ImageFont.load_default(size=round(font_size * 4))


def caption_lines(text, width, font_size=CAPTION_FONT_SIZE):
    font = _layout_font(font_size)
    available = max(1, width - 12)
    def fits(value):
        return font.getlength(value) / 4 * 1.08 <= available
    lines, line = [], ""
    for word in text.split():
        combined = f"{line} {word}" if line else word
        if fits(combined):
            line = combined
            continue
        if line:
            lines.append(line)
            line = ""
        for character in word:
            if line and not fits(line + character):
                lines.append(line)
                line = ""
            line += character
    if line:
        lines.append(line)
    return lines or [""]


def text_height(text, width, font_size):
    return 12 + len(caption_lines(text, width, font_size)) * font_size * 1.4


def fit_cited_image(slot, image_width, image_height, caption, citation, minimum_width=0):
    """Fit a top-aligned stack with identical left/right edges and no image crop."""
    x, y, w, h = slot
    # Text wraps discontinuously, so check widths instead of binary-searching.
    for step in range(201):
        bw = w * (1 - step / 250)
        if bw < max(minimum_width, 48):
            break
        ih = bw * image_height / image_width
        ch = text_height(caption, bw, CAPTION_FONT_SIZE)
        sh = text_height(citation, bw, CITATION_FONT_SIZE)
        if ih + 8 + ch + 4 + sh > h:
            continue
        left = x + (w-bw)/2
        return ([left, y, bw, ih], [left, y+ih+8, bw, ch],
                [left, y+ih+8+ch+4, bw, sh])
    return None


def aligned_slots(slot, target_boxes):
    """Prefer the top of related text when it leaves room; fall back to free space."""
    if target_boxes:
        x, y, w, h = slot
        top = min(box[1] for box in target_boxes)
        if y < top < y+h:
            return [[x, top, w, y+h-top], slot]
    return [slot]


def fit_captioned_image(slot, image_width, image_height, caption):
    x, y, w, h = slot
    caption_height = 12 + len(caption_lines(caption, w)) * CAPTION_FONT_SIZE * 1.4
    image_height_available = h - caption_height - 8
    if image_height_available <= 0:
        return None
    image_box = fit_image([x, y, w, image_height_available], image_width, image_height)
    caption_box = [x, image_box[1] + image_box[3] + 8, w, caption_height]
    return image_box, caption_box


def points(value):
    unit = value.get("unit", "PT")
    if unit not in ("PT", "EMU"):
        raise ValueError(f"Unsupported slide dimension unit: {unit}")
    return float(value.get("magnitude", 0)) / (12700 if unit == "EMU" else 1)


def text_of(element):
    def runs(container):
        return "".join(row.get("textRun", {}).get("content", "")
                       for row in container.get("textElements", []))
    if "shape" in element:
        return runs(element["shape"].get("text", {})).strip()
    if "table" in element:
        return "\n".join(" | ".join(runs(cell.get("text", {})).strip()
                                    for cell in row.get("tableCells", []))
                         for row in element["table"].get("tableRows", [])).strip()
    return ""


def blocks_on(page):
    blocks = []
    def visit(elements):
        for element in elements:
            text = text_of(element)
            if text:
                blocks.append({"id": element["objectId"], "text": text})
            visit(element.get("elementGroup", {}).get("children", []))
    visit(page.get("pageElements", []))
    return blocks


def slide_geometry(deck, slide, with_ids=False):
    """Resolve placeholder inheritance and affine group transforms before boxing."""
    pages = {p["objectId"]: p for key in ("slides", "layouts", "masters") for p in deck.get(key, [])}
    elements = {}
    def index(rows):
        for row in rows:
            elements[row["objectId"]] = row
            index(row.get("elementGroup", {}).get("children", []))
    for page in pages.values():
        index(page.get("pageElements", []))

    def inherited(element, key, seen=None):
        if key in element:
            return element[key]
        seen = set() if seen is None else seen
        parent = element.get("shape", {}).get("placeholder", {}).get("parentObjectId")
        if parent in elements and parent not in seen:
            return inherited(elements[parent], key, seen | {parent})
        raise ValueError(f"Cannot resolve {key} for slide element {element['objectId']}")

    def matrix(t):
        factor = points({"magnitude": 1, "unit": t.get("unit", "PT")})
        return (t.get("scaleX", 0), t.get("shearY", 0), t.get("shearX", 0),
                t.get("scaleY", 0), t.get("translateX", 0) * factor, t.get("translateY", 0) * factor)

    def compose(a, b):
        return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
                a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
                a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])

    boxes = []
    boxes_by_id = {}
    def visit(rows, parent=(1, 0, 0, 1, 0, 0), inherited_page=False):
        for element in rows:
            if inherited_page and element.get("shape", {}).get("placeholder"):
                continue
            transform = compose(parent, matrix(inherited(element, "transform")))
            children = element.get("elementGroup", {}).get("children")
            if children is not None:
                visit(children, transform, inherited_page)
                continue
            size = inherited(element, "size")
            w, h = points(size["width"]), points(size["height"])
            a, b, c, d, x, y = transform
            corners = [(a*u+c*v+x, b*u+d*v+y) for u, v in ((0, 0), (w, 0), (0, h), (w, h))]
            xs, ys = zip(*corners)
            box = [min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)]
            if not all(math.isfinite(n) for n in box):
                raise ValueError("Invalid slide geometry.")
            boxes.append(box)
            boxes_by_id[element["objectId"]] = box
    visit(slide.get("pageElements", []))
    props = slide.get("slideProperties", {})
    for page_id in {props.get("layoutObjectId"), props.get("masterObjectId")} - {None}:
        visit(pages.get(page_id, {}).get("pageElements", []), inherited_page=True)
    return boxes_by_id if with_ids else boxes


def overlaps(a, b, gap=6):
    return (a[0] < b[0]+b[2]+gap and a[0]+a[2]+gap > b[0]
            and a[1] < b[1]+b[3]+gap and a[1]+a[3]+gap > b[1])


def free_box(width, height, occupied):
    """Largest blank rectangle on a coarse grid, with readable minimum dimensions."""
    cols, rows = 32, 18
    cw, ch = width / cols, height / rows
    grid = [[not any(overlaps([x*cw, y*ch, cw, ch], box) for box in occupied)
             and x > 0 and y > 0 and x < cols-1 and y < rows-1
             for x in range(cols)] for y in range(rows)]
    best, area = None, 0
    for top in range(rows):
        clear = [True] * cols
        for bottom in range(top, rows):
            clear = [a and b for a, b in zip(clear, grid[bottom])]
            start = 0
            for end in range(cols + 1):
                if end < cols and clear[end]:
                    continue
                w, h = (end-start)*cw, (bottom-top+1)*ch
                if w >= width*.22 and h >= height*.25 and w*h > area:
                    best, area = [start*cw, top*ch, w, h], w*h
                start = end + 1
    return best


def fit_image(box, width, height):
    x, y, w, h = box
    factor = min(w / width, h / height)
    iw, ih = width * factor, height * factor
    return [x+(w-iw)/2, y+(h-ih)/2, iw, ih]
