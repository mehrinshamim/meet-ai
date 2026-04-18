"""
test_retrieval.py — Manual tests for Phase 6: RAG Query Engine.

Tests are numbered and self-describing. Run them after uploading at least one
meeting through the normal pipeline (upload → celery processes → chunks stored).

Usage:
    # Start infrastructure first
    docker compose up -d
    uv run celery -A backend.tasks.celery_app worker --loglevel=info &
    uv run uvicorn backend.main:app --reload --port 8000 &

    # Run with a meeting_id that has been processed
    uv run python tests/manual/test_retrieval.py <meeting_id>

    # Cross-meeting test (no meeting_id scope)
    uv run python tests/manual/test_retrieval.py

Tests 1–5: Pure Python (no DB needed)
Tests 6–13: Require DB with at least one processed meeting
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

# ─── Pure Python Tests (no DB) ────────────────────────────────────────────────

def test_01_rrf_merge_combines_lists():
    """RRF with overlapping and non-overlapping IDs produces correct ranking."""
    from backend.services.retrieval import _rrf_merge

    semantic = [(1, 0.1), (2, 0.2), (3, 0.3)]   # IDs 1, 2, 3 by cosine distance
    keyword  = [(2, 0.9), (4, 0.7), (1, 0.5)]   # IDs 2, 4, 1 by ts_rank

    fused = _rrf_merge(semantic, keyword, k=60)

    print("  RRF fused:", fused)
    ids = [cid for cid, _ in fused]
    # ID 1 appears in both lists → should rank high
    # ID 2 appears in both lists → should rank high
    assert 1 in ids, "ID 1 must be in fused results"
    assert 2 in ids, "ID 2 must be in fused results"
    assert 3 in ids, "ID 3 (semantic only) must be in fused results"
    assert 4 in ids, "ID 4 (keyword only) must be in fused results"

    # Item that appears in both lists should outscore items in only one list
    score_of_1 = next(s for cid, s in fused if cid == 1)
    score_of_3 = next(s for cid, s in fused if cid == 3)  # semantic only, rank 3
    score_of_4 = next(s for cid, s in fused if cid == 4)  # keyword only, rank 2
    assert score_of_1 > score_of_3, "ID 1 (in both lists) should outscore ID 3 (semantic only)"
    print("  PASS: RRF scores look correct")


def test_02_rrf_merge_empty_lists():
    """RRF handles empty inputs without error."""
    from backend.services.retrieval import _rrf_merge

    assert _rrf_merge([], []) == []
    assert _rrf_merge([(1, 0.5)], []) == [(1, pytest_approx(1 / 61))]
    print("  PASS: empty list cases handled")


def test_03_format_context_block_with_all_fields():
    """Context block includes meeting filename, time, and speaker."""
    from backend.services.retrieval import ChunkResult, _format_context_block

    chunk = ChunkResult(
        chunk_id=1, meeting_id=1, filename="standup.vtt",
        speaker="Alice", start_time="00:05:30", end_time="00:06:00",
        text="We decided to move the deadline to Friday.",
    )
    block = _format_context_block(chunk)
    print("  Block:\n", block)
    assert "standup.vtt" in block
    assert "00:05:30" in block
    assert "Alice" in block
    assert "We decided" in block
    print("  PASS: context block formatted correctly")


def test_04_format_context_block_missing_optional_fields():
    """Context block handles None speaker and None start_time gracefully."""
    from backend.services.retrieval import ChunkResult, _format_context_block

    chunk = ChunkResult(
        chunk_id=2, meeting_id=1, filename="meeting.txt",
        speaker=None, start_time=None, end_time=None,
        text="Some text with no speaker info.",
    )
    block = _format_context_block(chunk)
    print("  Block:", block)
    assert "meeting.txt" in block
    assert "None" not in block  # None should not appear as literal string
    print("  PASS: optional fields handled")


def test_05_reformulate_no_history():
    """reformulate_query returns original question unchanged when no history."""
    from backend.services.retrieval import reformulate_query

    q = "What decisions were made about the API redesign?"
    result = reformulate_query(q, [])
    assert result == q
    print("  PASS: no history → original question returned")


def test_06_reformulate_with_history_no_pronouns():
    """reformulate_query returns original if no reference words detected."""
    from backend.services.retrieval import reformulate_query

    history = [{"question": "What is the project timeline?", "answer": "Q2 2026."}]
    q = "Who owns the backend migration work?"
    result = reformulate_query(q, history)
    # No pronouns/reference words → should return q unchanged
    assert result == q
    print("  PASS: question without reference words returned as-is")


# ─── DB Tests ─────────────────────────────────────────────────────────────────

async def _async_test_07_embed_query():
    """embed_texts with is_query=True returns a 1024-dim normalised vector."""
    from backend.services.embeddings import embed_texts
    import numpy as np

    vecs = embed_texts(["What decisions were made?"], is_query=True)
    assert len(vecs) == 1, "Expected 1 vector"
    v = vecs[0]
    assert len(v) == 1024, f"Expected 1024 dims, got {len(v)}"

    norm = np.linalg.norm(v)
    assert abs(norm - 1.0) < 1e-5, f"Vector should be unit-norm, got {norm}"
    print(f"  Vector dim={len(v)}, norm={norm:.6f}")
    print("  PASS: query embedding is 1024-dim unit vector")


async def _async_test_08_semantic_search(session, meeting_id):
    """Semantic search returns chunk IDs ranked by cosine similarity."""
    from backend.services.embeddings import embed_texts
    from backend.services.retrieval import _semantic_search

    vecs = embed_texts(["What were the main decisions?"], is_query=True)
    results = await _semantic_search(session, vecs[0], meeting_id, top_k=5)

    print(f"  Semantic results: {results}")
    assert isinstance(results, list), "Expected list"
    if results:
        ids, distances = zip(*results)
        assert all(isinstance(d, float) for d in distances)
        assert all(d >= 0 for d in distances), "Cosine distances must be >= 0"
    print(f"  PASS: semantic search returned {len(results)} results")


async def _async_test_09_keyword_search(session, meeting_id):
    """Keyword search returns chunk IDs with ts_rank scores."""
    from backend.services.retrieval import _keyword_search

    results = await _keyword_search(session, "decision action item", meeting_id, top_k=5)
    print(f"  Keyword results: {results}")
    assert isinstance(results, list)
    if results:
        ids, ranks = zip(*results)
        assert all(isinstance(r, float) for r in ranks)
        assert all(r > 0 for r in ranks), "ts_rank scores must be > 0"
    print(f"  PASS: keyword search returned {len(results)} results")


async def _async_test_10_rrf_produces_merged_ranking(session, meeting_id):
    """RRF merges semantic + keyword into a unified ranked list."""
    from backend.services.embeddings import embed_texts
    from backend.services.retrieval import _semantic_search, _keyword_search, _rrf_merge

    vecs = embed_texts(["main discussion topics"], is_query=True)
    semantic = await _semantic_search(session, vecs[0], meeting_id, 10)
    keyword = await _keyword_search(session, "main discussion", meeting_id, 10)

    fused = _rrf_merge(semantic, keyword)
    print(f"  Fused: {fused[:5]}")
    assert len(fused) > 0, "Expected at least one result"
    # Scores should be descending
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True), "RRF list should be sorted descending"
    print(f"  PASS: RRF produced {len(fused)} merged results, sorted correctly")


async def _async_test_11_fetch_chunks_returns_text(session, meeting_id):
    """_fetch_chunks returns ChunkResult objects with text and metadata."""
    from sqlalchemy import text
    from backend.services.retrieval import _fetch_chunks

    # Get real child chunk IDs from the meeting
    result = await session.execute(
        text("SELECT id FROM chunks WHERE meeting_id = :mid AND is_parent = FALSE LIMIT 3"),
        {"mid": meeting_id},
    )
    ids = [row.id for row in result]
    if not ids:
        print("  SKIP: no child chunks found for this meeting")
        return

    chunks = await _fetch_chunks(session, ids)
    print(f"  Fetched {len(chunks)} chunks")
    for cid, c in chunks.items():
        print(f"    chunk {cid}: speaker={c.speaker}, start={c.start_time}, text[:60]={c.text[:60]!r}")

    assert len(chunks) == len(ids), "Should fetch same count as requested"
    for c in chunks.values():
        assert c.text, "text must not be empty"
        assert c.filename, "filename must be set"
    print("  PASS: chunk fetch returned valid ChunkResult objects")


async def _async_test_12_cross_encoder_rerank(session, meeting_id):
    """Cross-encoder reranks candidates and returns top_n."""
    from sqlalchemy import text
    from backend.services.retrieval import _fetch_chunks, _rerank

    result = await session.execute(
        text("SELECT id FROM chunks WHERE meeting_id = :mid AND is_parent = FALSE LIMIT 10"),
        {"mid": meeting_id},
    )
    ids = [row.id for row in result]
    if len(ids) < 2:
        print("  SKIP: need at least 2 child chunks")
        return

    chunk_map = await _fetch_chunks(session, ids)
    candidates = list(chunk_map.values())

    t0 = time.time()
    reranked = _rerank("What decisions were made?", candidates, top_n=3)
    elapsed = time.time() - t0

    print(f"  Reranked {len(candidates)} → {len(reranked)} in {elapsed:.2f}s")
    for c in reranked:
        print(f"    score={c.rerank_score:.4f} | {c.text[:80]!r}")

    assert len(reranked) <= 3, "Should return at most top_n"
    scores = [c.rerank_score for c in reranked]
    assert scores == sorted(scores, reverse=True), "Should be sorted by rerank score"
    print("  PASS: cross-encoder rerank works correctly")


async def _async_test_13_full_retrieve_pipeline(session, meeting_id):
    """Full retrieve() call returns context_blocks with citations."""
    from backend.services.retrieval import retrieve

    t0 = time.time()
    result = await retrieve(
        query="What were the key decisions discussed?",
        session=session,
        meeting_id=meeting_id,
        chat_history=None,
    )
    elapsed = time.time() - t0

    print(f"  retrieve() completed in {elapsed:.2f}s")
    print(f"  reformulated_query: {result.reformulated_query!r}")
    print(f"  context_blocks ({len(result.context_blocks)}):")
    for i, block in enumerate(result.context_blocks):
        print(f"    [{i}] {block[:120]!r}")
    print(f"  citations: {result.citations}")

    assert isinstance(result.context_blocks, list)
    assert isinstance(result.citations, list)
    # If we have chunks, context blocks and citations should be populated
    if result.chunks:
        assert len(result.context_blocks) > 0, "Expected context blocks when chunks exist"
        assert len(result.citations) > 0, "Expected citations when chunks exist"
        for citation in result.citations:
            assert "meeting" in citation
            assert "timestamp" in citation
            assert "speaker" in citation
    print("  PASS: full retrieval pipeline succeeded")


def pytest_approx(v):
    """Tiny helper so test_02 can reference approximate float equality."""
    return v  # for display only — test_02 doesn't actually assert the value


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_pure_tests():
    """Run tests that don't need a DB connection."""
    pure = [
        test_01_rrf_merge_combines_lists,
        test_02_rrf_merge_empty_lists,
        test_03_format_context_block_with_all_fields,
        test_04_format_context_block_missing_optional_fields,
        test_05_reformulate_no_history,
        test_06_reformulate_with_history_no_pronouns,
    ]
    for fn in pure:
        print(f"\n[TEST] {fn.__name__}")
        try:
            fn()
        except Exception as exc:
            print(f"  FAIL: {exc}")


async def run_db_tests(meeting_id: int):
    """Run tests that require an active DB session."""
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        db_tests = [
            ("07", _async_test_07_embed_query,              []),
            ("08", _async_test_08_semantic_search,          [session, meeting_id]),
            ("09", _async_test_09_keyword_search,           [session, meeting_id]),
            ("10", _async_test_10_rrf_produces_merged_ranking, [session, meeting_id]),
            ("11", _async_test_11_fetch_chunks_returns_text,   [session, meeting_id]),
            ("12", _async_test_12_cross_encoder_rerank,     [session, meeting_id]),
            ("13", _async_test_13_full_retrieve_pipeline,   [session, meeting_id]),
        ]

        for num, fn, args in db_tests:
            print(f"\n[TEST {num}] {fn.__name__}")
            try:
                await fn(*args)
            except Exception as exc:
                import traceback
                print(f"  FAIL: {exc}")
                traceback.print_exc()


def main():
    print("=" * 60)
    print("Phase 6 — RAG Retrieval: Manual Tests")
    print("=" * 60)

    # Pure Python tests (always run)
    run_pure_tests()

    # DB tests (only if meeting_id provided)
    if len(sys.argv) >= 2:
        meeting_id = int(sys.argv[1])
        print(f"\n--- DB tests (meeting_id={meeting_id}) ---")
        asyncio.run(run_db_tests(meeting_id))
    else:
        print("\nSkipping DB tests — pass a meeting_id as argument to run them.")
        print("Usage: uv run python tests/manual/test_retrieval.py <meeting_id>")

    print("\nDone.")


if __name__ == "__main__":
    main()
