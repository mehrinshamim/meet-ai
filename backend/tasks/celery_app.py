"""
celery_app.py — Celery application instance.

What is Celery?
  Celery is a task queue: it lets you push work (a function call) onto a queue
  so a separate worker process can execute it asynchronously.  The web server
  (FastAPI) just enqueues the task and returns immediately; the Celery worker
  does the heavy lifting in the background.

Why do we need this?
  Processing a meeting takes 10-60 seconds (embedding model + Groq calls).
  If we ran that inside a FastAPI request handler, the HTTP request would time
  out and the user would be stuck waiting.  With Celery:
    1. FastAPI receives the file, saves it, enqueues the task → returns in <1s
    2. The Celery worker processes the file in the background
    3. The frontend polls /api/meetings/{id}/status until processed=True

Why Redis as the broker?
  The broker is the message queue Celery workers read from.
  Redis is fast, simple, and we're already running it (docker-compose.yml).
  The broker (DB 0) stores task arguments until a worker picks them up.
  The result backend (DB 1) stores task results and status.
  Two separate Redis databases keep them cleanly separated.

Configuration notes:
  - task_serializer="json"    → task arguments are JSON (safe, human-readable)
  - task_acks_late=True       → the task is only acknowledged (removed from queue)
                                after it finishes, not when the worker starts it.
                                If the worker crashes mid-task, another worker will
                                re-pick it up.
  - task_track_started=True   → task state transitions to STARTED when a worker
                                begins processing.  We surface this via the
                                /api/meetings/{id}/status endpoint.
"""

from celery import Celery

from backend.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

celery_app = Celery(
    "meetai",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Reliability: ack only after task completes, not when it starts.
    # Prevents task loss if the worker process is killed mid-execution.
    task_acks_late=True,

    # Expose STARTED state so the status endpoint can distinguish
    # "waiting in queue" from "actively being processed".
    task_track_started=True,

    # Auto-discover tasks in the tasks package.
    # Celery will import backend.tasks.pipeline when the worker starts.
    include=["backend.tasks.pipeline"],
)
