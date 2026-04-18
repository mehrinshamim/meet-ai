# MeetAI — Local Setup & Run Guide

## Prerequisites

- **Docker** + **Docker Compose** installed and running
- **uv** — fast Python package manager
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

---

## Step 0 — Check if ports are already in use

Before running setup, verify ports 5434 (PostgreSQL) and 6379 (Redis) are free:

```bash
# Check port 5434 (PostgreSQL)
lsof -i :5434

# Check port 6379 (Redis)
lsof -i :6379
```

If either command shows output, a process is already using that port.

**To free a port:**
```bash
# Find the PID from lsof output (e.g. PID 12345) and kill it:
kill -9 <PID>

# Or stop any existing Docker containers using those ports:
docker ps
docker stop <container_name>
```

---

## Step 1 — Run setup.sh

From the project root (`meetai/`):

```bash
bash setup.sh
```

This script does the following (automatically):
1. Checks that `uv` is installed
2. Creates `.venv/` virtual environment
3. Installs all Python dependencies (~1.5 GB on first run — be patient)
4. Copies `.env.example` → `.env`
5. Starts PostgreSQL and Redis via Docker
6. Waits for PostgreSQL to be healthy, then enables `pgvector` extension
7. Initializes Alembic (database migration tool) if not already done

---

## Step 2 — Set your API key

Open `.env` and set your Groq API key:

```bash
# Get a free key at https://console.groq.com
GROQ_API_KEY=gsk_your_actual_key_here
```

The `.env` file also sets DB and Redis URLs — defaults match Docker, no changes needed unless ports conflict.

---

## Step 3 — Run database migrations

```bash
uv run alembic upgrade head
```

This creates all tables in PostgreSQL. Run this once after setup, and again any time migrations are added.

---

## Step 4 — Open 2 terminals and start each service

You need **2 separate terminals** open at the same time from the `meetai/` directory.

### Terminal 1 — FastAPI (web server)

```bash
uv run uvicorn backend.main:app --reload --port 8000
```

- Serves the API at `http://localhost:8000`
- `--reload` restarts automatically when you edit code
- Docs at `http://localhost:8000/docs`

### Terminal 2 — Celery worker (background task processor)

```bash
uv run celery -A backend.tasks.celery_app.celery_app worker --loglevel=info
```

- Picks up meeting-processing jobs from the Redis queue
- Handles: parsing → chunking → embedding → storing vectors
- Must be running for uploaded meetings to get processed

---

## Quick reference — Services and ports

| Service    | Port  | Container name   | Notes                       |
|------------|-------|------------------|-----------------------------|
| FastAPI    | 8000  | (host process)   | `http://localhost:8000/docs`|
| PostgreSQL | 5434  | meetai_postgres  | mapped from internal 5432   |
| Redis      | 6379  | meetai_redis     | broker + result backend     |

---

## Verify everything is running

```bash
# Check Docker containers are up
docker ps

# Check PostgreSQL is accepting connections
docker exec meetai_postgres pg_isready -U meetai -d meetai

# Check Redis is alive
docker exec meetai_redis redis-cli ping   # should return: PONG

# Check FastAPI is responding
curl http://localhost:8000/health
```

---

## Stopping everything

```bash
# Stop FastAPI and Celery: Ctrl+C in each terminal

# Stop Docker services (preserves data):
docker compose down

# Stop Docker AND delete all data (full reset):
docker compose down -v
```

---

## Common issues

| Problem | Fix |
|---|---|
| `port is already in use` | Run `lsof -i :<port>` → `kill -9 <PID>` |
| `uv: command not found` | Install uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| PostgreSQL unhealthy | `docker logs meetai_postgres` to see errors |
| Tasks not processing | Make sure Terminal 2 (Celery worker) is running |
| `GROQ_API_KEY` errors | Check `.env` — key must start with `gsk_` |
| Migration errors | Run `uv run alembic upgrade head` again |
