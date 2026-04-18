"""
pipeline.py — Main Celery task: parse → chunk → embed → Groq → store → done.

This file orchestrates everything Phase 2 and Phase 3 built.  It is the
heart of the backend processing pipeline.

Flow:
  process_meeting(meeting_id, filename, content)
    │
    ├─ Step 1: parse_transcript() → ParseResult
    │          (pure Python, no DB, no network)
    │
    ├─ Step 2: _store_and_embed(meeting_id, parse_result) [async]
    │          - INSERT parent chunks → get parent DB IDs
    │          - INSERT child chunks (with parent_id FK) → get child DB IDs
    │          - UPDATE meeting row (speaker_names, word_count, meeting_date)
    │          - embed_and_store() → writes embedding + tsvector to chunk rows
    │          - Returns child_ids + chunk_segments (list of dicts for Groq)
    │
    ├─ Step 3: extract_decisions_and_actions() [Groq, sync]
    │          Sends full transcript text, gets back decisions + action items JSON
    │
    ├─ Step 4: analyze_sentiment() [Groq, sync]
    │          Sends numbered chunk segments, gets back speaker + segment scores JSON
    │
    └─ Step 5: _store_results(meeting_id, ...) [async]
               - Map segment_index → chunk_id for segment_scores
               - INSERT into extractions table
               - INSERT into sentiments table
               - UPDATE meeting.processed = True

Why sync Celery task with async DB calls?
  Celery workers run in regular Python processes — there is no running event loop.
  Our SQLAlchemy engine is async (asyncpg).  To bridge these two worlds we use
  asyncio.run(), which creates a fresh event loop for each async function call.
  This is correct and safe because each call is fully isolated: no shared state
  between asyncio.run() calls.

Why separate _store_and_embed from _store_results?
  Groq calls (Steps 3 + 4) are synchronous and can take 5-15 seconds.
  Holding a DB connection open during that wait wastes a connection-pool slot.
  Separating the DB operations into two async functions means the connection
  is released between Steps 2 and 5.
"""

from __future__ import annotations

import asyncio
import logging

from backend.database import AsyncSessionLocal
from backend.models import Chunk, Extraction, Meeting, Sentiment
from backend.services.ai import analyze_sentiment, extract_decisions_and_actions
from backend.services.embeddings import embed_and_store
from backend.services.parser import parse_transcript
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


# ─── Async DB helpers ─────────────────────────────────────────────────────────

async def _store_and_embed(
    meeting_id: int,
    parse_result,
) -> tuple[list[int], list[dict]]:
    """
    Insert parent + child chunks, update meeting metadata, embed child chunks.

    Returns:
        child_ids:      list of DB primary keys for the inserted child chunks
        chunk_segments: list of dicts used for sentiment analysis and result mapping:
                        {"index", "chunk_id", "speaker", "start_time", "text"}

    Transaction strategy:
        We flush after each INSERT to obtain the auto-generated primary key.
        flush() sends SQL to PostgreSQL but does not commit — the entire
        function runs in a single transaction that commits at the end.
        If anything fails, the whole transaction rolls back automatically.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():  # auto-commit on exit, auto-rollback on error

            # ── Insert parent chunks (5-minute windows) ───────────────────────
            # Parents must be inserted first so we have their DB IDs ready to
            # assign as foreign keys on child chunks.
            parent_db_ids: list[int] = []
            for parent_data in parse_result.parent_chunks:
                parent_row = Chunk(
                    meeting_id=meeting_id,
                    parent_id=None,         # parents have no parent
                    is_parent=True,
                    speaker=parent_data.speaker,
                    start_time=parent_data.start_time,
                    end_time=parent_data.end_time,
                    text=parent_data.text,
                    token_count=parent_data.token_count,
                )
                session.add(parent_row)
                await session.flush()       # ← writes INSERT, gets auto ID
                parent_db_ids.append(parent_row.id)

            # ── Insert child chunks ────────────────────────────────────────────
            # Each child chunk knows which 5-min parent window it belongs to
            # via parse_result.child_chunks[i].parent_index (an index into
            # parent_db_ids, not a DB ID).  We resolve it here.
            child_db_ids: list[int] = []
            child_texts: list[str] = []
            chunk_segments: list[dict] = []

            for i, child_data in enumerate(parse_result.child_chunks):
                # Resolve parent_index → actual DB ID
                parent_db_id: int | None = None
                if child_data.parent_index is not None:
                    parent_db_id = parent_db_ids[child_data.parent_index]

                child_row = Chunk(
                    meeting_id=meeting_id,
                    parent_id=parent_db_id,
                    is_parent=False,
                    speaker=child_data.speaker,
                    start_time=child_data.start_time,
                    end_time=child_data.end_time,
                    text=child_data.text,
                    token_count=child_data.token_count,
                )
                session.add(child_row)
                await session.flush()       # ← get the auto-generated ID

                child_db_ids.append(child_row.id)
                child_texts.append(child_data.text)
                chunk_segments.append({
                    "index":      i,
                    "chunk_id":   child_row.id,
                    "speaker":    child_data.speaker or "Unknown",
                    "start_time": child_data.start_time or "",
                    "text":       child_data.text,
                })

            # ── Update meeting metadata ────────────────────────────────────────
            # The meeting row was created by the upload route (Phase 5) with
            # just the filename.  Now we have the full metadata from parsing.
            meeting = await session.get(Meeting, meeting_id)
            if meeting:
                meeting.speaker_names = parse_result.speaker_names
                meeting.word_count = parse_result.word_count
                meeting.meeting_date = parse_result.meeting_date

            # ── Embed child chunks ─────────────────────────────────────────────
            # embed_and_store() runs the bge model on child_texts, then writes
            # each vector + tsvector to the corresponding chunk row.
            # Parents are NOT embedded — they're only fetched for LLM context.
            await embed_and_store(session, child_db_ids, child_texts)

            # session.begin() auto-commits here

    logger.info(
        "Stored %d parent + %d child chunks for meeting %d",
        len(parent_db_ids), len(child_db_ids), meeting_id,
    )
    return child_db_ids, chunk_segments


async def _store_results(
    meeting_id: int,
    extractions: dict,
    sentiment_data: dict,
    chunk_segments: list[dict],
) -> None:
    """
    Write Groq results to the DB and mark the meeting as processed.

    Correlates Groq's segment_index values back to real chunk IDs using
    the same chunk_segments list that was passed to analyze_sentiment().
    """
    # Build an index: segment_index (int) → chunk_id (int)
    index_to_chunk_id: dict[int, int] = {
        seg["index"]: seg["chunk_id"] for seg in chunk_segments
    }
    # Also map index → speaker + start_time (for segment_scores JSONB rows)
    index_to_meta: dict[int, dict] = {
        seg["index"]: {"speaker": seg["speaker"], "start_time": seg["start_time"]}
        for seg in chunk_segments
    }

    # Build segment_scores with chunk_ids attached
    # (Groq only returns segment_index — we add the real chunk_id here)
    segment_scores: list[dict] = []
    for seg in sentiment_data.get("segment_scores", []):
        idx = seg.get("segment_index")
        if idx is None:
            continue
        chunk_id = index_to_chunk_id.get(idx)
        meta = index_to_meta.get(idx, {})
        segment_scores.append({
            "chunk_id":   chunk_id,
            "speaker":    meta.get("speaker"),
            "start_time": meta.get("start_time"),
            "score":      seg.get("score", 0.0),
            "label":      seg.get("label", "neutral"),
        })

    async with AsyncSessionLocal() as session:
        async with session.begin():

            # ── Extractions ────────────────────────────────────────────────────
            extraction_row = Extraction(
                meeting_id=meeting_id,
                decisions=extractions.get("decisions") or [],
                action_items=extractions.get("action_items") or [],
            )
            session.add(extraction_row)

            # ── Sentiment ──────────────────────────────────────────────────────
            sentiment_row = Sentiment(
                meeting_id=meeting_id,
                speaker_scores=sentiment_data.get("speaker_scores") or {},
                segment_scores=segment_scores,
            )
            session.add(sentiment_row)

            # ── Mark meeting processed ─────────────────────────────────────────
            meeting = await session.get(Meeting, meeting_id)
            if meeting:
                meeting.processed = True
                meeting.error = None


async def _mark_error(meeting_id: int, error_message: str) -> None:
    """
    Set meeting.error when the pipeline task fails.

    Called in the except block of process_meeting so the status endpoint
    can surface the error to the user instead of showing "still processing".
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            meeting = await session.get(Meeting, meeting_id)
            if meeting:
                meeting.processed = False
                meeting.error = error_message[:1000]    # truncate very long tracebacks


# ─── Transcript text builder ──────────────────────────────────────────────────

def _build_transcript_text(parse_result) -> str:
    """
    Concatenate parent chunks into a single readable transcript string.

    Parent chunk text is already formatted as:
        "Alice: sentence one\nBob: sentence two\n..."
    (built by build_parent_chunks() in parser.py)

    We join parent windows with a blank line so the LLM can see paragraph breaks.
    """
    return "\n\n".join(pc.text for pc in parse_result.parent_chunks)


# ─── Async pipeline orchestrator ─────────────────────────────────────────────

async def _run_pipeline(
    meeting_id: int,
    filename: str,
    parse_result,
) -> tuple[list[int], list[dict], dict, dict]:
    """
    Single-event-loop orchestrator for all async pipeline work.

    Why one function instead of two asyncio.run() calls?
      asyncio.run() creates a brand-new event loop each time and destroys it
      when done.  asyncpg connections are stamped with the loop they were
      created in — if you call asyncio.run() twice, the second call runs in
      a new loop but finds stale connections from the dead first loop, causing:
      "Future attached to a different loop".

      The fix: run everything inside a single asyncio.run() so there is only
      ever one event loop.  Blocking Groq calls are offloaded to a thread pool
      via run_in_executor() — they run in a background thread without blocking
      the event loop, and the event loop can do other work while waiting.

    Connection release between DB steps:
      _store_and_embed() opens a session with "async with AsyncSessionLocal()",
      which closes (and returns) the connection when the context exits.  By the
      time we reach run_in_executor(), the DB connection is already released.
      We get the "don't hold a connection during Groq calls" benefit without
      needing two separate event loops.
    """
    loop = asyncio.get_running_loop()

    # ── Step 1: DB writes + embeddings ────────────────────────────────────────
    child_ids, chunk_segments = await _store_and_embed(meeting_id, parse_result)
    # DB connection is released here — _store_and_embed's session context exited.

    # ── Step 2: Groq calls in thread pool ─────────────────────────────────────
    # run_in_executor(None, fn, *args) runs fn(*args) in the default
    # ThreadPoolExecutor.  "await" suspends this coroutine until the thread
    # finishes, but the event loop stays alive and can handle other tasks.
    full_transcript = _build_transcript_text(parse_result)
    extractions = await loop.run_in_executor(
        None, extract_decisions_and_actions, full_transcript, filename
    )
    sentiment_data = await loop.run_in_executor(
        None, analyze_sentiment, chunk_segments
    )

    # ── Step 3: Store results ─────────────────────────────────────────────────
    await _store_results(meeting_id, extractions, sentiment_data, chunk_segments)

    return child_ids, chunk_segments, extractions, sentiment_data


# ─── Main Celery task ─────────────────────────────────────────────────────────

@celery_app.task(name="tasks.process_meeting")
def process_meeting(meeting_id: int, filename: str, content: str) -> dict:
    """
    Full processing pipeline for one uploaded transcript file.

    Args:
        meeting_id: PK of the meetings row (created by the upload route).
        filename:   Original filename (e.g. "standup_2024-03-01.vtt").
        content:    Raw text content of the uploaded file.

    Returns:
        A summary dict stored in the Celery result backend (Redis DB 1).
    """
    logger.info("Pipeline started: meeting_id=%d  file=%s", meeting_id, filename)

    try:
        # ── Step 1: Parse (pure Python, no DB, no network) ────────────────────
        parse_result = parse_transcript(content, filename)
        logger.info(
            "Parsed %s → %d child chunks, %d parent chunks, %d speakers",
            filename,
            len(parse_result.child_chunks),
            len(parse_result.parent_chunks),
            len(parse_result.speaker_names),
        )

        # ── Step 2–6: All async work in a single event loop ───────────────────
        child_ids, chunk_segments, extractions, sentiment_data = asyncio.run(
            _run_pipeline(meeting_id, filename, parse_result)
        )

        logger.info(
            "Pipeline complete: meeting_id=%d  chunks=%d  decisions=%d  action_items=%d",
            meeting_id,
            len(child_ids),
            len(extractions.get("decisions", [])),
            len(extractions.get("action_items", [])),
        )

        return {
            "meeting_id":    meeting_id,
            "child_chunks":  len(child_ids),
            "decisions":     len(extractions.get("decisions", [])),
            "action_items":  len(extractions.get("action_items", [])),
        }

    except Exception as exc:
        logger.exception("Pipeline failed: meeting_id=%d  error=%s", meeting_id, exc)
        asyncio.run(_mark_error(meeting_id, str(exc)))
        raise
