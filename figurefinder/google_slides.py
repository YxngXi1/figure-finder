"""Private desktop OAuth and a small Google Slides REST client."""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

SCOPES = ["https://www.googleapis.com/auth/presentations"]
API = "https://slides.googleapis.com/v1/presentations"


def presentation_id(value: str) -> str:
    match = re.fullmatch(r"https://docs\.google\.com/presentation/d/([\w-]+)(?:/[^\s]*)?", value)
    if match:
        return match[1]
    if re.fullmatch(r"[\w-]+", value):
        return value
    raise ValueError("Supply a Google Slides presentation URL or presentation ID.")


def authorize(credentials_file: str, token_file: str):
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Install Slides dependencies: .venv/bin/pip install -r requirements-slides.txt") from exc
    token = Path(token_file)
    credentials = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if credentials and not credentials.has_scopes(SCOPES):
        raise RuntimeError("Saved Google token lacks Slides edit access; use a new --token path.")
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not Path(credentials_file).is_file():
                raise RuntimeError("Google desktop OAuth credentials are missing. See docs/google-slides.md.")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            # Print the URL so the user controls the browser authorization step.
            credentials = flow.run_local_server(port=0, open_browser=False, timeout_seconds=180)
        token.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(credentials.to_json())
        token.chmod(0o600)
    return AuthorizedSession(credentials)


class SlidesClient:
    def __init__(self, session):
        self.session = session

    def _request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=60, **kwargs)
        if not response.ok:
            # Do not echo request bodies, signed URLs, or tokens on failure.
            raise RuntimeError(
                f"Google Slides returned HTTP {response.status_code}. "
                "Check account access/API setup; if the deck changed, create a new plan. "
                "For image insertion, check that the source URL is publicly fetchable."
            )
        return response.json()

    def get(self, deck_id):
        return self._request("GET", f"{API}/{deck_id}")

    def screenshot(self, deck_id, slide_id, destination):
        result = self._request("GET", f"{API}/{deck_id}/pages/{slide_id}/thumbnail",
                               params={"thumbnailProperties.thumbnailSize": "LARGE"})
        # A thumbnail URL already conveys access; never forward OAuth headers to it.
        with requests.get(result["contentUrl"], timeout=30) as response:
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as image:
                image.convert("RGB").save(destination, "JPEG", quality=90)

    def update(self, deck_id, revision_id, requests_list):
        return self._request("POST", f"{API}/{deck_id}:batchUpdate", json={
            "writeControl": {"requiredRevisionId": revision_id},
            "requests": requests_list,
        })


def validate_image_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or len(url.encode()) > 2048:
        raise ValueError("Slides requires an HTTP(S) image URL no longer than 2 KB.")


def check_remote_image(url: str) -> None:
    """Verify the actual remote bytes, not the locally converted preview."""
    validate_image_url(url)
    with requests.get(url, timeout=30, stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) >= 20 * 1024 * 1024:
                raise ValueError("Source image exceeds this tool's 20 MB download limit.")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ("PNG", "JPEG", "GIF") or image.width * image.height > 25_000_000:
            raise ValueError("Slides needs PNG, JPEG, or GIF with at most 25 megapixels.")
        image.verify()
