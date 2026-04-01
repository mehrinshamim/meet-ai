# Contributing to MeetAI

## Tech Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI (async) |
| Database | PostgreSQL 16 + pgvector |
| Task Queue | Celery + Redis |
| LLM | Groq API |
| Embeddings | sentence-transformers (local, no API) |
| Frontend | Vanilla JS, no framework |

## Getting Started

```bash
# Requires: Docker, uv (https://astral.sh/uv)
bash setup.sh

# Activate the virtual environment
source .venv/bin/activate

# Set your Groq API key in .env
# Get one free at https://console.groq.com

# Start the API server
uvicorn backend.main:app --reload
```

## Approved Libraries

New dependencies require discussion before being added. The current approved set is in `requirements.txt`.

### Backend — do not add alternatives to these
- **FastAPI** — web framework. Not Flask, not Django.
- **SQLAlchemy (async)** — ORM. Use async sessions only.
- **Alembic** — migrations. Never modify tables by hand.
- **Celery + Redis** — async task queue. Don't use FastAPI BackgroundTasks for heavy work.
- **Groq SDK** — LLM inference.
- **sentence-transformers** — local embeddings. No embedding API calls.
- **webvtt-py** — VTT parsing. stdlib for .txt.
- stdlib `csv` — exports. No pandas, no reportlab.

### Frontend
- Vanilla JS only — no React, Vue, or jQuery.
- No CSS frameworks — no Bootstrap, no Tailwind.
- All API calls go through `frontend/js/api.js`.

## Code Conventions

- All route handlers must be `async def`.
- All Groq/LLM calls live in `backend/services/ai.py` only.
- All embedding calls live in `backend/services/embeddings.py` only.
- Celery tasks live in `backend/tasks/`.
- Structured JSON fields in PostgreSQL use `JSONB`, not `TEXT`.
- Every table has `created_at TIMESTAMPTZ DEFAULT NOW()`.
- No hardcoded strings — use `backend/config.py` for env vars.

## RAG Architecture

This project uses a hybrid retrieval pipeline. Key rules:

- Chunk transcripts at **speaker-turn boundaries**, not fixed character counts.
- Store **parent + child chunks**: child for embedding/retrieval, parent for LLM context.
- Always use **hybrid retrieval**: pgvector cosine similarity + PostgreSQL tsvector keyword search, merged with RRF.
- Always **re-rank** with a cross-encoder before passing results to the LLM.
- Citations must include: meeting filename, timestamp, and speaker name.
- Follow-up chat questions must be **reformulated** into standalone queries before retrieval.

## Project Structure

```
meetai/
├── backend/
│   ├── main.py
│   ├── config.py           # env vars
│   ├── database.py         # async engine + session
│   ├── models.py           # ORM models
│   ├── schemas.py          # Pydantic schemas
│   ├── routes/             # API endpoints
│   ├── services/
│   │   ├── parser.py       # .txt/.vtt → speaker-turn chunks
│   │   ├── embeddings.py   # batch embedding
│   │   ├── retrieval.py    # hybrid search + reranking
│   │   └── ai.py           # all LLM calls
│   └── tasks/              # Celery tasks
├── frontend/
│   ├── index.html          # dashboard
│   ├── upload.html         # file upload
│   ├── meeting.html        # meeting detail + chatbot
│   ├── css/style.css
│   └── js/
│       ├── api.js          # shared fetch wrapper
│       ├── dashboard.js
│       ├── upload.js
│       └── meeting.js
├── docker-compose.yml      # PostgreSQL + Redis
├── requirements.txt
├── setup.sh                # bootstrap script
└── .env.example
```
