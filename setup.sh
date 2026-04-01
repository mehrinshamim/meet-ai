#!/usr/bin/env bash
# MeetAI — one-command environment bootstrap
# Usage: bash setup.sh
set -e  # exit immediately if any command fails

echo "=== MeetAI Setup ==="
echo ""

# ── 1. Check for uv ──────────────────────────────────────────────────────────
# uv is a fast Python package manager written in Rust.
# It replaces: python -m venv, pip install, pip freeze, pip-tools — all in one tool.
if ! command -v uv &> /dev/null; then
    echo "ERROR: uv is not installed."
    echo ""
    echo "Install it with:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "Then re-run: bash setup.sh"
    exit 1
fi

echo "[1/7] uv found: $(uv --version)"

# ── 2. Create virtual environment ────────────────────────────────────────────
# A virtual environment is an isolated Python installation just for this project.
# It prevents your packages from clashing with other Python projects on your machine.
# uv creates it much faster than the built-in: python -m venv .venv
if [ ! -d ".venv" ]; then
    echo "[2/7] Creating virtual environment (.venv/)..."
    uv venv .venv
else
    echo "[2/7] Virtual environment already exists, skipping."
fi

# ── 3. Install dependencies ──────────────────────────────────────────────────
# uv reads requirements.txt and installs everything into .venv.
# First run will download ~1.5GB (torch + sentence-transformers models).
# uv is 10-100x faster than pip so this is as fast as it can get.
echo "[3/7] Installing Python dependencies (first run downloads ~1.5GB, be patient)..."
uv pip install -r requirements.txt

# ── 4. Set up .env ───────────────────────────────────────────────────────────
# .env holds secrets (API keys, DB passwords). Never committed to git.
# .env.example is committed — it's a template showing which variables are needed.
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "[4/7] Created .env from .env.example"
    echo ""
    echo "  >>> ACTION REQUIRED: Open .env and set your GROQ_API_KEY"
    echo "  >>> Get one free at: https://console.groq.com"
    echo ""
else
    echo "[4/7] .env already exists, skipping."
fi

# ── 5. Start Docker services (PostgreSQL + Redis) ────────────────────────────
# Docker runs PostgreSQL and Redis in isolated containers.
# 'up -d' means: start in detached mode (background, not blocking the terminal).
echo "[5/7] Starting Docker services (PostgreSQL + Redis)..."
docker compose up -d

# ── 6. Wait for PostgreSQL to be ready ───────────────────────────────────────
# PostgreSQL takes a few seconds to initialize on first start.
# pg_isready is a built-in PostgreSQL tool that checks if the server accepts connections.
# We poll it every second for up to 30 seconds.
echo "[6/7] Waiting for PostgreSQL to be healthy..."
for i in $(seq 1 30); do
    if docker exec meetai_postgres pg_isready -U meetai -d meetai &> /dev/null; then
        echo "      PostgreSQL is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: PostgreSQL did not become ready in 30 seconds."
        echo "Check logs with: docker logs meetai_postgres"
        exit 1
    fi
    sleep 1
done

# Enable pgvector extension inside PostgreSQL.
# pgvector adds a 'vector' column type and similarity search operators.
# 'IF NOT EXISTS' makes this safe to run multiple times.
docker exec meetai_postgres psql -U meetai -d meetai \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" > /dev/null
echo "      pgvector extension enabled."

# ── 7. Initialize Alembic ────────────────────────────────────────────────────
# Alembic manages database schema changes (migrations).
# Think of it as git for your database structure.
# We only initialize once — subsequent runs use 'alembic upgrade head'.
if [ ! -f "alembic.ini" ]; then
    echo "[7/7] Initializing Alembic..."
    .venv/bin/alembic init backend/alembic
    echo "      Alembic initialized."
else
    echo "[7/7] Alembic already initialized, skipping."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                     ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Next steps:                                         ║"
echo "║                                                      ║"
echo "║  1. source .venv/bin/activate   (activate venv)     ║"
echo "║  2. Edit .env → set GROQ_API_KEY                    ║"
echo "║  3. uvicorn backend.main:app --reload               ║"
echo "║                                                      ║"
echo "║  Services running:                                   ║"
echo "║    PostgreSQL → localhost:5432                       ║"
echo "║    Redis      → localhost:6379                       ║"
echo "╚══════════════════════════════════════════════════════╝"
