"""Diagnostic web interface for figure-finder.

This is deliberately a thin wrapper around the CLI. Each request runs the
normal command in a separate process and gets its own output directory, so the
web version cannot silently drift from the command-line pipeline while the UI
is still being evaluated.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import hmac
import json
import mimetypes
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, cli, config, llm


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("FF_WEB_OUTPUT_DIR", "./figurefinder_web_out"))
MAX_FORM_BYTES = 64 * 1024
MAX_NOTES_LENGTH = 8_000
MAX_LOG_LINES = 5_000
JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SESSION_COOKIE = "figure_finder_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_ATTEMPT_LIMIT = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_url(value: str) -> str:
    parsed = urlparse(value or "")
    return value if parsed.scheme in {"http", "https"} else ""


def _slug(value: str, default: str = "figure") -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value[:80] or default)


def _json_file(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def selected_result(payload: Optional[dict]) -> Optional[dict]:
    """Return an eligible judge winner, or the first eligible result."""
    if not payload:
        return None
    results = payload.get("results") or []
    winner_id = (payload.get("pick") or {}).get("winner_id")
    return next((item for item in results if item.get("id") == winner_id), None) or (
        results[0] if results else None
    )


@dataclass
class WebJob:
    id: str
    request: dict
    output_dir: str
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    status: str = "queued"
    exit_code: Optional[int] = None
    elapsed_seconds: Optional[float] = None
    command: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def directory(self) -> Path:
        return Path(self.output_dir)

    @property
    def results_path(self) -> Path:
        return self.directory / "results.json"

    def result_payload(self) -> Optional[dict]:
        return _json_file(self.results_path)

    def diagnostic_payload(self, include_logs: bool = True) -> dict:
        value = asdict(self)
        if not include_logs:
            value.pop("logs", None)
        payload = self.result_payload()
        value["result_count"] = len((payload or {}).get("results") or [])
        value["rejected_count"] = len((payload or {}).get("rejected_results") or [])
        return value


class AppState:
    def __init__(self, output_root: Path, workers: int = 1, password: str = ""):
        self.output_root = output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, WebJob] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="figure-job")
        self.workers = workers
        self.password = password
        self.session_secret = secrets.token_bytes(32)
        self.secure_cookies = os.environ.get("FF_WEB_SECURE_COOKIES") == "1"
        self.login_failures: dict[str, list[float]] = {}
        self.result_cache: dict[str, str] = {}

    @staticmethod
    def _request_key(request: dict) -> str:
        return hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def add_job(self, request: dict) -> WebJob:
        request_key = self._request_key(request)
        with self.lock:
            cached_id = self.result_cache.get(request_key)
            cached = self.jobs.get(cached_id or "")
            if cached and cached.status == "complete" and cached.results_path.is_file():
                return cached
        job_id = uuid.uuid4().hex
        job = WebJob(
            id=job_id,
            request=request,
            output_dir=str(self.output_root / job_id),
        )
        with self.lock:
            self.jobs[job_id] = job
        self._write_manifest(job)
        self.executor.submit(self._run_job, job)
        return job

    def create_session(self) -> str:
        expires = str(int(time.time()) + SESSION_TTL_SECONDS)
        unsigned = f"{expires}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(
            self.session_secret, unsigned.encode(), hashlib.sha256
        ).hexdigest()
        return f"{unsigned}.{signature}"

    def valid_session(self, token: str) -> bool:
        try:
            expires, nonce, signature = token.split(".", 2)
            if int(expires) < int(time.time()) or not nonce:
                return False
        except (TypeError, ValueError):
            return False
        unsigned = f"{expires}.{nonce}"
        expected = hmac.new(
            self.session_secret, unsigned.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def csrf_token(self, session_token: str) -> str:
        return hmac.new(
            self.session_secret, f"csrf:{session_token}".encode(), hashlib.sha256
        ).hexdigest()

    def valid_csrf(self, session_token: str, submitted: str) -> bool:
        return bool(submitted) and hmac.compare_digest(
            submitted, self.csrf_token(session_token)
        )

    def login_allowed(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            attempts = [
                when for when in self.login_failures.get(client, [])
                if now - when < LOGIN_WINDOW_SECONDS
            ]
            self.login_failures[client] = attempts
            return len(attempts) < LOGIN_ATTEMPT_LIMIT

    def login_failed(self, client: str) -> None:
        with self.lock:
            self.login_failures.setdefault(client, []).append(time.monotonic())

    def login_succeeded(self, client: str) -> None:
        with self.lock:
            self.login_failures.pop(client, None)

    def get_job(self, job_id: str) -> Optional[WebJob]:
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self.lock:
            return self.jobs.get(job_id)

    def recent_jobs(self) -> list[WebJob]:
        with self.lock:
            return list(reversed(list(self.jobs.values())))[:20]

    def _append_log(self, job: WebJob, line: str) -> None:
        with self.lock:
            job.logs.append(line.rstrip("\r\n"))
            if len(job.logs) > MAX_LOG_LINES:
                job.logs[:] = ["[older diagnostic output truncated]"] + job.logs[-MAX_LOG_LINES + 1 :]

    def _write_manifest(self, job: WebJob) -> None:
        job.directory.mkdir(parents=True, exist_ok=True)
        manifest = job.diagnostic_payload(include_logs=True)
        manifest["runtime"] = runtime_diagnostics()
        (job.directory / "web-job.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def _run_job(self, job: WebJob) -> None:
        started = time.monotonic()
        with self.lock:
            job.status = "running"
            job.started_at = _now()
            job.command = build_command(job.request, job.directory)
        self._write_manifest(job)
        try:
            child_env = os.environ.copy()
            child_env.setdefault("FF_EXPORT_MAX_EDGE", "1920")
            child_env.setdefault("FF_SCORE_WORKERS", "2")
            child_env.pop("FF_WEB_PASSWORD", None)
            process = subprocess.Popen(
                job.command,
                cwd=REPO_ROOT,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self._append_log(job, line)
            exit_code = process.wait()
            payload = job.result_payload()
            with self.lock:
                job.exit_code = exit_code
                if payload and payload.get("results"):
                    job.status = "complete"
                elif payload:
                    job.status = "no_result"
                    job.error = "The run completed, but no candidate passed the quality checks."
                else:
                    job.status = "failed"
                    job.error = f"The pipeline exited with status {exit_code} before writing results."
        except Exception as exc:  # diagnostics are intentionally retained in this first version
            self._append_log(job, traceback.format_exc())
            with self.lock:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
        finally:
            with self.lock:
                job.finished_at = _now()
                job.elapsed_seconds = round(time.monotonic() - started, 2)
                if job.status == "complete":
                    self.result_cache[self._request_key(job.request)] = job.id
            self._write_manifest(job)


def build_command(request: dict, output_dir: Path) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "figurefinder.cli",
        request["notes"],
        "--audience",
        request["audience"],
        "--style",
        request["style"],
        "--language",
        request["language"],
        "--sources",
        "brave",
        "--queries",
        str(request["queries"]),
        "--max-scored",
        str(request["max_scored"]),
        "--max-fetched",
        str(request["max_fetched"]),
        "--top",
        str(request["top"]),
        "--out",
        str(output_dir),
    ]
    if request.get("no_judge"):
        command.append("--no-judge")
    return command


def runtime_diagnostics() -> dict:
    provider = os.environ.get("FF_PROVIDER", config.PROVIDER).lower()
    try:
        models = llm.models_for(provider)
    except Exception:
        models = {}
    return {
        "app_version": __version__,
        "python": sys.version.split()[0],
        "provider": provider,
        "models": models,
        "credential_presence": {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "BRAVE_API_KEY": bool(os.environ.get("BRAVE_API_KEY")),
        },
    }


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _layout(title: str, content: str, refresh: bool = False, csrf: str = "") -> bytes:
    refresh_tag = '<meta http-equiv="refresh" content="2">' if refresh else ""
    logout = (
        f'<form method="post" action="/logout" class="logout">'
        f'<input type="hidden" name="csrf" value="{_escape(csrf)}">'
        '<button type="submit" class="text-button">Sign out</button></form>'
        if csrf else ""
    )
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">{refresh_tag}
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)} · Figure Finder</title>
<style>
:root{{--ink:#17202a;--muted:#667085;--line:#e2e5e9;--paper:#fff;--wash:#f6f4ef;
--navy:#16324f;--blue:#2563eb;--blue-dark:#1d4ed8;--green:#087443;--amber:#a15c07;--red:#b42318}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);
font:16px/1.55 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{background:var(--navy);color:white;padding:17px 0;box-shadow:0 1px 3px #0002}}
header .wrap{{display:flex;align-items:center;justify-content:space-between}} header a{{color:white;text-decoration:none}}
.brand{{font-size:18px;font-weight:800;letter-spacing:-.02em}} .wrap{{max-width:1040px;margin:auto;padding:0 24px}}
main.wrap{{padding-top:42px;padding-bottom:72px}} h1{{font-size:clamp(30px,5vw,48px);letter-spacing:-.035em;line-height:1.08;margin:0 0 12px}}
h2{{font-size:22px;letter-spacing:-.015em;margin:0 0 14px}} h3{{font-size:16px;margin:18px 0 7px}}
p{{margin:8px 0}} .lead{{font-size:18px;color:var(--muted);max-width:680px}} .muted{{color:var(--muted)}}
.eyebrow{{color:var(--blue);font-size:13px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;margin-bottom:9px}}
.card{{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:26px;margin:24px 0;box-shadow:0 8px 28px #17202a0a}}
.search-card{{padding:32px}} label{{display:block;font-weight:700;margin:16px 0 6px}}
input[type=text],input[type=password],input[type=number],textarea{{width:100%;border:1px solid #b8c2cf;border-radius:9px;
padding:12px 14px;font:inherit;background:white;color:var(--ink)}} textarea{{min-height:145px;resize:vertical;font-size:17px}}
input:focus,textarea:focus{{outline:3px solid #bfdbfe;border-color:var(--blue)}} .fields{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 18px}}
button,.button{{display:inline-block;background:var(--blue);color:white;border:0;border-radius:9px;padding:11px 17px;
font:inherit;font-weight:800;text-decoration:none;cursor:pointer}} button:hover,.button:hover{{background:var(--blue-dark)}}
.button.download{{background:var(--green)}} .text-button{{background:transparent;padding:4px;color:#dce7f2;font-weight:600}}
.logout{{margin:0}} .notice{{border-left:4px solid var(--amber);background:#fff8e8;padding:13px 16px;border-radius:7px;margin:18px 0}}
.error{{border-color:#f2b8b5;background:#fff1f0;color:var(--red)}} .status{{display:inline-block;border-radius:99px;padding:4px 11px;font-size:13px;font-weight:800;background:#e8edf3}}
.status.complete{{background:#d1fadf;color:var(--green)}} .status.failed,.status.no_result{{background:#fee4e2;color:var(--red)}}
.hero-image{{display:block;max-width:100%;max-height:650px;margin:20px auto;border-radius:10px;box-shadow:0 4px 18px #0002}}
.actions{{display:flex;align-items:center;flex-wrap:wrap;gap:14px;margin-top:18px}} .score{{font-size:34px;font-weight:850;letter-spacing:-.03em}}
.options{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}} .option{{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:white}}
.option.chosen{{border:2px solid var(--blue)}} .option img{{width:100%;height:170px;object-fit:contain;background:#f8fafc;display:block}}
.option-body{{padding:14px}} .option-score{{font-size:22px;font-weight:850}} .criteria{{font-size:12px;color:var(--muted);margin-top:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}} th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{color:var(--muted)}} pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#101828;color:#d0d5dd;padding:14px;border-radius:8px;max-height:520px;overflow:auto;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}
details{{border-top:1px solid var(--line);padding:14px 0}} details:first-child{{border-top:0}} summary{{cursor:pointer;font-weight:750}}
.advanced{{margin-top:18px}} .kv{{display:grid;grid-template-columns:180px 1fr;gap:5px 12px}} a{{color:#175cd3}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}} .login-shell{{max-width:430px;margin:8vh auto}}
.login-shell h1{{font-size:34px}} .center{{text-align:center}}
@media(max-width:760px){{.fields,.options{{grid-template-columns:1fr}}.kv{{grid-template-columns:1fr}}.wrap{{padding-left:16px;padding-right:16px}}
main.wrap{{padding-top:28px}}.search-card{{padding:22px}}.option img{{height:210px}}}}
</style></head><body>
<header><div class="wrap"><a class="brand" href="/">Figure Finder</a>{logout}</div></header>
<main class="wrap">{content}</main></body></html>"""
    return page.encode("utf-8")


def render_login(message: str = "") -> bytes:
    error = f'<div class="notice error">{_escape(message)}</div>' if message else ""
    content = f"""<section class="login-shell"><div class="card">
<div class="eyebrow">Private access</div><h1>Welcome</h1>
<p class="muted">Enter the shared password to use Figure Finder.</p>{error}
<form method="post" action="/login"><label for="password">Password</label>
<input id="password" name="password" type="password" required autofocus autocomplete="current-password">
<p style="margin-top:20px"><button type="submit">Sign in</button></p></form></div></section>"""
    return _layout("Sign in", content)


def _environment_html() -> str:
    diag = runtime_diagnostics()
    keys = diag["credential_presence"]
    key_rows = "".join(
        f"<tr><td><code>{_escape(name)}</code></td><td>{'present' if present else 'missing'}</td></tr>"
        for name, present in keys.items()
    )
    model_rows = "".join(
        f"<tr><td>{_escape(role)}</td><td><code>{_escape(model)}</code></td></tr>"
        for role, model in diag["models"].items()
    )
    return f"""<details><summary>Runtime and credential diagnostics</summary>
<div class="grid"><div><h3>Runtime</h3><table>
<tr><td>App</td><td>{_escape(diag['app_version'])}</td></tr>
<tr><td>Python</td><td>{_escape(diag['python'])}</td></tr>
<tr><td>Provider</td><td>{_escape(diag['provider'])}</td></tr>{model_rows}</table></div>
<div><h3>Credentials (values are never displayed)</h3><table>{key_rows}</table></div></div></details>"""


def render_home(state: AppState, csrf: str, message: str = "") -> bytes:
    error = f'<div class="notice">{_escape(message)}</div>' if message else ""
    content = f"""
<div class="eyebrow">Teaching visuals, without the tab hunt</div>
<h1>Find the right image for your slide</h1>
<p class="lead">Describe what students need to see. Figure Finder searches, checks, ranks, and prepares the strongest result for download.</p>{error}
<section class="card search-card">
<form method="post" action="/jobs">
<input type="hidden" name="csrf" value="{_escape(csrf)}">
<label for="notes">What should students understand from the image?</label>
<textarea id="notes" name="notes" required maxlength="{MAX_NOTES_LENGTH}" placeholder="Animal cell structure; label the nucleus, cell membrane, cytoplasm and mitochondria"></textarea>
<details class="advanced"><summary>Optional search settings</summary>
<div class="fields"><div><label for="audience">Audience</label><input id="audience" name="audience" type="text" value="undergraduate students" maxlength="200"></div>
<div><label for="language">Label language</label><input id="language" name="language" type="text" value="English" maxlength="80"></div></div>
<label for="style">Visual style</label><input id="style" name="style" type="text" value="clean labelled textbook diagram" maxlength="200">
<div class="fields"><div><label for="max_scored">Images to evaluate</label><input id="max_scored" name="max_scored" type="number" min="1" max="24" value="8"></div>
<div><label for="top">Options to show</label><input id="top" name="top" type="number" min="1" max="12" value="5"></div></div>
<label><input type="checkbox" name="run_judge" value="1"> Run an extra comparison of the finalists <span class="muted">(slower)</span></label>
</details>
<div class="actions"><button type="submit">Find my image</button><span class="muted">Powered by Brave Image Search</span></div>
</form></section>"""
    return _layout("Find an image", content, csrf=csrf)


def _candidate_rows(items: list[dict], rejected: bool = False) -> str:
    rows = []
    for item in items:
        source = _safe_url(item.get("page_url") or item.get("image_url") or "")
        source_html = f'<a href="{_escape(source)}" rel="noreferrer">source</a>' if source else "—"
        problems = item.get("rejected") if rejected else ", ".join(item.get("elements_missing") or [])
        raised = [name for name, value in (item.get("flags") or {}).items() if value]
        if raised:
            problems = ", ".join(filter(None, [str(problems or ""), *raised]))
        rows.append(
            "<tr>"
            f"<td><code>{_escape(item.get('id', ''))}</code><br>{source_html}</td>"
            f"<td>{_escape(item.get('final_score', '—'))}</td>"
            f"<td>{_escape(item.get('real_width', 0))}×{_escape(item.get('real_height', 0))}</td>"
            f"<td>{_escape(item.get('host', ''))}<br>{_escape(item.get('license', ''))}</td>"
            f"<td>{_escape(item.get('verdict', ''))}</td>"
            f"<td>{_escape(problems or '—')}</td></tr>"
        )
    return "".join(rows) or '<tr><td colspan="6" class="muted">None.</td></tr>'


def _candidate_cards(job: WebJob, items: list[dict], chosen_id: str) -> str:
    cards = []
    for item in items[: job.request.get("top", 5)]:
        candidate_id = str(item.get("id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", candidate_id):
            continue
        source = _safe_url(item.get("page_url") or item.get("image_url") or "")
        source_link = (
            f'<a href="{_escape(source)}" rel="noreferrer">Open source</a>'
            if source else "Source unavailable"
        )
        criteria = " · ".join(
            f"{name.replace('_', ' ')} {score}"
            for name, score in (item.get("scores") or {}).items()
        )
        chosen = " chosen" if candidate_id == chosen_id else ""
        label = '<span class="status complete">Selected</span>' if chosen else ""
        cards.append(f"""<article class="option{chosen}">
<img src="/jobs/{job.id}/images/{_escape(candidate_id)}" alt="{_escape(item.get('verdict') or 'Candidate figure')}">
<div class="option-body"><div class="option-score">{_escape(item.get('final_score', '—'))}<span class="muted" style="font-size:13px"> / 100</span> {label}</div>
<p>{_escape(item.get('verdict', ''))}</p><p>{source_link}</p>
<div class="criteria">{_escape(criteria)}</div></div></article>""")
    return "".join(cards) or '<p class="muted">No other suitable options were found.</p>'


def render_job(job: WebJob, csrf: str) -> bytes:
    payload = job.result_payload()
    selected = selected_result(payload)
    refresh = job.status in {"queued", "running"}
    result_block = ""
    if selected:
        source = _safe_url(selected.get("page_url") or selected.get("image_url") or "")
        source_link = f'<a href="{_escape(source)}" rel="noreferrer">View original source</a>' if source else ""
        concept = ((payload or {}).get("plan") or {}).get("concept", "Selected figure")
        result_block = f"""<section class="card"><div class="eyebrow">Recommended for your slide</div><h2>{_escape(concept)}</h2>
<div class="score">{_escape(selected.get('final_score', '—'))}<span class="muted" style="font-size:14px"> / 100</span></div>
<img class="hero-image" src="/jobs/{job.id}/image" alt="{_escape(selected.get('verdict') or 'Selected figure')}">
<p>{_escape(selected.get('verdict', ''))}</p><p>
<a class="button download" href="/jobs/{job.id}/download">Download image</a>
{source_link}</p><p class="muted">{_escape(selected.get('real_width', 0))}×{_escape(selected.get('real_height', 0))} ·
{_escape(selected.get('host', ''))} · {_escape(selected.get('license', '') or 'license not reported')}</p></section>"""
    elif job.status in {"no_result", "failed"}:
        result_block = f'<div class="notice"><strong>No downloadable figure.</strong> {_escape(job.error)}</div>'
    else:
        result_block = '<section class="card center"><div class="eyebrow">Searching and checking</div><h2>Finding your strongest options…</h2><p class="muted">The page updates automatically.</p></section>'

    plan = (payload or {}).get("plan") or {}
    must_show = "".join(f"<li>{_escape(item)}</li>" for item in plan.get("must_show", [])) or "<li>Not available yet.</li>"
    accepted = (payload or {}).get("results") or []
    rejected = (payload or {}).get("rejected_results") or []
    command = shlex.join(job.command) if job.command else "Waiting to start"
    logs = "\n".join(job.logs) or "Waiting for pipeline output…"
    raw_json = json.dumps(payload, indent=2) if payload else "Results have not been written yet."
    chosen_id = str((selected or {}).get("id", ""))
    options = ""
    if accepted:
        options = f"""<section class="card"><h2>Sources and other strong options</h2>
<p class="muted">Compare the scores and open the original source before using an image publicly.</p>
<div class="options">{_candidate_cards(job, accepted, chosen_id)}</div></section>"""
    diagnostics = f"""<section class="card"><details><summary>Technical diagnostics</summary>
<div class="kv"><strong>Status</strong><span><span class="status {_escape(job.status)}">{_escape(job.status)}</span></span>
<strong>Created</strong><span>{_escape(job.created_at)}</span><strong>Started</strong><span>{_escape(job.started_at or '—')}</span>
<strong>Finished</strong><span>{_escape(job.finished_at or '—')}</span><strong>Elapsed</strong><span>{_escape(job.elapsed_seconds if job.elapsed_seconds is not None else '—')} seconds</span>
<strong>Exit code</strong><span>{_escape(job.exit_code if job.exit_code is not None else '—')}</span>
<strong>Search</strong><span>Brave · {_escape(job.request.get('queries'))} parallel queries</span>
<strong>Candidate cap</strong><span>{_escape(job.request.get('max_fetched'))} downloads · {_escape(job.request.get('max_scored'))} graded</span></div>
<details><summary>Generated plan</summary><h3>{_escape(plan.get('concept', 'Not available yet'))}</h3>
<p>{_escape(plan.get('figure_brief', ''))}</p><ul>{must_show}</ul></details>
<details><summary>Rejected candidates ({len(rejected)})</summary><table><thead><tr><th>ID/source</th><th>Score</th><th>Pixels</th><th>Host/license</th><th>Verdict</th><th>Reason/flags</th></tr></thead><tbody>{_candidate_rows(rejected, True)}</tbody></table></details>
<details><summary>Raw pipeline output</summary><pre>{_escape(logs)}</pre></details>
<details><summary>Exact command (contains no API keys)</summary><pre>{_escape(command)}</pre></details>
<details><summary>Raw results JSON</summary><pre>{_escape(raw_json)}</pre></details>
{_environment_html()}</details></section>"""
    content = f"""<p><a href="/">← Start a new search</a></p><h1 style="font-size:32px">{_escape(job.request.get('notes', 'Figure search'))}</h1>
<p><span class="status {_escape(job.status)}">{_escape(job.status)}</span></p>{result_block}{options}{diagnostics}"""
    return _layout("Figure results", content, refresh=refresh, csrf=csrf)


def _parse_job_request(body: bytes) -> tuple[Optional[dict], Optional[str]]:
    fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    notes = (fields.get("notes") or [""])[0].strip()
    if not notes:
        return None, "Describe the figure you need."
    if len(notes) > MAX_NOTES_LENGTH:
        return None, f"Notes must be {MAX_NOTES_LENGTH:,} characters or fewer."
    def text_field(name: str, default: str, maximum: int) -> str:
        value = (fields.get(name) or [default])[0].strip() or default
        return value[:maximum]

    try:
        max_scored = int((fields.get("max_scored") or ["8"])[0])
        top = int((fields.get("top") or ["5"])[0])
    except ValueError:
        return None, "Image and result counts must be whole numbers."
    if not 1 <= max_scored <= 24 or not 1 <= top <= 20:
        return None, "Images to grade must be 1–24 and results to retain must be 1–20."
    return {
        "notes": notes,
        "audience": text_field("audience", "undergraduate students", 200),
        "style": text_field("style", "clean labelled textbook diagram", 200),
        "language": text_field("language", "English", 80),
        "sources": ["brave"],
        "queries": 3,
        "max_fetched": max(24, max_scored),
        "max_scored": max_scored,
        "top": top,
        "no_judge": (fields.get("run_judge") or [""])[0] != "1",
    }, None


class DiagnosticHandler(BaseHTTPRequestHandler):
    state: AppState
    server_version = "FigureFinderDiagnostic/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("web: " + fmt % args + "\n")

    def _session_token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(SESSION_COOKIE)
            return morsel.value if morsel else ""
        except Exception:
            return ""

    def _authenticated_token(self) -> str:
        token = self._session_token()
        return token if self.state.valid_session(token) else ""

    def _read_form_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_FORM_BYTES:
            return None
        return self.rfile.read(length)

    @staticmethod
    def _field(body: bytes, name: str) -> str:
        return (parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
                .get(name) or [""])[0]

    def _session_cookie(self, token: str, clear: bool = False) -> str:
        parts = [
            f"{SESSION_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
            f"Max-Age={0 if clear else SESSION_TTL_SECONDS}",
        ]
        if self.state.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def _send(self, body: bytes, status: int = HTTPStatus.OK, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path == "/login":
            if self._authenticated_token():
                self._redirect("/")
            else:
                self._send(render_login())
            return
        session_token = self._authenticated_token()
        if not session_token:
            self._redirect("/login")
            return
        csrf = self.state.csrf_token(session_token)
        if path == "/":
            self._send(render_home(self.state, csrf))
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "jobs":
            job = self.state.get_job(parts[1])
            if not job:
                self._send(_layout("Not found", "<h1>Job not found</h1>"), HTTPStatus.NOT_FOUND)
                return
            if len(parts) == 2:
                self._send(render_job(job, csrf))
                return
            if len(parts) == 3 and parts[2] == "status.json":
                data = json.dumps(job.diagnostic_payload()).encode("utf-8")
                self._send(data, content_type="application/json; charset=utf-8")
                return
            if len(parts) == 3 and parts[2] in {"image", "download"}:
                item = selected_result(job.result_payload())
                self._serve_candidate_image(
                    job, str((item or {}).get("id", "")),
                    attachment=parts[2] == "download",
                )
                return
            if len(parts) == 4 and parts[2] == "images":
                self._serve_candidate_image(job, parts[3], attachment=False)
                return
        self._send(_layout("Not found", "<h1>Page not found</h1>"), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        body = self._read_form_body()
        if body is None:
            self._send(_layout("Invalid request", "<h1>The submitted form was empty or too large.</h1>"),
                       HTTPStatus.BAD_REQUEST)
            return

        if path == "/login":
            client = self.client_address[0]
            if not self.state.login_allowed(client):
                self._send(render_login("Too many attempts. Try again in five minutes."),
                           HTTPStatus.TOO_MANY_REQUESTS)
                return
            submitted = self._field(body, "password")
            if not hmac.compare_digest(submitted.encode(), self.state.password.encode()):
                self.state.login_failed(client)
                self._send(render_login("That password was not accepted."), HTTPStatus.UNAUTHORIZED)
                return
            self.state.login_succeeded(client)
            token = self.state.create_session()
            self._redirect("/", self._session_cookie(token))
            return

        session_token = self._authenticated_token()
        if not session_token:
            self._redirect("/login")
            return
        if not self.state.valid_csrf(session_token, self._field(body, "csrf")):
            self._send(_layout("Forbidden", "<h1>Your session form expired. Refresh and try again.</h1>"),
                       HTTPStatus.FORBIDDEN)
            return
        if path == "/logout":
            self._redirect("/login", self._session_cookie("", clear=True))
            return
        if path != "/jobs":
            self._send(_layout("Not found", "<h1>Page not found</h1>"), HTTPStatus.NOT_FOUND)
            return

        request, error = _parse_job_request(body)
        if error:
            csrf = self.state.csrf_token(session_token)
            self._send(render_home(self.state, csrf, error), HTTPStatus.BAD_REQUEST)
            return
        job = self.state.add_job(request or {})
        self._redirect(f"/jobs/{job.id}")

    def _serve_candidate_image(self, job: WebJob, candidate_id: str, attachment: bool) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", candidate_id):
            self._send(b"Selected image is unavailable.", HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            return
        eligible_ids = {
            str(item.get("id", "")) for item in (job.result_payload() or {}).get("results", [])
        }
        if candidate_id not in eligible_ids:
            self._send(b"Selected image is unavailable.", HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            return
        export_path = job.directory / "images" / f"{candidate_id}.download.jpg"
        path = (export_path if export_path.is_file() else
                job.directory / "images" / f"{candidate_id}.jpg").resolve()
        try:
            path.relative_to(job.directory.resolve())
        except ValueError:
            self._send(b"Invalid image path.", HTTPStatus.BAD_REQUEST, "text/plain; charset=utf-8")
            return
        if not path.is_file():
            self._send(b"Selected image is unavailable.", HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8")
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            concept = ((job.result_payload() or {}).get("plan") or {}).get("concept", "figure")
            self.send_header("Content-Disposition", f'attachment; filename="{_slug(concept)}.jpg"')
        self.end_headers()
        self.wfile.write(data)


def create_server(host: str, port: int, state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundDiagnosticHandler", (DiagnosticHandler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Figure Finder diagnostic web preview.")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="listen port (default: 8000)")
    parser.add_argument("--workers", type=int, default=1, help="simultaneous user jobs (default: 1)")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_ROOT), help="web job output directory")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    cli._load_dotenv(REPO_ROOT / ".env")
    cli._load_dotenv(REPO_ROOT / ".env.web")
    args = build_parser().parse_args(argv)
    if not 1 <= args.workers <= 8:
        print("--workers must be between 1 and 8", file=sys.stderr)
        return 2
    password = os.environ.get("FF_WEB_PASSWORD", "")
    if not password:
        print("FF_WEB_PASSWORD is missing. Add it to .env.web before starting the website.",
              file=sys.stderr)
        return 2
    state = AppState(Path(args.out), workers=args.workers, password=password)
    server = create_server(args.host, args.port, state)
    shown_host = "localhost" if args.host in {"127.0.0.1", "::1"} else args.host
    print(f"Figure Finder diagnostic web preview: http://{shown_host}:{server.server_port}")
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print("WARNING: use HTTPS before sending the shared password over a network.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web server.")
    finally:
        server.server_close()
        state.executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
