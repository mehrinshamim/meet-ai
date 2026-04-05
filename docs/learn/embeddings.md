# embeddings.md — Embeddings: What, Why, and How

> Phase 3 concept doc. Read this alongside `backend/services/embeddings.py`.

---

## What is an embedding?

An **embedding** is a list of numbers that represents the *meaning* of a piece of text.

```
"The meeting was postponed."  →  [0.032, -0.187, 0.441, ..., 0.019]  (1024 numbers)
```

Every string — a word, a sentence, a paragraph — can be converted into one of these lists.
The critical property: **texts with similar meanings produce vectors that point in the same direction** in this 1024-dimensional space.

```
"Rescheduled the call."     →  [0.031, -0.191, 0.438, ...]
"The meeting was postponed." →  [0.032, -0.187, 0.441, ...]
                                  ↑ nearly identical — they mean the same thing
```

This is what makes semantic search work. Instead of matching keywords, we match *meaning*.

---

## Why 1024 dimensions?

The model we use — **BAAI/bge-large-en-v1.5** — outputs 1024-dimensional vectors.

Think of dimensions as "axes of meaning". More dimensions = the model can encode more nuance.

| Model size | Dimensions | Tradeoff |
|---|---|---|
| Tiny (e.g. MiniLM) | 384 | Very fast, less accurate |
| Medium | 768 | Balanced |
| **bge-large (ours)** | **1024** | Slower, most accurate — right choice for RAG |

The dimension must match the pgvector column in the database exactly: `Vector(1024)` in `models.py`. If you change the model, you must also change the column size and re-embed everything.

---

## What is cosine similarity?

Two vectors can be compared by measuring the angle between them. The **cosine of that angle** is the similarity score.

```
cosine_sim(a, b) = (a · b) / (‖a‖ × ‖b‖)
```

- `1.0` → angle is 0° → vectors point the same direction → identical meaning
- `0.0` → angle is 90° → perpendicular → unrelated topics
- `-1.0` → angle is 180° → opposite direction → opposite meaning

pgvector uses the `<=>` operator, which computes cosine **distance** = `1 − cosine_sim`. Lower distance = more similar. So querying for the closest chunk means `ORDER BY embedding <=> query_vector ASC`.

---

## Why we normalise vectors (unit length)

In `embed_texts()` we pass `normalize_embeddings=True` to `model.encode()`.

This scales every vector so its length (L2 norm) is exactly `1.0`.

**Why?** When both vectors are unit length:
```
cosine_sim(a, b) = a · b   (just the dot product — division by norms becomes 1/1 = 1)
```

The dot product is a simpler, faster operation. The ivfflat index in pgvector is optimised for it. So normalising at index time means every similarity lookup is faster at query time.

---

## The bge asymmetric prefix trick

Most embedding models treat documents and queries the same way. BGE does not — it is **asymmetric**.

It was trained to expect a different instruction prefix depending on the role of the text:

| Role | Prefix | Used where |
|---|---|---|
| Transcript chunk (document) | `"Represent this passage: "` | `embeddings.py` at index time |
| User question (query) | `"Represent this question: "` | `retrieval.py` at query time |

This asymmetry is intentional. The model learns to map questions and answers into nearby locations even though they are worded differently. Without the prefix, retrieval accuracy drops noticeably.

In `embed_texts()`, the `is_query` parameter controls which prefix is applied:

```python
prefix = "Represent this question: " if is_query else PASSAGE_PREFIX
prefixed = [prefix + t for t in texts]
```

Callers don't need to think about this — they just pass `is_query=True` when embedding a user's question.

---

## Batch embedding

The model processes text through a neural network. Running it on 1 text at a time wastes most of the GPU's (or CPU's) parallel capacity.

**Batching** means sending 32 texts at once. The model processes them in a single forward pass — the same time it would take for 1 text, but now you get 32 vectors out.

```python
embeddings = model.encode(
    prefixed,
    batch_size=BATCH_SIZE,   # BATCH_SIZE = 32
    normalize_embeddings=True,
    show_progress_bar=False,
)
```

`encode()` handles pagination automatically. If you pass 200 texts, it runs 7 batches of 32 (+ a final smaller batch). You don't have to slice manually.

---

## In-memory model: the singleton pattern

Loading the embedding model means:
1. Reading ~1.3 GB of neural network weights from disk
2. Loading them into RAM (or GPU memory)
3. Initialising PyTorch's internal state

This takes **2–3 seconds**. You never want to do it inside a request handler.

The solution is a **module-level singleton** — a variable that lives in the Python process's memory for as long as the server is running.

```python
_model: SentenceTransformer | None = None   # lives in module memory

def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)  # only runs ONCE
    return _model
```

**What "module-level" means in practice:**

When Python first imports `embeddings.py`, it executes the file top-to-bottom and allocates `_model = None` in the module's namespace. That namespace persists in the process's heap memory — it's not garbage collected because the module itself is never unloaded.

- First call to `get_model()` → `_model is None` → loads the model → stores it in `_model`
- Every call after → `_model is not None` → returns the already-loaded object immediately
- The ~1.3 GB of weights stay in RAM until the process exits

This means:
- The Celery worker that processes meeting uploads loads the model once on startup
- Every upload task after that calls `embed_texts()` with zero warm-up cost
- If you restart the worker, the model loads once again on the next task

```
Process starts
  → first embed_and_store() call
    → get_model() called
      → _model is None → load from disk (2-3s)
      → _model = <loaded model>
  → second embed_and_store() call
    → get_model() called
      → _model is not None → return immediately (0ms)
  → third, fourth, ... → all instant
Process exits → _model is freed
```

---

## Code walkthrough: `embeddings.py` function by function

### Constants (top of file)

```python
MODEL_NAME = "BAAI/bge-large-en-v1.5"
PASSAGE_PREFIX = "Represent this passage: "
BATCH_SIZE = 32
VECTOR_DIM = 1024
```

**What:** Named constants for things that could otherwise be magic strings/numbers.
**Why here:** Centralised — if we ever switch models, we change `MODEL_NAME` and `VECTOR_DIM` in one place and everything else updates automatically.

---

### `get_model()` — singleton loader

```python
def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model
```

**What:** Loads the embedding model into RAM if not already loaded, then returns it.
**Why:** `embed_texts()` calls this every time it runs. The `if _model is None` guard ensures the heavy load only happens once per process lifetime.
**In the system:** Called internally by `embed_texts()`. Nothing else in the codebase calls it directly.

---

### `embed_texts()` — the core embedding function

```python
def embed_texts(texts: Sequence[str], *, is_query: bool = False) -> list[list[float]]:
```

**What:** Takes a list of strings, runs them through the model, returns a list of 1024-float vectors.
**Why:** This is the only place in the codebase where the embedding model is actually used. By isolating it here, any change to the model, prefix, or normalisation logic is a one-file change.
**In the system:**
- Called by `embed_and_store()` (below) when indexing chunks at upload time
- Called by `retrieval.py` (Phase 6) with `is_query=True` when a user asks a question

The function's steps:
1. Guard: return `[]` if input is empty (prevents crashing the model)
2. Select prefix based on `is_query`
3. Prepend prefix to all texts
4. Call `model.encode()` with batching + normalisation
5. Convert numpy array → Python list of lists (pgvector-compatible format)

---

### `store_embeddings()` — DB writer

```python
async def store_embeddings(
    session: AsyncSession,
    chunk_ids: list[int],
    vectors: list[list[float]],
    texts: list[str],
) -> None:
```

**What:** Takes parallel lists of `(chunk_id, vector, text)` and writes two columns to the `chunks` table:
- `embedding` — the 1024-dim vector for semantic search
- `search_vector` — a PostgreSQL `tsvector` for keyword search

**Why raw SQL instead of ORM?**
- pgvector's column type needs an explicit `CAST(:vector AS vector)` — SQLAlchemy's ORM doesn't handle this cleanly for bulk updates
- `to_tsvector('english', :text)` runs inside PostgreSQL, using PostgreSQL's own English stemmer and stop-word list — we don't want to replicate that logic in Python

**Why no `session.commit()` here?**
The Celery pipeline (Phase 4) will call this inside a larger transaction that also writes meeting metadata and Groq extraction results. If we committed here and the Groq call failed, we'd have partial data. By leaving commit to the caller, all writes succeed or all fail together.

**In the system:** Only called from `embed_and_store()` (and directly in tests).

---

### `embed_and_store()` — the entry point

```python
async def embed_and_store(
    session: AsyncSession,
    chunk_ids: list[int],
    texts: list[str],
) -> list[list[float]]:
```

**What:** Combines `embed_texts()` + `store_embeddings()` into a single async call. Returns the vectors in case the caller needs them.
**Why:** The Celery pipeline task in Phase 4 will do exactly "embed these chunks then store them". This wrapper means the task doesn't need to know about the two-step process — it just calls `embed_and_store()`.
**In the system:** This is the function the Celery task (`tasks/pipeline.py`, Phase 4) will call. It's the public API of this module for the pipeline.

---

## Workflow in the code file

Top-to-bottom, here is the order things happen at runtime:

```
1. Python imports embeddings.py
   → allocates _model = None in module memory
   → nothing else runs yet

2. Celery task starts processing an uploaded file
   → calls embed_and_store(session, chunk_ids, texts)

3. embed_and_store() calls embed_texts(texts)

4. embed_texts() calls get_model()
   → if first call: loads ~1.3GB model into RAM (2-3s)
   → if subsequent call: returns cached model instantly

5. embed_texts() prepends "Represent this passage: " to each text
   → sends prefixed texts to model.encode() in batches of 32
   → gets back numpy array of shape (N, 1024)
   → converts to list of lists → returns

6. embed_and_store() calls store_embeddings(session, chunk_ids, vectors, texts)

7. store_embeddings() builds SQL UPDATE params
   → executes one UPDATE per chunk via executemany (single DB round-trip)
   → sets embedding = vector, search_vector = tsvector
   → does NOT commit

8. embed_and_store() returns vectors to the Celery task

9. Celery task does remaining work (Groq calls), then commits the session
   → all writes (embeddings + Groq results) land atomically
```

---

## Workflow in the whole system

Here is where `embeddings.py` sits in the full MeetAI pipeline:

```
User uploads .vtt or .txt file
         │
         ▼
  [Phase 5] Upload route (routes/meetings.py)
  → saves file, creates Meeting row
  → enqueues Celery task
         │
         ▼
  [Phase 4] Celery task (tasks/pipeline.py)
  → calls parser.parse_transcript()       ← Phase 2
  → gets back child chunks + parent chunks
         │
         ▼
  → calls embed_and_store()               ← Phase 3 (THIS FILE)
     → get_model()    (in RAM)
     → embed_texts()  (CPU/GPU)
     → store_embeddings() (PostgreSQL)
         │
         ▼
  → calls Groq API for extractions + sentiment  ← Phase 4/services/ai.py
  → commits all DB rows
         │
         ▼
  [Phase 6] User asks a question (services/retrieval.py)
  → calls embed_texts("user question", is_query=True)   ← Phase 3 AGAIN
  → runs pgvector cosine search:  embedding <=> query_vector
  → runs tsvector keyword search: search_vector @@ tsquery
  → merges results (RRF), reranks (cross-encoder)
  → returns top chunks
         │
         ▼
  [Phase 7] Chat route (routes/chat.py)
  → assembles prompt from retrieved chunks
  → calls Groq LLM with context
  → returns answer with citations
```

`embeddings.py` is used **twice** in this flow:
1. **At index time** (after upload) — embed all chunks and store in the DB
2. **At query time** (on every chat message) — embed the user's question to search against stored vectors

The in-memory singleton means the model is loaded once and shared across both uses, for the lifetime of the process.

---

## What is tsvector and why do we set it here?

`tsvector` is PostgreSQL's internal type for full-text search. When you call `to_tsvector('english', text)`, PostgreSQL:

1. Tokenises the text into words
2. Lowercases them
3. Stems them (removes suffixes: "deployment" → "deploy", "running" → "run")
4. Removes stop words ("the", "is", "a", ...)
5. Stores the result as a compact sorted list: `'deploy':2 'pipelin':3 'block':6`

This lets you do keyword search with `@@`:
```sql
WHERE search_vector @@ plainto_tsquery('english', 'deployment pipeline')
```

We set `search_vector` in `store_embeddings()` because that's the same moment we have the chunk text available. There's no reason to make a second pass. PostgreSQL does the stemming inside the DB, which is more accurate than doing it in Python.

In Phase 6, the hybrid retrieval will run both:
- `embedding <=> query_vector` — semantic (meaning-based)
- `search_vector @@ tsquery` — keyword (exact term-based)

The scores are merged with Reciprocal Rank Fusion (RRF) to get the best of both.
