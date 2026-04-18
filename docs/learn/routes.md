# Phase 5 — FastAPI Routes

---

## Bug: "Future attached to a different loop"

This crashed the pipeline during testing. Understanding it is important because
it reveals how Python's async model actually works under the hood.

### The characters

**Event loop** — the engine that runs async code. Every `await` call hands
control back to the event loop, which decides what to run next. There can only
be one active event loop per thread at a time.

**asyncio.run(coro)** — creates a brand-new event loop, runs the coroutine
inside it until it finishes, then **destroys the loop and closes everything in it**.

**asyncpg connection** — a live TCP socket to PostgreSQL. When asyncpg opens a
connection, it registers it with the current event loop. The connection can only
be used from that same loop — it holds an internal reference to it.

### What went wrong

```
asyncio.run(_store_and_embed(...))
│
├─ creates event loop 1
├─ asyncpg opens DB connection → bound to loop 1
├─ INSERT chunks, embed, commit
└─ loop 1 is DESTROYED
   └─ but the asyncpg connection pool still exists in memory,
      still holding a reference to the now-dead loop 1

# Groq calls happen here (pure Python, no async)

asyncio.run(_store_results(...))
│
├─ creates event loop 2
├─ tries to use the engine (same module-level object)
├─ engine still has connections from loop 1 in its pool
├─ asyncpg tries to use a loop-1 connection inside loop 2
└─ CRASH: "Future attached to a different loop"
```

### Why this only crashes on the second call

The first `asyncio.run()` works fine because it creates loop 1 and the
connections are created inside loop 1 — everything matches.

The second `asyncio.run()` fails because the engine's connection pool was
populated during loop 1. Python won't let you use a connection from one
event loop inside a different event loop.

### The fix: engine.dispose()

```python
child_ids, chunk_segments = asyncio.run(_store_and_embed(meeting_id, parse_result))

engine.dispose()   # ← this is the fix

asyncio.run(_store_results(meeting_id, extractions, sentiment_data, chunk_segments))
```

`engine.dispose()` tells SQLAlchemy: "close every connection in the pool right now,
forget they existed". It's a synchronous call (no event loop needed).

The next `asyncio.run()` starts with an empty pool. When `_store_results` first
touches the DB, asyncpg opens fresh connections — bound to loop 2, which is the
currently active loop. Everything matches, no crash.

### Analogy — the post office

Imagine `asyncio.run()` is like **opening a post office branch for a day**.

- The branch opens in the morning (event loop created)
- It hires delivery drivers and assigns them vans — each van has the **branch's ID stamped on it** (asyncpg connections bound to the loop)
- Deliveries happen all day (DB queries run)
- At 5pm the branch **shuts down permanently** (event loop destroyed)

Now here's the problem:

The **vans don't disappear** when the branch closes. They're still parked outside (connections still exist in the pool). The branch ID stamped on them still says "Branch 1".

Next morning, a **completely new branch opens** (second `asyncio.run()`). It's Branch 2. It tries to use the vans parked outside — but those vans say "Branch 1" on them. Branch 2 refuses: *"These aren't our vans."* Everything crashes.

`engine.dispose()` is like **impounding all the vans** at the end of the day before the branch closes. Next morning, Branch 2 opens to an empty car park. It orders brand new vans, stamps them "Branch 2", and everything works fine.

### Why engine.dispose() wasn't enough (first fix attempt)

`engine.dispose()` closes the TCP sockets — the actual network connections to
PostgreSQL. But `asyncpg` also creates internal asyncio objects (locks, queues,
condition variables) that are registered with the event loop. These are not
network connections — they're Python objects living in memory, and `dispose()`
doesn't touch them.

So after `dispose()`:
- TCP sockets: closed ✓
- asyncpg internal asyncio objects: still tied to dead loop 1 ✗

The second `asyncio.run()` still finds those internal objects and crashes on them.

### The final fix: one event loop for everything

Instead of two `asyncio.run()` calls, we now have one `asyncio.run()` that calls
a single async orchestrator `_run_pipeline()`:

```python
# Before (broken): two event loops
child_ids, chunk_segments = asyncio.run(_store_and_embed(...))   # loop 1, dies
engine.dispose()
asyncio.run(_store_results(...))                                  # loop 2, crashes

# After (fixed): one event loop
asyncio.run(_run_pipeline(...))   # one loop, lives for the whole pipeline
```

Inside `_run_pipeline`, the blocking Groq calls run in a **thread pool executor**:

```python
async def _run_pipeline(meeting_id, filename, parse_result):
    # DB writes (async, releases connection when done)
    child_ids, chunk_segments = await _store_and_embed(meeting_id, parse_result)

    # Groq calls run in a background thread — loop stays alive
    extractions = await loop.run_in_executor(None, extract_decisions_and_actions, ...)
    sentiment_data = await loop.run_in_executor(None, analyze_sentiment, ...)

    # DB writes again — same loop, fresh session, no conflict
    await _store_results(meeting_id, extractions, sentiment_data, chunk_segments)
```

`run_in_executor(None, fn, *args)` means: "run `fn(*args)` in the default thread
pool, and `await` its result". The event loop is **not** blocked while the thread
runs — it can do other work. When the thread finishes, control returns here.

This gives us everything we wanted:
- Single event loop — no loop switching, no stale references
- DB connection released before Groq calls — `_store_and_embed`'s `async with` session closes when it returns
- Groq calls don't block the event loop — they run in a thread

### The visual

```
# BROKEN (two loops):
asyncio.run(_store_and_embed)
  loop 1 created
  asyncpg: conn + internal locks/queues → all stamped "loop 1"
  loop 1 destroyed
  conn + locks/queues still in memory, still say "loop 1" ← stale

asyncio.run(_store_results)
  loop 2 created
  asyncpg finds stale "loop 1" objects → CRASH

# FIXED (one loop):
asyncio.run(_run_pipeline)
  loop 1 created
  _store_and_embed → conn opens (loop 1), work done, conn released
  run_in_executor → Groq runs in thread, loop 1 stays alive
  _store_results  → conn opens (loop 1) again, work done, conn released
  loop 1 destroyed cleanly — no stale references
```

### Why not just use one asyncio.run()?

We deliberately split into two `asyncio.run()` calls to avoid holding a DB
connection open while Groq calls happen (which take 5–15 seconds).
Holding a connection during a long wait wastes a slot from the connection pool.
The dispose-between-calls pattern keeps that benefit while fixing the loop crash.

---

## What is a route?

A route is a URL + HTTP method pair that maps to a Python function.
When a browser or curl sends `POST /api/meetings/upload`, FastAPI finds the
matching function and calls it.  The function runs, returns something, and
FastAPI serialises it into a JSON HTTP response.

```
Client  ──POST /api/meetings/upload──►  FastAPI  ──►  upload_meeting()
                                                            │
                                                     validates file
                                                     inserts DB row
                                                     enqueues Celery task
                                                     returns JSON
```

---

## APIRouter vs the app directly

You *could* put all routes directly on `app`:

```python
@app.post("/api/meetings/upload")
async def upload_meeting(): ...
```

But as the project grows, `main.py` becomes a 1000-line mess.
Instead, we use `APIRouter` — a mini-app that holds a group of related routes:

```python
# routes/meetings.py
router = APIRouter()

@router.post("/meetings/upload")
async def upload_meeting(): ...
```

Then in `main.py`:
```python
app.include_router(meetings.router, prefix="/api")
```

FastAPI glues them together: the final URL is `/api` + `/meetings/upload` = `/api/meetings/upload`.

---

## Pydantic schemas

Every route declares what it accepts and what it returns using Pydantic models.

```python
class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
```

If a client sends `{"description": "hello"}` (missing `name`), FastAPI rejects
it automatically with a 422 response — you never even reach your function.

For responses, `response_model=ProjectOut` tells FastAPI:
- serialise the return value using `ProjectOut`
- strip any fields that aren't in `ProjectOut` (security: don't leak internals)
- validate that your code actually returns the right shape

`model_config = {"from_attributes": True}` lets Pydantic read SQLAlchemy ORM
objects directly.  Without it, you'd have to convert `project` to a dict manually.

---

## Dependency injection — `Depends(get_db)`

Every route that needs a DB session declares it like this:

```python
async def upload_meeting(db: AsyncSession = Depends(get_db)):
```

`Depends(get_db)` means: "call `get_db()`, give me what it yields, and clean up
after the request".  FastAPI handles the lifecycle automatically.

`get_db` is a generator:
```python
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session   # ← the route runs here
        # ← session is closed after the route returns
```

This pattern guarantees DB connections are always returned to the pool, even if
the route raises an exception.

---

## File uploads — UploadFile + Form

HTTP multipart uploads are different from JSON requests.  FastAPI uses `File()`
and `Form()` instead of `Body()`:

```python
async def upload_meeting(
    file: UploadFile = File(...),      # the actual file bytes
    project_id: int | None = Form(None),  # a text field alongside the file
):
```

You **cannot** mix `File()` with a JSON body (`Body()`).  When uploading a file,
all non-file fields must be `Form()` fields.  This is an HTTP limitation, not a
FastAPI one.

`await file.read()` reads all bytes into memory.  Fine for text transcripts
(usually < 1 MB).  For large binary files (video, audio) you'd stream to disk
instead.

---

## The upload-then-poll pattern

Why return immediately instead of waiting for processing?

```
Without polling (bad):
  Upload ──[wait 30s]──► response     (user stares at spinner)

With polling (good):
  Upload ──────────────► response {meeting_id}   (instant)
  Frontend polls /status every 2s
  After ~20s: processed=True → show results
```

The Celery task runs in a separate worker process.  The API responds to the
upload in milliseconds, then the worker does the heavy work independently.

---

## CORS — why the browser would block your API otherwise

Browsers enforce the "same-origin policy": JavaScript running on
`http://localhost:5500` is not allowed to call `http://localhost:8000` by default.

`CORSMiddleware` adds HTTP headers that tell the browser "this API allows
cross-origin requests from anywhere":

```
Access-Control-Allow-Origin: *
```

Without this, your frontend JS would get a network error even though the API
is working perfectly.  It's purely a browser security feature — curl, Postman,
and server-to-server calls are never affected.

---

## Correlated subqueries — how project stats work

To count meetings per project in a single query:

```sql
SELECT
    projects.*,
    (SELECT COUNT(*) FROM meetings WHERE meetings.project_id = projects.id) AS meeting_count
FROM projects
ORDER BY created_at DESC;
```

The inner `SELECT COUNT(*)` runs once per project row, using `projects.id` from
the outer query.  That reference to the outer row is what makes it "correlated".

In SQLAlchemy:
```python
meeting_count_sq = (
    select(func.count(Meeting.id))
    .where(Meeting.project_id == Project.id)
    .correlate(Project)       # ← tells SQLAlchemy this references the outer Project
    .scalar_subquery()        # ← makes it a scalar (single value) subquery
)
```

---

## `jsonb_array_length` — counting items in a JSONB array

Our `action_items` column stores a JSON array:
```json
[{"task": "update roadmap", "assignee": "Bob"}, ...]
```

To count the total number of action items across all extractions:
```sql
SELECT COALESCE(SUM(jsonb_array_length(action_items)), 0)
FROM extractions
WHERE action_items IS NOT NULL;
```

`jsonb_array_length([...])` returns the number of elements.
`SUM(...)` adds them all up.
`COALESCE(..., 0)` returns 0 if there are no extractions (SUM of nothing is NULL).

---

## In-memory workflow

```
1. Client sends POST /api/meetings/upload with file bytes

2. FastAPI calls upload_meeting()
   ├─ validates extension (must be .txt or .vtt)
   ├─ decodes bytes → UTF-8 string
   ├─ opens DB transaction
   │   ├─ INSERT meetings row (processed=False)
   │   ├─ flush → get meeting.id
   │   ├─ process_meeting.delay(meeting.id, filename, content)
   │   │       └─ pushes task to Redis queue, returns a task handle instantly
   │   └─ saves task.id on meeting row
   └─ returns MeetingOut JSON

3. Client receives {id, processed: false, task_id: "abc123", ...}

4. Client polls GET /api/meetings/{id}/status every 2s
   ├─ FastAPI reads meeting row from DB
   ├─ calls celery_app.AsyncResult(task_id).state → queries Redis
   └─ returns {processed: false, task_status: "STARTED", error: null}

5. Meanwhile, Celery worker:
   ├─ picks up task from Redis
   ├─ parse → embed → Groq → store
   └─ sets meeting.processed = True in DB

6. Next poll: processed=True → frontend navigates to meeting page
```
