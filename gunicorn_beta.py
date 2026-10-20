"""Production HTTP entry point for the small, single-instance Slides beta."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
# Sessions, job ownership and limits live in one process. Multiple workers or
# instances would split that state. Background image work uses child processes.
workers = 1
worker_class = "gthread"
threads = 4
timeout = 120
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
preload_app = False
# Do not recycle the web process while a background presentation run is active.
max_requests = 0
