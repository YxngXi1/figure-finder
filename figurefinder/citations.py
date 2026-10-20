"""Source credits built from retrieved metadata, never invented by the model."""

from html.parser import HTMLParser
from urllib.parse import urlparse


def plain_text(value):
    class TextOnly(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = TextOnly()
    parser.feed(str(value or ""))
    return " ".join(" ".join(parser.parts).split())


def source_url(figure):
    for value in (figure.get("source_page"), figure.get("url")):
        if not isinstance(value, str):
            continue
        parsed = urlparse(value)
        if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username:
            return value
    raise ValueError("An image citation needs a valid source page or image URL.")


def make_citation(figure):
    url = source_url(figure)
    host = urlparse(url).hostname.removeprefix("www.")
    # Keep the complete metadata in the plan and image alt text. The slide credit
    # uses a short title, full creator/custom attribution, host, and license.
    title = plain_text(figure.get("title"))
    if len(title) > 65:
        title = title[:62].rstrip() + "…"
    creator = plain_text(figure.get("attribution") or figure.get("creator"))
    license_name = plain_text(figure.get("license"))
    parts = [part for part in (creator, title, host, license_name) if part]
    text = "Source: " + " · ".join(parts)
    return {"text": text, "url": url}
