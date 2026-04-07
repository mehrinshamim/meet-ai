# MeetAI — Meeting Intelligence Hub

## Project Title

**MeetAI — Meeting Intelligence Hub**

---

## The Problem

Meeting content gets buried in long transcripts that nobody reads. Teams waste time re-discussing decisions already made because there is no fast way to search across past meetings. Important decisions, action items, and context remain locked inside files that are rarely revisited.

---

## The Solution

MeetAI lets you upload meeting transcripts (`.txt` or `.vtt`), automatically extracts decisions and action items using an LLM, runs sentiment analysis per speaker, and provides a RAG-powered chatbot that answers questions like "Why did we delay the API launch?" by searching across all meeting transcripts and citing the exact timestamp and speaker where it was discussed.

Key features:
- **Automatic extraction** of decisions and action items from every uploaded transcript
- **Speaker-level sentiment analysis** across time segments
- **Hybrid RAG chatbot** — semantic + keyword retrieval, cross-encoder reranking, parent-child chunking, and structured citations with meeting filename, timestamp, and speaker name
- **Query reformulation** for follow-up questions to prevent retrieval failure on pronouns and implicit references

---

## Tech Stack

**Programming Languages**
- Python 3.11+
- JavaScript (Vanilla JS)
- HTML / CSS

**Frameworks**
- FastAPI — backend API
- Celery — async task queue for background processing

**Databases**
- PostgreSQL 16 with `pgvector` extension — stores meetings, chunks, embeddings, extractions, sentiments, chat history
- Redis 7 — Celery broker and result backend

**APIs and Third-Party Tools**
- [Groq](https://groq.com) — LLM inference (`llama-3.3-70b-versatile` for generation, `llama-3.1-8b-instant` for query reformulation)
- `sentence-transformers` — local embedding model (`BAAI/bge-large-en-v1.5`, 1024 dimensions) and cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`)
- `webvtt-py` — `.vtt` file parsing
- SQLAlchemy (async) — ORM
- Alembic — database migrations
- `python-dotenv` — environment variable management

---

## Setup Instructions

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager
- Docker and Docker Compose

### 1. Clone the repository

```bash
git clone <repo-url>
cd meetai
```

### 2. Run setup.sh


```bash
bash setup.sh
```

This will:
- Create a `.venv` virtual environment
- Install all Python dependencies (~1.5 GB on first run due to torch + sentence-transformers)
- Copy `.env.example` to `.env` if it doesn't exist
- Start PostgreSQL 16 (with `pgvector`) and Redis 7 via Docker
- Wait for PostgreSQL to be healthy and enable the `pgvector` extension
- Initialize Alembic for database migrations

### 3. Set your Groq API key

Open `.env` and fill in:

```
GROQ_API_KEY=your_groq_api_key_here
```

Get a free key at [console.groq.com](https://console.groq.com).

### 4. Run database migrations

```bash
uv run alembic upgrade head
```

### 5. Start the Celery worker

In a separate terminal:

```bash
uv run celery -A backend.tasks.celery_app worker --loglevel=info
```

### 6. Start the FastAPI server

```bash
uv run uvicorn backend.main:app --reload
```

The API will be available at `http://localhost:8000`.

The frontend can be served by opening the HTML files in the `frontend/` directory directly in a browser, or via any static file server pointed at that folder.
