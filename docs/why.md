# why.md — Tool & Technology Justifications

> Every tool we use is here with a reason. If you're wondering "why not X?", the answer is probably here too.

---

## Backend

### FastAPI
**What:** Python web framework for building APIs.
**Why we use it:** It's async-native (handles many requests at once without blocking), automatically generates API docs at `/docs`, and uses Python type hints to validate request/response data. Much faster than Flask for I/O-heavy workloads like ours (file uploads, DB queries, LLM calls).
**Why not Flask:** Flask is synchronous by default — it handles one request at a time per worker. Bad for an app that waits on LLM APIs.
**Why not Django:** Too opinionated, too heavy. We don't need its ORM, admin panel, or templating engine.

### SQLAlchemy (async mode)
**What:** Python ORM (Object-Relational Mapper) — lets you write Python objects instead of raw SQL.
**Why we use it:** Abstracts database operations into Python classes. Async mode means DB queries don't block the FastAPI event loop. Works great with PostgreSQL + pgvector.
**Why not raw SQL:** Harder to maintain, no type safety, migration tooling is weaker.
**Why not Tortoise ORM:** Smaller community, fewer pgvector integration examples.

### Alembic
**What:** Database migration tool for SQLAlchemy.
**Why we use it:** When you change your database schema (add a column, create a table), you need a versioned record of those changes. Alembic generates migration scripts you can run forward or roll back. Without it, schema changes are manual and dangerous.
**Analogy:** Alembic is to your database what git is to your code.

### asyncpg
**What:** Async PostgreSQL driver for Python.
**Why we use it:** SQLAlchemy async mode requires an async driver to talk to PostgreSQL. asyncpg is the fastest available — benchmarks consistently show it outperforms psycopg2 by 3-5x.

---

## Database

### PostgreSQL
**What:** Production-grade relational database.
**Why we use it over SQLite:** SQLite is a single-file DB that only allows one writer at a time. Our Celery workers (multiple processes) all write to the DB simultaneously — this causes locking errors in SQLite. PostgreSQL handles concurrent writes correctly. Also supports pgvector, JSONB, and tsvector — all critical for our app.
**Why not MySQL:** No pgvector support. JSONB is worse. Full-text search is inferior.

### pgvector (PostgreSQL extension)
**What:** Adds a `vector` column type and similarity search operators to PostgreSQL.
**Why we use it:** We need to store 1024-dimensional embedding vectors and query "find the 20 most similar vectors to this query vector." pgvector does this inside PostgreSQL — no separate vector database needed.
**Why not Pinecone/Qdrant/Weaviate:** Those are separate services to run and maintain. pgvector keeps everything in one place. For our scale (thousands, not billions of vectors), pgvector is more than fast enough.
**Index type used:** `ivfflat` (Inverted File with Flat compression) — good balance of speed and accuracy for our dataset size.

### tsvector (PostgreSQL built-in)
**What:** PostgreSQL's built-in full-text search index.
**Why we use it:** Semantic search (embedding similarity) is great at finding paraphrases but terrible at exact matches. If someone asks "what did the Finance Lead say?", embedding search might miss "Finance Lead" if it's uncommon. tsvector catches exact keyword matches. We combine both (hybrid search).
**Why not Elasticsearch:** Separate service, complex setup, overkill for our scale.

---

## AI / ML

### Groq API
**What:** LLM inference API. Extremely fast (uses custom LPU hardware).
**Why we use it:** Much faster than standard GPU inference. `llama-3.3-70b-versatile` returns structured JSON in under 2 seconds typically. Free tier is generous. OpenAI-compatible SDK so easy to switch models.
**Why not Anthropic/OpenAI:** User preference. Groq is faster and cheaper for our use case.

### llama-3.3-70b-versatile (main LLM)
**What:** Meta's 70B parameter open-source model, hosted on Groq.
**Why:** Best open model for structured JSON extraction and nuanced reasoning. 70B parameters means it's smart enough to extract decisions/action items reliably and follow complex citation instructions.

### llama-3.1-8b-instant (reformulation LLM)
**What:** Meta's 8B parameter model, extremely fast on Groq.
**Why:** Query reformulation is a simple rewriting task — doesn't need a 70B model. Using 8B here is 5-10x cheaper and faster. "If the user said 'what did she mean by that?', rewrite it as a standalone question" — any decent 8B model handles this.

### sentence-transformers (BAAI/bge-large-en-v1.5)
**What:** Local embedding model that converts text into 1024-dimensional vectors.
**Why bge-large-en-v1.5:** Consistently top-ranked on the MTEB retrieval benchmark (the standard leaderboard for embedding models). 1024 dimensions captures more semantic nuance than smaller models.
**Why run locally:** No API cost per embedding. We embed thousands of chunks per meeting — API costs would add up. Also faster for batch operations (no network latency).
**Why not OpenAI text-embedding-3:** Costs money per token. We'd pay every time a user uploads a transcript.

### cross-encoder/ms-marco-MiniLM-L-6-v2 (re-ranker)
**What:** A cross-encoder model that scores how relevant a document is to a query.
**Why we need it:** Embedding models (bi-encoders) encode query and document separately. They're fast but approximate — they miss subtle relevance signals. A cross-encoder sees the query and document together and gives a much more accurate relevance score.
**Why this specific model:** Small (22M parameters), fast on CPU (~100ms for 20 candidates), trained on MS-MARCO (a massive passage retrieval dataset). Great accuracy/speed tradeoff.
**The tradeoff:** Too slow to run on all chunks (we'd need to score thousands). So we: embed → top-20 fast → rerank → top-5 precise.

---

## Task Queue

### Celery
**What:** Distributed task queue for Python.
**Why we use it:** After a user uploads a transcript, we need to: parse it, chunk it, embed all chunks, run two Groq API calls. This takes 15-60 seconds. We can't make the user wait for the HTTP response that long. Celery runs this work in a background worker process. The upload endpoint returns immediately; the worker processes asynchronously.
**Why not FastAPI BackgroundTasks:** BackgroundTasks runs in the same process as the API server. If the server restarts, the task is lost. No retry on failure. No visibility into task status. Fine for trivial tasks, wrong for 60-second AI pipelines.
**Why not threading:** Python's GIL limits true parallelism for CPU-bound work. Celery uses separate processes.

### Redis
**What:** In-memory data store. Used as Celery's message broker and result backend.
**Why:** Celery needs a "broker" — a place to put tasks so workers can pick them up. Redis is the most common choice: fast, simple, battle-tested with Celery.
**As broker:** FastAPI puts a task message into Redis → Celery worker picks it up → processes it.
**As result backend:** Stores task status (PENDING → STARTED → SUCCESS/FAILURE) so we can query it.

---

## Frontend

### Vanilla JS
**Why not React/Vue:** This is an internal tool with 3 pages. React adds ~150KB of JS, a build system (webpack/vite), and conceptual overhead (components, state management, hooks). For 3 pages of UI, vanilla JS with fetch() is simpler, faster to load, and easier to understand when learning.
**Rule:** If a feature requires more than 100 lines of vanilla JS, we reconsider. Until then, no framework.

### Canvas 2D API (for sentiment charts)
**Why not Chart.js:** Chart.js is 60KB. Our sentiment visualization is simple: horizontal bars + colored timeline blocks. This is ~40 lines of Canvas 2D code. No dependency needed.

---

## File Formats

### .vtt (WebVTT)
**What:** Web Video Text Tracks — a subtitle format with timestamps and optional speaker labels.
**Why support it:** Most meeting recording tools (Zoom, Google Meet, Teams) export transcripts in VTT format. It has structured timestamps (`00:03:12.000 --> 00:03:45.000`) which we use for precise chunking and sentiment segment labeling.

### .txt
**What:** Plain text. Speaker turns detected by regex (e.g., `Alice: ...` or `[Alice] ...`).
**Why support it:** Older tools and manual transcripts often come as plain text. Fallback to paragraph chunking if no speaker pattern detected.

---

## uv (Package Manager)
**What:** A Rust-based Python package installer and virtual environment manager from Astral.
**Why we use it over pip+venv:** `pip install -r requirements.txt` for this project (torch + sentence-transformers) takes 5-10 minutes. `uv pip install` takes 30-60 seconds for the same packages — it resolves and installs in parallel, uses a global cache, and is written in Rust so it's not bottlenecked by Python. It also creates virtual environments (`uv venv`) faster than `python -m venv`.
**Lock file:** `uv.lock` pins every transitive dependency to an exact version, like `package-lock.json` in Node. Ensures everyone on the team gets identical package versions.
**Drop-in:** We still keep `requirements.txt` — uv reads it identically to pip.

## Docker Compose
**What:** Defines and runs multi-container Docker apps.
**Why:** Instead of asking you to manually install PostgreSQL and Redis, docker-compose.yml starts both with one command: `docker compose up -d`. Reproducible, isolated, no system pollution.

---

## Questions & Concepts (Q&A)

### Q: Why bge-large-en-v1.5 instead of all-MiniLM-L6-v2? Most MVPs use MiniLM.

**Short answer:** MiniLM (384 dims, 22M params) is the default for quick prototypes. This build targets production-quality RAG, so we use bge-large (1024 dims, 335M params) which scores ~5 points higher on the MTEB retrieval benchmark.

The speed difference (~10x) doesn't matter here — embedding happens once per upload in a background task. A 30-min meeting (~200 chunks) embeds in under 1 second even with bge-large.

The quality difference does matter: meeting language is noisy and domain-specific. bge-large has more dimensions to encode that nuance, and its stronger semantic scores make the hybrid RRF merge more reliable.

Full comparison in `docs/model-choices.md`.

### Q: What if a user's question spans multiple parent chunks — or even multiple meetings?

The system handles this naturally. Here's the full flow:

1. The user's question is embedded → vector search finds the top 20 most similar **child** chunks across the entire meeting (or all meetings if it's a cross-meeting query)
2. Those 20 children may come from **different parent chunks** — that's expected and fine
3. After cross-encoder reranking to top 5, we fetch the **parent** of each winning child
4. The LLM receives 3–5 parent chunks (each ~1200 tokens) as context, from potentially different time windows or different meeting files

So if the answer lives at minute 2 and minute 47 of a meeting, retrieval surfaces one child from each window → two different parents are sent → the LLM synthesises an answer that references both.

**Cross-meeting queries** work identically — the top 5 children might come from 3 different transcript files. Each has its own parent. The citations (`[[meeting: file, time: HH:MM:SS, speaker: name]]`) in the LLM's answer tell the user exactly which meeting and moment each piece came from.

**Why doesn't this hit context limits?** Each parent is ~1200 tokens. 5 parents = ~6000 tokens of context. llama-3.3-70b on Groq supports 128k tokens. We have ~120k tokens of headroom.

**The design principle:** child chunks = retrieval precision, parent chunks = LLM context richness. The two levels decouple these concerns so you don't have to choose between them.
