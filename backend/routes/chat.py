"""
chat.py — Chat route: ask questions about meeting transcripts.

Routes:
  POST /api/chat              → ask a question, get an answer with citations
  GET  /api/chat/history      → return all messages for a session_id

How a chat turn works end-to-end:
  1. Client sends {question, meeting_id?, session_id?}
  2. Load prior chat history for this session (used for query reformulation).
  3. If history exists and question looks like a follow-up, rewrite it to
     a standalone question via retrieval.reformulate_query().
  4. Run the hybrid retrieval pipeline → top-5 parent-context blocks.
  5. Call Groq with the context + question → answer string with inline citations.
  6. Parse [[meeting: X, time: Y, speaker: Z]] citations into JSONB list.
  7. Save question + answer + citations to chat_messages table.
  8. Return the saved ChatOut object.

Why session_id?
  A session groups turns into a conversation thread so we can pass history
  to the reformulation step. It's a plain string (UUID or any client token).
  If the client doesn't send one, the server generates a UUID.

Why meeting_id is optional?
  meeting_id=None means "search across all meetings in the DB".
  meeting_id=42 means "restrict retrieval to chunks from meeting 42".
  The frontend toggles this with a "Scope: This meeting / All meetings" button.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import ChatMessage, Meeting
from backend.schemas import ChatHistoryOut, ChatOut, ChatRequest
from backend.services import ai
from backend.services.retrieval import retrieve

router = APIRouter()


# ─── POST /api/chat ────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatOut)
async def post_chat(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Ask a question about one or all meeting transcripts.

    Steps:
      1. Validate meeting_id if provided.
      2. Resolve or generate session_id.
      3. Load chat history for this session.
      4. Run retrieval (handles reformulation internally).
      5. Call Groq to generate an answer.
      6. Parse citations from the answer.
      7. Persist and return the Q&A turn.
    """
    # ── 1. Validate meeting_id ────────────────────────────────────────────────
    meeting_scope: str | None = None
    if body.meeting_id is not None:
        meeting = await db.get(Meeting, body.meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="Meeting not found.")
        if not meeting.processed:
            raise HTTPException(
                status_code=400,
                detail="Meeting has not been processed yet. Please wait for the pipeline to finish.",
            )
        meeting_scope = meeting.filename

    # ── 2. Resolve session_id ─────────────────────────────────────────────────
    session_id = body.session_id or str(uuid.uuid4())

    # ── 3. Load prior chat history for this session ───────────────────────────
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    prior_messages = result.scalars().all()

    # Convert ORM rows to the dict format retrieval.py expects:
    # [{"question": str, "answer": str}, ...]
    chat_history = [
        {"question": m.question, "answer": m.answer}
        for m in prior_messages
    ]

    # ── 4. Hybrid retrieval (reformulation + search + rerank) ─────────────────
    retrieval_result = await retrieve(
        query=body.question,
        session=db,
        meeting_id=body.meeting_id,
        chat_history=chat_history if chat_history else None,
    )

    # ── 5. Generate answer via Groq ───────────────────────────────────────────
    # answer_question is synchronous (Groq SDK) — run as-is, FastAPI handles it fine
    # in an async route because the Groq call is I/O-bound and usually fast.
    answer = ai.answer_question(
        question=retrieval_result.reformulated_query,
        context_blocks=retrieval_result.context_blocks,
        meeting_scope=meeting_scope,
    )

    # ── 6. Parse citations ────────────────────────────────────────────────────
    citations = ai.parse_citations(answer)

    # ── 7. Persist Q&A turn ───────────────────────────────────────────────────
    message = ChatMessage(
        session_id=session_id,
        meeting_id=body.meeting_id,
        question=body.question,       # store original, not reformulated
        answer=answer,
        citations=citations if citations else None,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    return message


# ─── GET /api/chat/history ─────────────────────────────────────────────────────

@router.get("/chat/history", response_model=ChatHistoryOut)
async def get_chat_history(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Return all Q&A turns for a given session_id, oldest first.

    Used by the frontend to reconstruct the conversation thread on page load
    or after a browser refresh.

    Returns 404 if no messages exist for this session_id.
    """
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()

    if not messages:
        raise HTTPException(
            status_code=404,
            detail=f"No chat history found for session '{session_id}'.",
        )

    return ChatHistoryOut(
        session_id=session_id,
        messages=messages,
    )
