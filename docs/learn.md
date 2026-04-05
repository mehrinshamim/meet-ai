# learn.md — Concepts Explained While Building MeetAI

> Every concept introduced during development is explained here.
> Updated phase by phase as new code is written.

---

## Phase 0 — Project Setup

### `__init__.py` files
Python requires these empty files to treat a folder as a "package" — something you can import from. Without them, `from backend.services.ai import something` would throw a `ModuleNotFoundError`. They contain no code; they're just markers that say "this folder is a Python module."

### Virtual Environment (`.venv/`)
A virtual environment is an isolated Python installation just for this project. Without it, every Python project on your machine shares the same packages — and they'll conflict. For example, Project A needs `sqlalchemy==1.4`, Project B needs `sqlalchemy==2.0`. With a venv, each project has its own copy of every package. You activate it with `source .venv/bin/activate` and from then on, `python` and `pip` refer to the isolated versions.

### uv (Package Manager)
`uv` is a Rust-based replacement for `pip` + `python -m venv`. It does the same thing but 10–100x faster because it's written in Rust (a compiled systems language), resolves dependencies in parallel, and uses a local cache so packages you've installed before don't re-download. This matters here because `torch` + `sentence-transformers` is ~1.5GB — pip takes 8+ minutes, uv takes ~45 seconds.

`uv.lock` is like `package-lock.json` in JavaScript — it pins every dependency (including indirect ones) to exact versions so that `pip install` on any machine produces the identical environment.

### `.env` and `python-dotenv`
A `.env` file stores secrets (API keys, database passwords) as key=value pairs. You never commit this to git — it's in `.gitignore`. Instead, `.env.example` is committed as a template showing which variables exist (without the actual values). `python-dotenv` reads the `.env` file and loads its contents into `os.environ` at runtime. `config.py` then reads from `os.environ` so the rest of the app never touches the `.env` file directly.

### Docker & Docker Compose
Docker runs software in isolated "containers" — think of them as lightweight virtual machines that bundle the application and everything it needs. Without Docker, you'd have to manually install PostgreSQL and Redis on your machine, which pollutes your system and can break other projects. `docker-compose.yml` describes multiple containers and starts them all with one command: `docker compose up -d`. The `-d` flag means "detached" — runs in the background.

### Docker Volumes (`postgres_data`, `redis_data`)
By default, everything inside a container is wiped when it stops. Volumes are a way to persist data to your real disk so it survives restarts. `postgres_data` maps to the folder inside the container where PostgreSQL stores its database files. Without it, your entire database would disappear every time you ran `docker compose down`.

### Healthchecks in Docker Compose
The `healthcheck` block tells Docker to periodically run a command inside the container and check if it succeeds. PostgreSQL takes a few seconds to start — without a healthcheck, another service might try to connect before it's ready and crash. `pg_isready` is a built-in PostgreSQL utility that returns success (exit code 0) when the server is accepting connections.

### pgvector (PostgreSQL Extension)
PostgreSQL extensions add new data types and functions to the database. `pgvector` adds a `vector` column type and operators like `<=>` (cosine distance), `<->` (L2 distance), and `<#>` (inner product). This lets us store 1024-dimensional embedding vectors as a column and run similarity queries like "find the 20 chunks most similar to this query vector" entirely inside PostgreSQL — no separate vector database needed.

### tsvector (Full-Text Search)
`tsvector` is PostgreSQL's built-in full-text search type. When you store a column as `tsvector`, PostgreSQL tokenizes the text, removes stop words ("the", "a", "is"), and stems words ("running" → "run"). A GIN index on this column makes keyword queries like `ts_vector @@ to_tsquery('decision & api')` very fast. We use this alongside semantic search for hybrid retrieval.

### Async SQLAlchemy Engine
"Async" means non-blocking. When FastAPI sends a query to PostgreSQL, it has to wait for the result — that's I/O (network) time. Synchronous code freezes during that wait, handling nothing else. Async code yields control back to the event loop while waiting, so FastAPI can handle other incoming requests simultaneously. The `create_async_engine` function creates a connection pool that works with Python's `asyncio` event loop. `asyncpg` is the driver (the actual PostgreSQL client library) that supports this async mode.

### SQLAlchemy Session
A session is a "unit of work" with the database. You open one, perform reads and writes, then commit (save to disk) or rollback (discard). Each HTTP request gets its own session — they're independent. The `get_db()` function in `database.py` is a FastAPI dependency: it opens a session before the route handler runs and closes it after, even if an exception is thrown.

### `pool_pre_ping=True`
A database connection can go stale — if the DB restarts or the network hiccups, the connection object in your pool still exists in Python but is actually broken. `pool_pre_ping=True` makes SQLAlchemy send a lightweight "ping" before using a pooled connection to check if it's still alive. If it's not, it discards it and creates a fresh one. Prevents mysterious `connection closed` errors.

### Alembic (Database Migrations)
When your app evolves, you need to change the database schema — add a column, rename a table, add an index. You can't just edit `models.py` and restart; the actual PostgreSQL database won't change. Alembic generates Python migration scripts that describe schema changes. Each script has an `upgrade()` function (apply the change) and a `downgrade()` function (undo it). Alembic tracks which migrations have run in a `alembic_version` table. `alembic upgrade head` applies all pending migrations. Think of it as git, but for your database schema.

### `.gitignore`
Git tracks every file in your project by default. `.gitignore` tells git which files and folders to ignore. The most important entries: `.env` (secrets — never commit), `.venv/` (reproducible from `requirements.txt`, no need to commit 500MB of packages), `__pycache__/` (compiled Python bytecode, auto-generated, machine-specific). `CLAUDE.md` is also gitignored — it's the AI assistant's config file, not useful to other developers.

### `CONTRIBUTING.md` vs `CLAUDE.md`
`CLAUDE.md` contains rules written for the AI assistant ("Claude must...", "never do X"). `CONTRIBUTING.md` contains the same project conventions written for human developers ("use async routes", "all LLM calls go in ai.py"). We commit `CONTRIBUTING.md` (useful to any contributor) and gitignore `CLAUDE.md` (internal AI config, would reveal the tooling used).

### `set -e` in shell scripts
`set -e` at the top of a bash script means "exit immediately if any command returns a non-zero exit code (i.e., fails)." Without it, the script continues running even after an error, potentially doing more damage. With it, if `docker compose up -d` fails, the script stops and shows the error rather than silently proceeding to the next step.

---

## Phase 1 — Database & Models

### SQLAlchemy ORM Models
Instead of writing raw SQL `CREATE TABLE` statements, we describe our tables as Python classes. SQLAlchemy maps these classes to database tables. Each class attribute is a column. This is called an ORM (Object-Relational Mapper) — it translates between Python objects and database rows.

```python
class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

`Mapped[int]` is a type hint that tells both Python and SQLAlchemy the column type. `mapped_column(...)` is where you set the actual database constraints (nullable, default, etc.).

### `server_default=func.now()` vs `default=datetime.now`
These look similar but behave very differently:
- `default=datetime.now` — Python sets the value when you create the object. The value is generated in your application code.
- `server_default=func.now()` — PostgreSQL sets the value using `DEFAULT NOW()`. The database fills it in when the row is inserted.

We always use `server_default` for `created_at` because it's more reliable — even if rows are inserted directly via SQL (not through our app), they still get a timestamp.

### `BigInteger` vs `Integer` for primary keys
`Integer` is 32-bit (max ~2.1 billion rows). `BigInteger` is 64-bit (max ~9.2 quintillion rows). For a new project this doesn't matter right now, but it's a cheap choice that avoids a painful migration later if the app ever scales. Always use `BigInteger` for primary keys.

### `JSONB` columns
PostgreSQL has two JSON column types: `JSON` and `JSONB`. `JSONB` stores the data in a binary parsed format:
- Faster to read (no re-parsing)
- Supports GIN indexes for querying inside the JSON
- Deduplicates object keys

We use `JSONB` everywhere we store structured data (decisions, action items, citations, sentiment scores).

```python
decisions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
# Stores: [{"text": "...", "timestamp": "00:05:12", "speaker": "Alice"}]
```

### `ON DELETE CASCADE` vs `ON DELETE SET NULL`
When a parent row is deleted, what happens to child rows that reference it?

- `CASCADE` — delete the children too. Used for `chunks`, `extractions`, `sentiments`: delete a meeting → delete all its data.
- `SET NULL` — set the foreign key to NULL instead. Used for `meetings.project_id`: delete a project → orphan the meetings, don't delete them.

```python
# chunks are owned by a meeting — delete meeting → delete chunks
meeting_id: Mapped[int] = mapped_column(
    BigInteger, ForeignKey("meetings.id", ondelete="CASCADE")
)

# meetings belong to a project — delete project → just unassign them
project_id: Mapped[int | None] = mapped_column(
    BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
)
```

### Parent-Child Chunk Pattern
We store chunks at two levels:

- **Child chunks** (~400 tokens): small segments retrieved by vector search. Small = precise matches.
- **Parent chunks** (~5-minute windows): larger context around a child chunk. Fetched after retrieval to give the LLM more surrounding text.

Why both? Tiny chunks = precise retrieval but no context. Huge chunks = rich context but poor precision. This pattern gets both.

```python
class Chunk(Base):
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIM), nullable=True)
    # Only child chunks get embeddings. Parents are fetched by ID after retrieval.
```

### Alembic `env.py` — Async Rewrite
The default `env.py` uses a synchronous engine. Our app uses `asyncpg`. We must use `async_engine_from_config` + `asyncio.run()` so migrations use the same driver.

```python
async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no persistent pool during migrations
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())
```

`pool.NullPool` — don't keep connections open after the migration finishes. Correct for a one-shot script.

### Alembic Autogenerate
`alembic revision --autogenerate -m "message"` compares what's in `Base.metadata` (your Python models) against what's actually in the database, and generates a Python migration script with the diff. It handles `CREATE TABLE`, `ADD COLUMN`, `DROP COLUMN` automatically. You still review and edit the output — it doesn't detect raw SQL indexes or custom extensions.

### IVFFlat Index (Vector Search)
`IVFFlat` = Inverted File with Flat quantization. pgvector's index for approximate nearest-neighbor search.

1. At build time: clusters vectors into `lists` groups (we use 100).
2. At query time: finds the closest cluster centroids, searches only those.

Much faster than scanning all rows, with minor recall tradeoff — acceptable for our use case.

```sql
CREATE INDEX chunks_embedding_idx ON chunks
USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

`vector_cosine_ops` = use cosine distance, which matches how bge-large-en-v1.5 embeddings are designed to be compared.

### GIN Index (Full-Text Search)
`GIN` = Generalized Inverted Index. For a `tsvector` column, it maps each token → list of rows containing it. Makes `WHERE search_vector @@ to_tsquery('decision & budget')` fast — no full table scan.

```sql
CREATE INDEX chunks_search_vector_idx ON chunks USING gin(search_vector);
```

We populate `search_vector` during the embedding pipeline (Phase 3) using `to_tsvector('english', text)`.

### Why Both Indexes (Hybrid Retrieval)
- **Vector index**: finds semantically similar chunks even with different words. "Budget cuts" → retrieves "cost reduction" chunks.
- **GIN index**: finds exact keyword matches. "Q3 budget" → retrieves chunks with those exact words.

Combining both with Reciprocal Rank Fusion (RRF) + cross-encoder reranking gives better recall than either alone — this is the core of the RAG system.

## Phase 2 — File Parser + Chunker
_To be added when Phase 2 begins._

## Phase 3 — Embedding Pipeline
_To be added when Phase 3 begins._

## Phase 4 — Celery Pipeline
_To be added when Phase 4 begins._

## Phase 5 — Upload Routes
_To be added when Phase 5 begins._

## Phase 6 — RAG Query Engine
_To be added when Phase 6 begins._

## Phase 7 — Chat Route
_To be added when Phase 7 begins._

## Phase 2 — File Parser + Chunker

### WebVTT format
WebVTT (Web Video Text Tracks) is the standard subtitle/caption format used by Zoom, Google Meet, and Microsoft Teams exports. Each block is called a **cue** and contains a time range and text. The `<v Speaker>` tag is the W3C-standard way to encode who is speaking. The `webvtt-py` library parses these files and gives you a list of cue objects — `caption.start`, `caption.end`, `caption.text`. Important: `caption.text` strips the `<v>` tags, so you must read the speaker from `caption.raw_text` instead.

### Why we use a tempfile for VTT parsing
`webvtt-py` 0.5.x `read_buffer()` is unreliable with StringIO. Instead, we write the content to a `NamedTemporaryFile` (a real file that auto-deletes), pass its path to `webvtt.read()`, then delete it. This is a common pattern when a library only accepts file paths, not in-memory buffers.

### Chunking — why not split by character count?
Splitting a transcript at fixed character intervals (e.g. every 1000 characters) will cut mid-sentence or mid-thought, producing fragments with no clear meaning. The embedding of "...and that is why we" is essentially meaningless. We always split at speaker-turn boundaries so each chunk is a complete thought.

### Token count estimation
A "token" is roughly a word-fragment used by LLM tokenisers. On average, one English word ≈ 1.3 tokens. We estimate `len(text.split()) * 1.3` rather than importing a tokeniser (which would be an extra dependency). This is accurate enough for chunking decisions — we don't need exact counts, just a rough size signal.

### Parent-child chunk pattern
This is the most important RAG design decision in the project. Child chunks (~400 tokens) are small enough that their embeddings are semantically precise — they represent one focused idea. But 400 tokens is often too little context for the LLM to write a complete answer. So we also store parent chunks (5-minute windows, ~1200 tokens), which are the full surrounding conversation. At query time: **vector search uses children** (precise), **LLM receives parents** (rich context). The child's `parent_id` in the database links them.

### Dataclass
A Python `@dataclass` automatically generates `__init__`, `__repr__`, and `__eq__` methods from the class fields. It's like a struct — a simple container for data with no behaviour. We use them for `Turn`, `ChunkData`, and `ParseResult` to get clean, readable data objects without writing boilerplate constructors.

### PLAN.md step: "Test: parse a sample .vtt and .txt, verify chunk output"
From Phase 2 onward, every component has a dedicated manual test file in `tests/manual/`. Run them with `uv run python tests/manual/test_<name>.py`. Each test prints PASS/FAIL and exits with code 1 on any failure. This lets you verify a single phase in isolation before moving to the next.

## Phase 8 — Extractions + Sentiment Routes
_To be added when Phase 8 begins._

## Phase 9 — Frontend
_To be added when Phase 9 begins._
