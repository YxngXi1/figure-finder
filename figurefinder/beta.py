"""Password-gated, single-process Slides beta for a free Python web host.

Google tokens live only in memory and a worker's stdin. No desktop token file
is used. Free hosting is intentionally ephemeral: restart loses job history.
"""

from __future__ import annotations

from collections import deque
from datetime import timedelta
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

SCOPE = "https://www.googleapis.com/auth/drive.file"
ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"starting", "running"}


def verify_google(token, deck_id, client_id, minimum_lifetime):
    """Validate token audience, scope, expiry and selected-file edit permission."""
    try:
        response = requests.get("https://oauth2.googleapis.com/tokeninfo",
                                params={"access_token": token}, timeout=15)
        response.raise_for_status()
        info = response.json()
        if (info.get("aud") != client_id or SCOPE not in info.get("scope", "").split()
                or int(info.get("expires_in", 0)) < minimum_lifetime):
            raise ValueError("Choose your presentation again to renew Google access.")
        response = requests.get(f"https://www.googleapis.com/drive/v3/files/{deck_id}",
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
            params={"fields": "id,name,mimeType,capabilities(canEdit)", "supportsAllDrives": "true"})
        response.raise_for_status()
        file = response.json()
        if (file.get("mimeType") != "application/vnd.google-apps.presentation"
                or not file.get("capabilities", {}).get("canEdit")):
            raise ValueError("Choose a Google Slides presentation that you can edit.")
        return file.get("name", "Presentation")[:250]
    except (requests.RequestException, KeyError, TypeError):
        # Never return Google's request URL, bearer token or response body.
        raise ValueError("Google access could not be verified. Choose the presentation again.") from None


def worker_environment():
    """Do not pass the website password/session key or desktop credentials to jobs."""
    allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
               "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "OPENAI_API_KEY", "BRAVE_API_KEY",
               "FF_PLANNER_MODEL", "FF_SCORER_MODEL", "FF_JUDGE_MODEL", "FF_API_USER_AGENT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1", FF_SCORE_WORKERS="1",
               FF_SEARCH_WORKERS="2", FF_DOWNLOAD_WORKERS="2", FF_HOSTED_WORKER="1")
    return env


class Jobs:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.rows = {}
        self.starts = deque()
        self.login_attempts = deque()
        self.processes = {}

    def prune(self):
        now = time.time()
        self.rows = {key: row for key, row in self.rows.items()
                     if row["status"] in ACTIVE or now - row["created"] < 6 * 3600}
        while self.starts and self.starts[0] < now - 86400:
            self.starts.popleft()

    def start(self, owner, token, deck_id, title, figures):
        with self.lock:
            self.prune()
            if any(row["status"] in ACTIVE for row in self.rows.values()):
                abort(409, "Someone is running a presentation. Please try again shortly.")
            if len(self.starts) >= self.app.config["DAILY_JOBS"]:
                abort(429, "The beta has reached its daily run limit. Please try tomorrow.")
            job_id = secrets.token_urlsafe(24)
            self.rows[job_id] = dict(id=job_id, owner=owner, created=time.time(),
                status="starting", title=title, deck_id=deck_id, progress="Starting…")
            self.starts.append(time.time())
            threading.Thread(target=self.run, args=(job_id, token, figures), daemon=True).start()
            return job_id

    def run(self, job_id, token, figures):
        process = None
        timer = None
        timed_out = threading.Event()
        try:
            with tempfile.TemporaryDirectory(prefix="figurefinder-beta-") as work:
                with self.lock:
                    row = self.rows[job_id]
                    row["status"] = "running"
                    payload = dict(token=token, deck_id=row["deck_id"], figures_per_slide=figures,
                                   max_slides=self.app.config["MAX_SLIDES"])
                process = subprocess.Popen([sys.executable, "-m", "figurefinder.beta_worker"],
                    cwd=work, env=worker_environment(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    start_new_session=True)
                with self.lock:
                    self.processes[job_id] = process
                def stop():
                    timed_out.set()
                    self.kill(process)
                timer = threading.Timer(self.app.config["JOB_SECONDS"], stop)
                timer.start()
                process.stdin.write(json.dumps(payload))
                process.stdin.close()
                del payload, token
                completed = False
                for line in process.stdout:
                    # Worker sends only bounded structured status, never raw provider errors.
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    with self.lock:
                        if event.get("type") == "progress":
                            row["progress"] = str(event.get("message", ""))[:500]
                        elif event.get("type") == "done":
                            row.update(status="done", result=event["result"], progress="Finished")
                            completed = True
                        elif event.get("type") == "error":
                            row.update(status="error", progress=str(event.get("message", "Run failed."))[:500])
                            completed = True
                process.wait()
                if not completed:
                    with self.lock:
                        row.update(status="error", progress=(
                            "This run reached the time limit. Check your presentation before retrying."
                            if timed_out.is_set() else
                            "The run was interrupted. Check your presentation before retrying."))
        except Exception:
            with self.lock:
                self.rows[job_id].update(status="error", progress="The run stopped unexpectedly. Check your presentation before retrying.")
        finally:
            if timer:
                timer.cancel()
            if process:
                self.kill(process)
                process.wait()
                if process.stdout:
                    process.stdout.close()
            with self.lock:
                self.processes.pop(job_id, None)

    @staticmethod
    def kill(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def create_app(settings=None):
    # Import after loading env so model configuration sees existing overrides.
    from .cli import _load_dotenv
    _load_dotenv()
    app = Flask(__name__, template_folder="beta_templates", static_folder="beta_static", static_url_path="/assets")
    app.config.update(
        SECRET_KEY=os.getenv("FF_BETA_SESSION_SECRET", ""),
        PASSWORD=os.getenv("FF_BETA_PASSWORD", ""),
        GOOGLE_CLIENT_ID=os.getenv("FF_GOOGLE_WEB_CLIENT_ID", ""),
        GOOGLE_PICKER_KEY=os.getenv("FF_GOOGLE_PICKER_KEY", ""),
        GOOGLE_PROJECT_NUMBER=os.getenv("FF_GOOGLE_PROJECT_NUMBER", ""),
        MAX_SLIDES=int(os.getenv("FF_BETA_MAX_SLIDES", "15")),
        MAX_FIGURES=int(os.getenv("FF_BETA_MAX_FIGURES_PER_SLIDE", os.getenv("FF_BETA_MAX_FIGURES", "3"))),
        DAILY_JOBS=int(os.getenv("FF_BETA_DAILY_JOBS", "10")),
        JOB_SECONDS=int(os.getenv("FF_BETA_JOB_SECONDS", "1200")),
        SESSION_COOKIE_NAME="ff_beta", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("FF_BETA_SECURE_COOKIES", "1") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=6), MAX_CONTENT_LENGTH=16384,
        SUPPORT_EMAIL=os.getenv("FF_SUPPORT_EMAIL", ""),
    )
    if settings:
        app.config.update(settings)
    if len(app.config["SECRET_KEY"]) < 32 or len(app.config["PASSWORD"]) < 12:
        raise ValueError("Set FF_BETA_SESSION_SECRET (32+ characters) and FF_BETA_PASSWORD (12+ characters).")
    for name, maximum in (("MAX_SLIDES", 50), ("MAX_FIGURES", 10), ("DAILY_JOBS", 100), ("JOB_SECONDS", 2400)):
        if not 1 <= app.config[name] <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}.")
    password_hash = generate_password_hash(app.config.pop("PASSWORD"))
    # Per-process auth epoch makes both password changes and server restarts sign everyone out.
    epoch = secrets.token_hex(16)
    jobs = Jobs(app)
    app.extensions["beta_jobs"] = jobs

    def authenticated():
        return session.get("epoch") == epoch and bool(session.get("owner"))

    @app.before_request
    def protect():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST":
            csrf = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf", "")
            if not hmac.compare_digest(csrf, session["csrf"]):
                abort(403, "Your session expired. Reload the page and try again.")
        if request.endpoint not in {"login", "health", "privacy", "static"} and not authenticated():
            if request.path.startswith("/api/"):
                abort(401, "Your session ended or the server restarted. Sign in again; check your deck before retrying a run.")
            return redirect("/login")

    @app.after_request
    def headers(response):
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin", "X-Frame-Options": "DENY",
            "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' https://accounts.google.com https://apis.google.com; style-src 'self' 'unsafe-inline' https://accounts.google.com; frame-src https://accounts.google.com https://docs.google.com; connect-src 'self' https://accounts.google.com https://www.googleapis.com; img-src 'self' data: https://*.googleusercontent.com https://*.gstatic.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"})
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return render_template("message.html", message=error.description), error.code

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = ""
        if request.method == "POST":
            # Global throttle cannot be bypassed with forged proxy headers or new cookies.
            with jobs.lock:
                now = time.time()
                while jobs.login_attempts and jobs.login_attempts[0] < now - 60:
                    jobs.login_attempts.popleft()
                if len(jobs.login_attempts) >= 15:
                    abort(429, "Too many login attempts. Please wait one minute.")
                jobs.login_attempts.append(now)
            if check_password_hash(password_hash, request.form.get("password", "")):
                session.clear()
                session.update(owner=secrets.token_urlsafe(32), epoch=epoch, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect("/")
            error = "That password didn’t match. Please try again."
        return render_template("login.html", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        with jobs.lock:
            if any(r["owner"] == session["owner"] and r["status"] in ACTIVE for r in jobs.rows.values()):
                abort(409, "Wait for your active run to finish before signing out.")
            jobs.rows = {key: row for key, row in jobs.rows.items() if row["owner"] != session["owner"]}
        session.clear()
        return redirect("/login")

    @app.get("/")
    def home():
        ready = all(app.config[k] for k in ("GOOGLE_CLIENT_ID", "GOOGLE_PICKER_KEY", "GOOGLE_PROJECT_NUMBER"))
        return render_template("home.html", ready=ready)

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/api/jobs")
    def list_jobs():
        with jobs.lock:
            jobs.prune()
            return jsonify(jobs=[{key: value for key, value in row.items() if key != "owner"}
                                 for row in reversed(list(jobs.rows.values())) if row["owner"] == session["owner"]])

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        with jobs.lock:
            jobs.prune()
            row = jobs.rows.get(job_id)
            if not row or row["owner"] != session["owner"]:
                abort(404, "This run is unavailable. It may have expired or the server restarted. Check your deck before retrying.")
            return jsonify({key: value for key, value in row.items() if key != "owner"})

    @app.post("/api/jobs")
    def start_job():
        if not app.config["GOOGLE_CLIENT_ID"] or not os.getenv("OPENAI_API_KEY") or not os.getenv("BRAVE_API_KEY"):
            abort(503, "The owner still needs to finish the Google/API setup.")
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, "Expected a presentation selection.")
        token, deck_id, figures = data.get("access_token"), data.get("deck_id"), data.get("figures_per_slide")
        if not isinstance(token, str) or not 20 <= len(token) <= 4096 or not re.fullmatch(r"[A-Za-z0-9._~-]+", token):
            abort(400, "Choose your presentation again to authorize Google access.")
        if not isinstance(deck_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", deck_id):
            abort(400, "Choose a Google Slides presentation.")
        if type(figures) is not int or not 1 <= figures <= app.config["MAX_FIGURES"]:
            abort(400, "Choose a valid maximum figures per slide. Reload the page if it was open before the update.")
        try:
            title = verify_google(token, deck_id, app.config["GOOGLE_CLIENT_ID"], app.config["JOB_SECONDS"] + 60)
        except ValueError as exc:
            abort(400, str(exc))
        job_id = jobs.start(session["owner"], token, deck_id, title, figures)
        return jsonify(id=job_id), 202

    @app.post("/api/jobs/<job_id>/delete")
    def delete_job(job_id):
        with jobs.lock:
            row = jobs.rows.get(job_id)
            if not row or row["owner"] != session["owner"]:
                abort(404)
            if row["status"] in ACTIVE:
                abort(409, "Wait for the run to finish before clearing it.")
            del jobs.rows[job_id]
        return jsonify(deleted=True)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("PORT", "8001")))
