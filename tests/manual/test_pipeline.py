"""
test_pipeline.py — Manual tests for the Phase 4 Celery pipeline.

Run with:
  uv run python tests/manual/test_pipeline.py

What these tests verify:
  1-3.   celery_app: correct broker URL, backend URL, task included
  4-5.   ai.py get_client(): returns Groq instance, is singleton
  6-8.   extract_decisions_and_actions(): returns correct structure with real Groq call
  9-11.  analyze_sentiment(): returns correct structure with real Groq call
  12-15. _store_and_embed(): inserts chunks in DB, sets embeddings, sets tsvector
  16-18. _store_results(): inserts extraction + sentiment rows, marks meeting processed
  19-20. _mark_error(): writes error message, keeps processed=False
  21-23. process_meeting() task end-to-end: full pipeline on a sample .vtt file

IMPORTANT:
  - Tests 6-23 require a real .env file with GROQ_API_KEY, DATABASE_URL, etc.
  - Tests 12-23 require PostgreSQL and the migrations to be applied.
  - Tests 6-11 make real Groq API calls and will be billed against your key.
  - Tests 21-23 take ~30-60 seconds (embedding model load + Groq calls).

The test creates a temporary meeting row before the pipeline tests and cleans
it up afterwards.  If a test fails mid-way, orphan rows may remain in the DB.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

# ─── Pre-flight: fail fast with a clear message if .env is incomplete ──────────
# config.py uses os.environ["KEY"] (not .get()), so a missing var would crash
# at import time with a bare KeyError before any test output is printed.

_REQUIRED_VARS = ["DATABASE_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "GROQ_API_KEY"]
_missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
if _missing:
    print(f"ERROR: Missing required env vars: {', '.join(_missing)}")
    print("Copy .env.example to .env and fill in your values.")
    sys.exit(1)

# ─── Imports ──────────────────────────────────────────────────────────────────

from groq import Groq
from sqlalchemy import select, text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.models import Chunk, Extraction, Meeting, Sentiment
from backend.services.ai import (
    analyze_sentiment,
    extract_decisions_and_actions,
    get_client,
)
from backend.tasks.celery_app import celery_app
from backend.tasks.pipeline import (
    _mark_error,
    _store_and_embed,
    _store_results,
    _build_transcript_text,
    process_meeting,
)
from backend.services.parser import parse_transcript

import backend.database as _db_mod
import backend.tasks.pipeline as _pipeline_mod

# ─── NullPool session factory for tests ───────────────────────────────────────
# asyncpg connection pools are event-loop scoped.  Each asyncio.run() creates a
# new event loop and closes the previous one, making pooled connections from the
# old loop invalid for the new one (InterfaceError: connection bound to closed
# event loop).  NullPool disables pooling entirely: every asyncio.run() acquires
# a fresh connection and releases it immediately on exit — no cross-loop state.

_test_engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False)

# Patch every module that holds a reference to AsyncSessionLocal so they all
# use the NullPool version.  (Python's import system caches module objects, so
# we must patch the name in each module's namespace, not just in backend.database.)
_db_mod.AsyncSessionLocal = _TestSession
_pipeline_mod.AsyncSessionLocal = _TestSession

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def check(n: int, desc: str, cond: bool) -> None:
    status = PASS if cond else FAIL
    print(f"  [{status}] Test {n:02d}: {desc}")
    if not cond:
        raise AssertionError(f"Test {n} failed: {desc}")


# ─── Sample transcript ─────────────────────────────────────────────────────────

SAMPLE_VTT = """\
WEBVTT

00:00:05.000 --> 00:00:20.000
<v Alice>We need to decide on the Q3 budget today. I'm proposing we increase the marketing spend by 20 percent.

00:00:20.000 --> 00:00:35.000
<v Bob>I think that's a reasonable proposal. We've seen good returns from digital marketing this quarter.

00:00:35.000 --> 00:00:55.000
<v Alice>Great. Let's also talk about the product roadmap. We need to ship the API redesign by end of August.

00:00:55.000 --> 00:01:10.000
<v Carol>I can own the API redesign. I'll have a draft spec ready by next Friday.

00:01:10.000 --> 00:01:25.000
<v Bob>Sounds good. Can we also agree to move the weekly standup from Monday to Tuesday? Monday mornings are rough for the team.

00:01:25.000 --> 00:01:40.000
<v Alice>Agreed. Let's move it to Tuesday 10am. Bob, please update the calendar invite.

00:01:40.000 --> 00:01:55.000
<v Carol>One more thing — we need to decide on the vendor for the new data pipeline. I recommend going with Snowflake over Redshift.

00:01:55.000 --> 00:02:10.000
<v Alice>Let's go with Snowflake. Carol, please initiate the contract by end of this week.
"""

SAMPLE_FILENAME = "team_meeting_2024-07-15.vtt"


# ─── Async DB helpers (use _TestSession directly) ─────────────────────────────

async def _create_test_meeting() -> int:
    """Insert a minimal meeting row and return its ID."""
    async with _TestSession() as session:
        async with session.begin():
            meeting = Meeting(
                filename=SAMPLE_FILENAME,
                file_format="vtt",
                processed=False,
            )
            session.add(meeting)
            await session.flush()
            return meeting.id


async def _delete_test_meeting(meeting_id: int) -> None:
    """Delete the test meeting (CASCADE deletes chunks, extractions, sentiments)."""
    async with _TestSession() as session:
        async with session.begin():
            meeting = await session.get(Meeting, meeting_id)
            if meeting:
                await session.delete(meeting)


async def _get_chunk_count(meeting_id: int) -> tuple[int, int]:
    """Return (parent_count, child_count) for a meeting."""
    async with _TestSession() as session:
        result = await session.execute(
            select(Chunk).where(Chunk.meeting_id == meeting_id)
        )
        chunks = result.scalars().all()
        parents = sum(1 for c in chunks if c.is_parent)
        children = sum(1 for c in chunks if not c.is_parent)
        return parents, children


async def _get_extraction(meeting_id: int) -> Extraction | None:
    async with _TestSession() as session:
        result = await session.execute(
            select(Extraction).where(Extraction.meeting_id == meeting_id)
        )
        return result.scalars().first()


async def _get_sentiment(meeting_id: int) -> Sentiment | None:
    async with _TestSession() as session:
        result = await session.execute(
            select(Sentiment).where(Sentiment.meeting_id == meeting_id)
        )
        return result.scalars().first()


async def _get_meeting(meeting_id: int) -> Meeting | None:
    async with _TestSession() as session:
        return await session.get(Meeting, meeting_id)


async def _child_chunks_have_embeddings(meeting_id: int) -> bool:
    async with _TestSession() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM chunks "
                "WHERE meeting_id = :mid AND is_parent = false AND embedding IS NOT NULL"
            ),
            {"mid": meeting_id},
        )
        count = result.scalar()
        return count > 0


# ─── Tests ────────────────────────────────────────────────────────────────────

def test_celery_config() -> None:
    print("\n── Celery App Config ────────────────────────────────────────────")

    check(1, "broker URL matches CELERY_BROKER_URL env var",
          celery_app.conf.broker_url == os.environ["CELERY_BROKER_URL"])

    check(2, "result backend matches CELERY_RESULT_BACKEND env var",
          celery_app.conf.result_backend == os.environ["CELERY_RESULT_BACKEND"])

    check(3, "pipeline task is in included modules",
          "backend.tasks.pipeline" in celery_app.conf.include)


def test_groq_client() -> None:
    print("\n── Groq Client ──────────────────────────────────────────────────")

    client1 = get_client()
    client2 = get_client()

    check(4, "get_client() returns a Groq instance",
          isinstance(client1, Groq))

    check(5, "get_client() is a singleton (same object both calls)",
          client1 is client2)


def test_extract_decisions() -> None:
    print("\n── extract_decisions_and_actions() ─────────────────────────────")
    print("  (makes a real Groq API call — may take a few seconds)")

    # Parse the VTT and build the formatted transcript text, exactly as the
    # pipeline does in _build_transcript_text().  Sending raw VTT (with
    # timestamp blocks and <v> tags) to Groq tests a different code path than
    # what actually runs in production.
    parse_result = parse_transcript(SAMPLE_VTT, SAMPLE_FILENAME)
    full_transcript = _build_transcript_text(parse_result)
    result = extract_decisions_and_actions(full_transcript, SAMPLE_FILENAME)

    check(6, "returns a dict with 'decisions' key",
          isinstance(result.get("decisions"), list))

    check(7, "returns a dict with 'action_items' key",
          isinstance(result.get("action_items"), list))

    check(8, "at least one decision found (budget, standup, vendor)",
          len(result["decisions"]) >= 1)

    print(f"     Decisions found: {len(result['decisions'])}")
    for d in result["decisions"]:
        print(f"       - {d.get('text', '')[:80]}")
    print(f"     Action items found: {len(result['action_items'])}")
    for a in result["action_items"]:
        print(f"       - [{a.get('assignee', '?')}] {a.get('task', '')[:60]}")


def test_analyze_sentiment() -> None:
    print("\n── analyze_sentiment() ──────────────────────────────────────────")
    print("  (makes a real Groq API call — may take a few seconds)")

    # Build the segments list as the pipeline would
    parse_result = parse_transcript(SAMPLE_VTT, SAMPLE_FILENAME)
    segments = [
        {
            "index":      i,
            "chunk_id":   i + 9000,   # fake IDs for this test
            "speaker":    c.speaker or "Unknown",
            "start_time": c.start_time or "",
            "text":       c.text,
        }
        for i, c in enumerate(parse_result.child_chunks)
    ]

    result = analyze_sentiment(segments)

    check(9, "returns dict with 'speaker_scores' key",
          isinstance(result.get("speaker_scores"), dict))

    check(10, "returns dict with 'segment_scores' key",
          isinstance(result.get("segment_scores"), list))

    check(11, "at least one speaker scored",
          len(result["speaker_scores"]) >= 1)

    print(f"     Speaker scores:")
    for name, score in result["speaker_scores"].items():
        print(f"       {name}: {score:.2f}")
    print(f"     Segment scores: {len(result['segment_scores'])} returned")


def test_store_and_embed() -> None:
    print("\n── _store_and_embed() ───────────────────────────────────────────")
    print("  (real DB + embedding model — may take 10-30 seconds on first run)")

    # All async work runs inside a single asyncio.run() so every await shares
    # the same event loop.  Multiple asyncio.run() calls would each create a
    # new event loop; asyncpg connections from the old loop are invalid for the
    # new one even with pool_pre_ping (NullPool handles the end-to-end test
    # where process_meeting() forces separate asyncio.run() calls internally).
    async def _body() -> None:
        meeting_id = await _create_test_meeting()
        print(f"     Created test meeting ID: {meeting_id}")
        try:
            parse_result = parse_transcript(SAMPLE_VTT, SAMPLE_FILENAME)
            child_ids, chunk_segments = await _store_and_embed(meeting_id, parse_result)

            parents, children = await _get_chunk_count(meeting_id)
            has_embeddings = await _child_chunks_have_embeddings(meeting_id)

            check(12, "parent chunks inserted into DB",
                  parents == len(parse_result.parent_chunks))

            check(13, "child chunks inserted into DB",
                  children == len(parse_result.child_chunks))

            check(14, "chunk_segments list has correct length",
                  len(chunk_segments) == len(child_ids))

            check(15, "child chunks have embeddings stored",
                  has_embeddings)

            print(f"     Parents: {parents}  Children: {children}  Embeddings: {has_embeddings}")
        finally:
            await _delete_test_meeting(meeting_id)
            print(f"     Cleaned up meeting {meeting_id}")

    asyncio.run(_body())


def test_store_results() -> None:
    print("\n── _store_results() ─────────────────────────────────────────────")

    async def _body() -> None:
        meeting_id = await _create_test_meeting()
        try:
            # First store the chunks (needed for chunk_ids)
            parse_result = parse_transcript(SAMPLE_VTT, SAMPLE_FILENAME)
            _, chunk_segments = await _store_and_embed(meeting_id, parse_result)

            # Fake Groq outputs
            fake_extractions = {
                "decisions":    [{"text": "Go with Snowflake", "timestamp": "00:01:55", "speaker": "Alice"}],
                "action_items": [{"task": "Update calendar", "assignee": "Bob", "due_date": "", "timestamp": "00:01:40"}],
            }
            fake_sentiment = {
                "speaker_scores": {"Alice": 0.3, "Bob": 0.5, "Carol": 0.2},
                "segment_scores": [
                    {"segment_index": i, "score": 0.2, "label": "neutral"}
                    for i in range(len(chunk_segments))
                ],
            }

            await _store_results(meeting_id, fake_extractions, fake_sentiment, chunk_segments)

            extraction = await _get_extraction(meeting_id)
            sentiment = await _get_sentiment(meeting_id)
            meeting = await _get_meeting(meeting_id)

            check(16, "Extraction row inserted with decisions",
                  extraction is not None and len(extraction.decisions) == 1)

            check(17, "Sentiment row inserted with speaker_scores",
                  sentiment is not None and "Alice" in sentiment.speaker_scores)

            check(18, "Meeting marked as processed",
                  meeting is not None and meeting.processed is True)

            print(f"     Decisions: {len(extraction.decisions)}  "
                  f"Action items: {len(extraction.action_items)}")
            print(f"     Speaker scores: {sentiment.speaker_scores}")
            print(f"     Segment scores: {len(sentiment.segment_scores)} rows")
        finally:
            await _delete_test_meeting(meeting_id)
            print(f"     Cleaned up meeting {meeting_id}")

    asyncio.run(_body())


def test_mark_error() -> None:
    print("\n── _mark_error() ────────────────────────────────────────────────")

    async def _body() -> None:
        meeting_id = await _create_test_meeting()
        try:
            await _mark_error(meeting_id, "pipeline exploded")

            meeting = await _get_meeting(meeting_id)

            check(19, "_mark_error writes meeting.error",
                  meeting is not None and meeting.error == "pipeline exploded")

            check(20, "_mark_error keeps processed=False",
                  meeting is not None and meeting.processed is False)
        finally:
            await _delete_test_meeting(meeting_id)

    asyncio.run(_body())


def test_end_to_end() -> None:
    print("\n── process_meeting() end-to-end ─────────────────────────────────")
    print("  (full pipeline: parse + embed + 2 Groq calls — ~30-60 seconds)")

    # process_meeting() is a synchronous Celery task that internally calls
    # asyncio.run() for each async step.  We cannot wrap it in an outer async
    # body without triggering "This event loop is already running".  Instead we
    # keep separate asyncio.run() calls here; NullPool (configured above) gives
    # each call a fresh DB connection so there is no cross-loop contamination.

    meeting_id = asyncio.run(_create_test_meeting())
    print(f"     Created test meeting ID: {meeting_id}")

    try:
        # Call the task function directly (not via Celery worker) to test logic
        result = process_meeting(meeting_id, SAMPLE_FILENAME, SAMPLE_VTT)

        check(21, "task returns a summary dict with meeting_id",
              result.get("meeting_id") == meeting_id)

        check(22, "child_chunks count is positive",
              result.get("child_chunks", 0) > 0)

        meeting = asyncio.run(_get_meeting(meeting_id))
        check(23, "meeting.processed is True after full pipeline",
              meeting is not None and meeting.processed is True)

        print(f"     Result: {result}")
        print(f"     Meeting speakers: {meeting.speaker_names}")

    finally:
        asyncio.run(_delete_test_meeting(meeting_id))
        print(f"     Cleaned up meeting {meeting_id}")


# ─── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("test_pipeline.py — Phase 4 Celery Pipeline")
    print("=" * 60)

    tests = [
        ("Celery config",            test_celery_config),
        ("Groq client singleton",    test_groq_client),
        ("Extract decisions",        test_extract_decisions),
        ("Analyze sentiment",        test_analyze_sentiment),
        ("Store + embed chunks",     test_store_and_embed),
        ("Store Groq results",       test_store_results),
        ("Mark error",               test_mark_error),
        ("End-to-end pipeline",      test_end_to_end),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  STOPPED at: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR in {name}: {e}")
            import traceback; traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} test groups passed, {failed} failed")
    print("=" * 60)
