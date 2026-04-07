# Phase 4: Celery Pipeline — All 3 Files Explained

> Learn doc for `backend/services/ai.py`, `backend/tasks/celery_app.py`, `backend/tasks/pipeline.py`

---

## Why a Task Queue at All?

When a user uploads a meeting file, the backend needs to:
1. Parse the transcript (fast, ~0.1s)
2. Load the embedding model and embed 50–200 chunks (~10s on first call)
3. Make 2 Groq API calls (~5–15s each)
4. Write everything to PostgreSQL

That's 20–40 seconds total.

**If you ran that inside a FastAPI request handler**, the HTTP request would hang for 40 seconds. Most browsers and reverse proxies time out at 30 seconds. The user would see an error even though the work succeeded.

**Solution: decouple**. The web server receives the file and returns *immediately*. A separate process (the Celery worker) does the heavy work in the background. The user polls a status endpoint until `processed=True`.

---

## How the 3 Files Fit Together

```
Upload request
     │
     ▼
[FastAPI route]  (Phase 5 — not yet written)
     │  calls: process_meeting.delay(meeting_id, filename, content)
     ▼
[celery_app.py]  ←── defines the Celery app and Redis connection
     │  routes task message through Redis broker
     ▼
[pipeline.py]    ←── the Celery task: orchestrates everything
     │
     ├── calls parse_transcript()        (parser.py — Phase 2)
     ├── calls embed_and_store()         (embeddings.py — Phase 3)
     ├── calls extract_decisions_and_actions()  ┐
     └── calls analyze_sentiment()              ┘ (ai.py — Phase 4)
```

Each file has one clear responsibility:
- `celery_app.py` — **configuration only**: creates the app, sets up Redis
- `ai.py` — **LLM calls only**: all Groq interactions, retry logic
- `pipeline.py` — **orchestration only**: calls everything else in the right order, manages DB writes

---

---

# File 1: `backend/services/ai.py`

## What This File Does

All Groq LLM calls — and *only* Groq calls — live here. No route, no task, no other service is allowed to call Groq directly. This is a hard rule from `CLAUDE.md`.

Why? One place to:
- Swap the model (`llama-3.3-70b-versatile` → something newer)
- Adjust prompts across the whole app
- Change retry logic
- Add token counting or cost tracking
- Mock for tests

## The 4 Things Inside It

```
ai.py
│
├── get_client()                       — Groq client singleton
├── _call_groq(messages)               — internal retry wrapper
├── extract_decisions_and_actions()    — Groq call 1 (public)
└── analyze_sentiment()                — Groq call 2 (public)
```

## Workflow: `get_client()`

```
First call:
  _client is None
      ↓
  Groq(api_key=GROQ_API_KEY)          ← reads from config.py (from .env)
      ↓
  _client = <Groq instance>           ← saved at module level
      ↓
  return _client

All subsequent calls:
  _client is not None
      ↓
  return _client                      ← same object, no reconnection
```

This is the **singleton pattern** — the Groq client holds an HTTP connection pool internally. Creating a new one per call would waste TCP handshakes. One client for the lifetime of the worker process.

## Workflow: `_call_groq(messages)`

This is a private function (prefixed with `_`) — only called inside `ai.py`.

```
attempt 1:
  client.chat.completions.create(
      model="llama-3.3-70b-versatile",
      messages=messages,
      temperature=0.0,
      response_format={"type": "json_object"}   ← forces valid JSON output
  )
  ├── success → return response string
  ├── RateLimitError (429) → sleep 1s → attempt 2
  └── APIError (5xx) → sleep 1s → attempt 2

attempt 2:
  ├── success → return response string
  ├── RateLimitError → sleep 2s → attempt 3
  └── APIError → sleep 2s → attempt 3

attempt 3:
  ├── success → return response string
  └── any error → raise (caller sees the exception)
```

Sleep times: 1s, 2s, 4s — this is **exponential backoff**. Each retry waits twice as long as the last. Why? If Groq is overloaded, hammering it immediately makes it worse. Backing off gives it time to recover.

`response_format={"type": "json_object"}` is important — it tells the model it *must* return parseable JSON. Without this, the model might say `"Here is the JSON: \`\`\`json {...}\`\`\`"` which would break `json.loads()`.

`temperature=0.0` — deterministic output. For fact extraction you want the model to pick the most probable answer, not a creative one.

## Workflow: `extract_decisions_and_actions(transcript, filename)`

```
transcript (full meeting text, speaker-labelled)
filename   ("team_meeting_2024-07-15.vtt")
    │
    ├── truncate to 60,000 chars if too long (safety valve)
    │
    ├── build system_prompt: "Extract only what is explicitly stated..."
    │
    ├── build user_prompt:
    │     "Meeting file: {filename}
    │      Transcript: {truncated}
    │      Return JSON in this format:
    │        { decisions: [...], action_items: [...] }"
    │
    ├── _call_groq([system_msg, user_msg])
    │     → raw JSON string from model
    │
    ├── json.loads(raw)
    │     ├── success → data dict
    │     └── JSONDecodeError → log warning, data = {}
    │
    └── return {
          "decisions":    data.get("decisions") or [],
          "action_items": data.get("action_items") or []
        }
```

The `or []` fallback handles cases where the model returns `null` or omits the key entirely.

**What the model returns:**
```json
{
  "decisions": [
    {"text": "Go with Snowflake", "timestamp": "00:01:55", "speaker": "Alice"}
  ],
  "action_items": [
    {"task": "Initiate Snowflake contract", "assignee": "Carol", "due_date": "end of week", "timestamp": "00:02:05"}
  ]
}
```

## Workflow: `analyze_sentiment(segments)`

`segments` is a list of dicts built from child chunks — one dict per chunk:
```python
{"index": 0, "chunk_id": 42, "speaker": "Alice", "start_time": "00:00:10", "text": "..."}
```

```
segments (list of chunk dicts)
    │
    ├── cap at 80 segments (MAX_SENTIMENT_SEGMENTS)
    │     why 80? → long meetings have 200+ chunks; 80 ≈ 60 min meeting
    │     sending all 200 wastes tokens + model accuracy drops near context boundary
    │
    ├── format as numbered list:
    │     "[0] [00:00:10] Alice: We need to finalize..."
    │     "[1] [00:00:30] Bob: I agree with that..."
    │     ...
    │
    ├── build prompt asking for per-speaker and per-segment scores
    │
    ├── _call_groq([system_msg, user_msg])
    │     → raw JSON string
    │
    ├── json.loads(raw)
    │
    └── return {
          "speaker_scores": {"Alice": 0.3, "Bob": 0.5},
          "segment_scores": [
            {"segment_index": 0, "score": 0.3, "label": "positive"},
            {"segment_index": 1, "score": 0.1, "label": "neutral"},
            ...
          ]
        }
```

Notice: Groq returns `segment_index` (the position in the list we sent), not `chunk_id`.
The `chunk_id` is only in our local `segments` list. `pipeline.py` maps them back together after the call.

---

---

# File 2: `backend/tasks/celery_app.py`

## What This File Does

One job: **create and configure the Celery application object**.

This file is imported by:
- `pipeline.py` — to register tasks onto the app with `@celery_app.task`
- The CLI when you start a worker: `celery -A backend.tasks.celery_app.celery_app worker`

## The 2 Things Inside It

```
celery_app.py
│
├── celery_app = Celery(...)           — creates the app instance
└── celery_app.conf.update(...)        — sets configuration options
```

## Workflow: App Creation

```
Celery(
  "meetai",                            ← app name (appears in logs)
  broker=CELERY_BROKER_URL,            ← redis://localhost:6379/0  (task inbox)
  backend=CELERY_RESULT_BACKEND,       ← redis://localhost:6379/1  (task results)
)
```

Redis is used in **two separate roles**:

| Role | Redis DB | What it stores |
|------|----------|----------------|
| **Broker** | `/0` | Task *messages* — the function name + serialized arguments waiting to be picked up |
| **Result Backend** | `/1` | Task *state and return values* — PENDING, STARTED, SUCCESS, FAILURE, and the return dict |

They're on different database numbers (`/0`, `/1`) to avoid key collisions.

## Workflow: Configuration

```
celery_app.conf.update(
  task_serializer  = "json"    → serialize task args as JSON (not pickle)
                                  why? pickle can execute arbitrary code if Redis is compromised
                                       JSON is safe and human-readable in Redis

  accept_content   = ["json"]  → only accept JSON messages (reject pickled tasks)

  timezone         = "UTC"     → all timestamps in task logs/results use UTC
  enable_utc       = True

  task_acks_late   = True      → ack (remove from queue) only AFTER task finishes
                                  default is False (ack when worker STARTS the task)
                                  why True? if the worker process is killed mid-task,
                                  the task stays in Redis and another worker picks it up
                                  risk: if task half-completes before crash, re-running it
                                  may create duplicate DB rows (acceptable for us now)

  task_track_started = True    → expose the STARTED state
                                  without this, a task jumps from PENDING → SUCCESS/FAILURE
                                  with this: PENDING → STARTED → SUCCESS/FAILURE
                                  the status endpoint uses STARTED to show "Processing..."

  include = ["backend.tasks.pipeline"]
                               → when the worker starts, import this module
                                  this registers process_meeting as a known task
                                  without this, the worker wouldn't know about our task
)
```

## How the Worker Uses This File

```bash
uv run celery -A backend.tasks.celery_app.celery_app worker --loglevel=info
```

Breaking down that command:
- `-A backend.tasks.celery_app.celery_app` — Python module path to the Celery *instance* (not just the module)
- `worker` — start a worker process
- `--loglevel=info` — show INFO logs (task started, completed, failed)

The worker:
1. Imports `backend.tasks.celery_app` → gets the configured `celery_app`
2. Reads `include=["backend.tasks.pipeline"]` → imports `pipeline.py` → registers `process_meeting`
3. Connects to Redis broker
4. Waits in a loop, picking up tasks as they arrive

---

---

# File 3: `backend/tasks/pipeline.py`

## What This File Does

The main Celery task and its helper functions. It orchestrates all previous phases: calls `parser.py`, calls `embeddings.py`, calls `ai.py`, and writes everything to PostgreSQL.

## The 5 Things Inside It

```
pipeline.py
│
├── _store_and_embed(meeting_id, parse_result)    — async: DB write + embed
├── _store_results(meeting_id, ...)               — async: store Groq results + mark done
├── _mark_error(meeting_id, error_message)        — async: write error to meeting row
├── _build_transcript_text(parse_result)          — sync: build text string for Groq
└── process_meeting(meeting_id, filename, content) — the Celery task (sync entry point)
```

Functions prefixed with `_` are private — only called within this file. `process_meeting` is the only public entry point.

## Workflow: `process_meeting()` — The Top-Level Task

```
process_meeting(meeting_id=42, filename="meeting.vtt", content="WEBVTT\n...")
│
│  (this runs in a Celery worker process — no async event loop here)
│
try:
├── Step 1: parse_transcript(content, filename)
│     → ParseResult
│         .child_chunks  [ChunkData, ChunkData, ...]  (50-200 items)
│         .parent_chunks [ChunkData, ChunkData, ...]  (grouped 5-min windows)
│         .speaker_names ["Alice", "Bob", "Carol"]
│         .word_count    1842
│         .meeting_date  datetime(2024, 7, 15)
│     (pure Python — no DB, no network, fast)
│
├── Step 2: asyncio.run(_store_and_embed(meeting_id, parse_result))
│     → (child_ids, chunk_segments)
│     (opens DB, inserts rows, embeds, commits, closes DB)
│
├── Step 3: _build_transcript_text(parse_result)
│     → full_transcript string (parent chunks joined with blank lines)
│
├── Step 4: extract_decisions_and_actions(full_transcript, filename)
│     → extractions = {"decisions": [...], "action_items": [...]}
│     (Groq API call — retried internally in ai.py)
│
├── Step 5: analyze_sentiment(chunk_segments)
│     → sentiment_data = {"speaker_scores": {...}, "segment_scores": [...]}
│     (Groq API call — retried internally in ai.py)
│
├── Step 6: asyncio.run(_store_results(meeting_id, extractions, sentiment_data, chunk_segments))
│     (opens DB, writes extractions + sentiments, marks processed=True, commits, closes DB)
│
└── return {"meeting_id": 42, "child_chunks": 74, "decisions": 3, "action_items": 5}
         ↑ stored in Redis result backend

except Exception:
├── asyncio.run(_mark_error(meeting_id, str(exc)))
│     (opens DB, sets meeting.error = "...", closes DB)
└── raise   ← Celery marks task as FAILURE
```

## Workflow: `_store_and_embed()` — The DB Write Phase

This is an `async` function. It runs inside `asyncio.run()` from the task.

```
_store_and_embed(meeting_id=42, parse_result)
│
│ async with AsyncSessionLocal() as session:
│   async with session.begin():           ← one transaction for everything
│
├── Loop over parse_result.parent_chunks:
│     for each parent ChunkData:
│       ├── Chunk(meeting_id=42, is_parent=True, speaker=..., text=..., ...)
│       ├── session.add(chunk_row)
│       ├── await session.flush()         ← sends INSERT, gets auto-generated .id
│       └── parent_db_ids.append(chunk_row.id)
│     result: parent_db_ids = [101, 102, 103]  ← real PostgreSQL IDs
│
├── Loop over parse_result.child_chunks:
│     for each child ChunkData at index i:
│       ├── resolve parent_id:
│       │     child_data.parent_index = 1  (index into parent_chunks list)
│       │     parent_db_id = parent_db_ids[1] = 102  (real DB ID)
│       ├── Chunk(meeting_id=42, is_parent=False, parent_id=102, ...)
│       ├── session.add(chunk_row)
│       ├── await session.flush()         ← gets auto-generated .id
│       ├── child_db_ids.append(chunk_row.id)
│       ├── child_texts.append(chunk_row.text)
│       └── chunk_segments.append({
│               "index": i,
│               "chunk_id": chunk_row.id,   ← real DB ID saved here
│               "speaker": ..., "start_time": ..., "text": ...
│             })
│
├── Update meeting row:
│     meeting = await session.get(Meeting, 42)
│     meeting.speaker_names = ["Alice", "Bob", "Carol"]
│     meeting.word_count    = 1842
│     meeting.meeting_date  = datetime(2024, 7, 15)
│
├── await embed_and_store(session, child_db_ids, child_texts)
│     → calls embeddings.py: runs bge model, writes vectors + tsvector to chunk rows
│
└── session.begin() exits → auto-commit ← all inserts + updates committed atomically

return (child_db_ids, chunk_segments)
```

**Why flush() inside the loop instead of one commit at the end?**

Parent chunks must be inserted and their IDs obtained *before* inserting child chunks that reference them via `parent_id` (a foreign key). `flush()` sends the INSERT and gets the ID back from PostgreSQL — but doesn't commit. Everything is still one atomic transaction.

## Workflow: `_store_results()` — After Groq Calls

```
_store_results(meeting_id, extractions, sentiment_data, chunk_segments)
│
│ Build index_to_chunk_id:
│   {0: 201, 1: 202, 2: 203, ...}
│   (maps Groq's segment_index → real chunk DB ID)
│
│ Build segment_scores list:
│   for each seg in sentiment_data["segment_scores"]:
│     idx = seg["segment_index"]          ← e.g. 2
│     chunk_id = index_to_chunk_id[2]     ← e.g. 203 (real DB ID)
│     append {
│       "chunk_id":   203,
│       "speaker":    "Alice",
│       "start_time": "00:00:45",
│       "score":      0.4,
│       "label":      "positive"
│     }
│
│ async with session.begin():
│
├── Extraction(
│     meeting_id=42,
│     decisions=    [{"text": "...", "timestamp": "...", "speaker": "..."}],
│     action_items= [{"task": "...", "assignee": "...", ...}]
│   )
│   session.add(extraction_row)
│
├── Sentiment(
│     meeting_id=42,
│     speaker_scores= {"Alice": 0.3, "Bob": 0.5},
│     segment_scores= [{chunk_id: 203, score: 0.4, label: "positive"}, ...]
│   )
│   session.add(sentiment_row)
│
├── meeting = await session.get(Meeting, 42)
│   meeting.processed = True
│   meeting.error     = None
│
└── auto-commit
```

## Workflow: `_mark_error()`

```
_mark_error(meeting_id=42, error_message="Connection timeout to Groq")
│
│ async with session.begin():
│   meeting = await session.get(Meeting, 42)
│   meeting.processed = False
│   meeting.error     = "Connection timeout to Groq"  (truncated to 1000 chars)
└── auto-commit
```

This is only called in the `except` block of `process_meeting`. It lets the status endpoint show the user *why* the pipeline failed, rather than leaving them seeing "still processing" forever.

## Why Two Separate asyncio.run() Calls?

```
process_meeting()
│
├── asyncio.run(_store_and_embed(...))
│     ↑ DB connection open
│     ↓ DB connection closed ← connection returned to pool here
│
│   ← Groq calls happen HERE — no DB connection held
│     (5-15 seconds each, external network call)
│
└── asyncio.run(_store_results(...))
      ↑ DB connection open
      ↓ DB connection closed
```

Holding a DB connection during the Groq wait is wasteful. Each connection uses ~5–10 MB RAM on PostgreSQL, and the pool has limited slots. Releasing it between steps means other requests can use the pool while this task waits for Groq.

---

---

# How All 3 Files Connect at Runtime

```
celery_app.py                    ai.py                    pipeline.py
─────────────                    ──────                   ───────────
celery_app = Celery(...)         get_client()             @celery_app.task
  broker=Redis/0                 _call_groq()               ↑ registered on celery_app
  backend=Redis/1                extract_decisions()
  include=[pipeline]             analyze_sentiment()
                                      ↑
                               called by pipeline.py


Worker startup:
  import celery_app.py → celery_app instance created
  include triggers:
    import pipeline.py → process_meeting registered as task

Upload (Phase 5, not yet written):
  FastAPI → process_meeting.delay(42, "file.vtt", "...") → Redis/0

Worker loop:
  Redis/0 has a task message →
    pipeline.py: process_meeting(42, "file.vtt", "...")
      → parse_transcript()          (parser.py)
      → asyncio.run(_store_and_embed)
           → embed_and_store()      (embeddings.py)
      → extract_decisions()         (ai.py → Groq)
      → analyze_sentiment()         (ai.py → Groq)
      → asyncio.run(_store_results)
  result stored in Redis/1
```

---

## Quick Reference: Where Each Concern Lives

| Concern | File |
|---------|------|
| Groq client, prompts, retry | `services/ai.py` |
| Celery app, Redis config | `tasks/celery_app.py` |
| Task logic, DB writes, orchestration | `tasks/pipeline.py` |
| Embedding model, vector storage | `services/embeddings.py` (Phase 3) |
| VTT/TXT parsing, chunking | `services/parser.py` (Phase 2) |
| ORM models (tables) | `models.py` (Phase 1) |
