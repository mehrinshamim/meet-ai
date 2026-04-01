

## Stack Rules (Approved Libraries Only)

### Backend
- Framework: `fastapi` only. No Flask, no Django.
- DB ORM: `sqlalchemy` (async mode) only. No raw psycopg2 queries except migrations.
- Migrations: `alembic` only. Never modify tables by hand.
- Task queue: `celery` with `redis` as broker. No BackgroundTasks for heavy work.
- LLM: `groq` SDK only. No anthropic, no openai direct.
- Embeddings: `sentence-transformers` only, run locally. No embedding API calls.
- File parsing: `webvtt-py` for .vtt, stdlib for .txt.
- Export: stdlib `csv` only. No pandas, no reportlab.

### Frontend
- Vanilla JS only. No React, no Vue, no jQuery.
- No external CSS frameworks (no Bootstrap, no Tailwind).
- One shared `api.js` for all fetch calls.
- No TypeScript (keep it simple for now).

### Database
- PostgreSQL with `pgvector` extension.
- Vectors: 1024 dimensions (bge-large-en-v1.5).
- Always use JSONB (not TEXT) for structured JSON fields.
- Every table must have `created_at TIMESTAMPTZ DEFAULT NOW()`.

## Code Rules
- All backend routes must be async (`async def`).
- No synchronous DB calls inside route handlers.
- All Groq calls live exclusively in `backend/services/ai.py`. Nowhere else.
- All embedding calls live exclusively in `backend/services/embeddings.py`. Nowhere else.
- Celery tasks live in `backend/tasks/`. One file per domain.
- No hardcoded strings — use constants or config.
- Environment variables via `.env` file + `python-dotenv`. Never commit secrets.

## RAG Rules (Critical)
- Always chunk at speaker-turn boundaries. Never fixed character splits.
- Always store parent + child chunks. Child for retrieval, parent for context.
- Always use hybrid retrieval (semantic + keyword). Never semantic-only.
- Always re-rank with cross-encoder before passing to LLM.
- Citations must include: meeting filename, timestamp, speaker name.
- Query reformulation required for follow-up questions.

## Development Rules
- Explain every concept as it is introduced (user is learning).
- Log every terminal command in `log.md`.
- Log every code change phase by phase in `log.md`.
- Check `PLAN.md` before starting any task and mark steps complete.
- Check `memory-bank/activeContext.md` to know what's in progress.
- Never add a library not in the approved list without asking.
- Write minimum code needed. No premature abstractions.
- No test files unless explicitly asked.

## Folder Structure
```
meetai/
├── CLAUDE.md           ← this file
├── PLAN.md             ← development checklist
├── log.md              ← command + change log
├── why.md              ← tool justification reference
├── memory-bank/        ← project context for Claude
│   ├── projectbrief.md
│   ├── techContext.md
│   ├── systemPatterns.md
│   ├── activeContext.md
│   └── progress.md
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── routes/
│   │   ├── meetings.py
│   │   ├── extractions.py
│   │   ├── chat.py
│   │   └── sentiment.py
│   ├── services/
│   │   ├── parser.py       # .txt/.vtt → speaker-turn chunks
│   │   ├── embeddings.py   # sentence-transformers, batch embed
│   │   ├── retrieval.py    # hybrid search + RRF + reranking
│   │   └── ai.py           # all Groq calls
│   ├── tasks/
│   │   ├── celery_app.py
│   │   └── pipeline.py     # upload processing pipeline
│   └── alembic/
├── frontend/
│   ├── index.html
│   ├── upload.html
│   ├── meeting.html
│   ├── css/style.css
│   └── js/
│       ├── api.js
│       ├── dashboard.js
│       ├── upload.js
│       └── meeting.js
├── .env.example
└── requirements.txt
```
