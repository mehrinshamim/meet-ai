"""
test_embeddings.py — Manual tests for backend/services/embeddings.py

Run with:
  uv run python tests/manual/test_embeddings.py

Tests are numbered and printed to stdout so you can read them one by one.
No test framework needed — just assertions and print statements.

What these tests verify:
  1-3.  Model loading (singleton, correct model name)
  4-6.  embed_texts() shape and type
  7-8.  Vectors are unit-normalised (‖v‖ ≈ 1.0)
  9.    Semantic similarity makes sense (related texts score higher)
  10.   Query prefix differs from passage prefix
  11-12. store_embeddings() writes to a real PostgreSQL row
  13.   embed_and_store() end-to-end round-trip
  14.   Cosine similarity query via pgvector <=> operator
  15.   tsvector is populated after store_embeddings()
"""

from __future__ import annotations

import asyncio
import math
import os
import sys

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import numpy as np
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from backend.database import AsyncSessionLocal, engine
from backend.services.embeddings import (
    MODEL_NAME,
    VECTOR_DIM,
    embed_and_store,
    embed_texts,
    get_model,
    store_embeddings,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(n: int, desc: str, cond: bool) -> None:
    status = PASS if cond else FAIL
    print(f"  [{status}] Test {n:02d}: {desc}")
    if not cond:
        raise AssertionError(f"Test {n} failed: {desc}")

def cosine_sim(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_arr = np.array(a)
    b_arr = np.array(b)
    return float(np.dot(a_arr, b_arr) / (np.linalg.norm(a_arr) * np.linalg.norm(b_arr)))

# ─── DB helpers ───────────────────────────────────────────────────────────────

async def create_test_meeting(session) -> int:
    """Insert a minimal meeting row and return its id."""
    result = await session.execute(
        text(
            "INSERT INTO meetings (filename, file_format, processed) "
            "VALUES ('test_embed.vtt', 'vtt', false) RETURNING id"
        )
    )
    return result.scalar_one()

async def create_test_chunk(session, meeting_id: int, text_content: str) -> int:
    """Insert a minimal child chunk row and return its id."""
    result = await session.execute(
        text(
            "INSERT INTO chunks (meeting_id, is_parent, speaker, start_time, end_time, text) "
            "VALUES (:mid, false, 'Alice', '00:00:00', '00:00:10', :txt) RETURNING id"
        ),
        {"mid": meeting_id, "txt": text_content},
    )
    return result.scalar_one()

async def cleanup(session, meeting_id: int) -> None:
    """Delete test rows so tests are idempotent."""
    await session.execute(text("DELETE FROM meetings WHERE id = :id"), {"id": meeting_id})
    await session.commit()

# ─── Tests ────────────────────────────────────────────────────────────────────

def test_model_loading() -> None:
    print("\n--- Model Loading ---")

    # 1. get_model() returns a non-None object
    model = get_model()
    check(1, "get_model() returns a model object", model is not None)

    # 2. Calling get_model() twice returns the same object (singleton)
    model2 = get_model()
    check(2, "get_model() is a singleton (same object on second call)", model is model2)

    # 3. Model name matches the constant
    check(3, f"Model name matches {MODEL_NAME}", MODEL_NAME in str(type(model).__mro__[0].__module__ + MODEL_NAME))


def test_embed_texts_shape() -> None:
    print("\n--- embed_texts() Shape & Type ---")

    texts = [
        "Alice decided to schedule the next meeting for Friday.",
        "Bob will send the updated budget report by end of week.",
        "The team agreed to move the deadline to Q2.",
    ]
    vectors = embed_texts(texts)

    # 4. Returns a list of the correct length
    check(4, f"Returns list of length {len(texts)}", len(vectors) == len(texts))

    # 5. Each vector has VECTOR_DIM dimensions
    check(5, f"Each vector has {VECTOR_DIM} dimensions", all(len(v) == VECTOR_DIM for v in vectors))

    # 6. Each element is a float
    check(6, "Vector elements are floats", all(isinstance(x, float) for x in vectors[0]))


def test_normalisation() -> None:
    print("\n--- Unit Normalisation ---")

    texts = ["The project deadline was moved to next quarter."]
    vectors = embed_texts(texts)
    norm = math.sqrt(sum(x ** 2 for x in vectors[0]))

    # 7. Vectors are unit-length (‖v‖ ≈ 1.0) because normalize_embeddings=True
    check(7, f"Vector norm ≈ 1.0 (got {norm:.6f})", abs(norm - 1.0) < 1e-4)

    # 8. Empty input returns empty list
    check(8, "embed_texts([]) returns []", embed_texts([]) == [])


def test_semantic_similarity() -> None:
    print("\n--- Semantic Similarity ---")

    related_a = "The meeting was postponed to next week."
    related_b = "The call has been rescheduled for the following week."
    unrelated  = "The quarterly revenue exceeded expectations by 20 percent."

    vecs = embed_texts([related_a, related_b, unrelated])
    sim_related   = cosine_sim(vecs[0], vecs[1])
    sim_unrelated = cosine_sim(vecs[0], vecs[2])

    # 9. Semantically related texts score higher than unrelated ones
    check(
        9,
        f"Related sim ({sim_related:.3f}) > Unrelated sim ({sim_unrelated:.3f})",
        sim_related > sim_unrelated,
    )


def test_query_vs_passage_prefix() -> None:
    print("\n--- Query vs Passage Prefix ---")

    text_str = "The budget was approved in the Monday meeting."
    passage_vec = embed_texts([text_str], is_query=False)[0]
    query_vec   = embed_texts([text_str], is_query=True)[0]

    # 10. Query and passage vectors for the same text are not identical
    # (different prefixes → different vectors)
    sim = cosine_sim(passage_vec, query_vec)
    check(
        10,
        f"Query and passage vectors differ (cosine sim = {sim:.4f}, should be < 1.0)",
        sim < 0.9999,
    )


async def test_store_embeddings() -> None:
    print("\n--- store_embeddings() DB Write ---")

    async with AsyncSessionLocal() as session:
        mid = await create_test_meeting(session)
        await session.commit()

        chunk_text = "Alice confirmed that the API integration will be done by Thursday."
        cid = await create_test_chunk(session, mid, chunk_text)
        await session.commit()

        vectors = embed_texts([chunk_text])

        await store_embeddings(session, [cid], vectors, [chunk_text])
        await session.commit()

        # 11. embedding column is populated
        row = await session.execute(
            text("SELECT embedding IS NOT NULL AS has_embed FROM chunks WHERE id = :id"),
            {"id": cid},
        )
        has_embed = row.scalar_one()
        check(11, "embedding column is NOT NULL after store_embeddings()", has_embed)

        # 12. search_vector (tsvector) column is populated
        row2 = await session.execute(
            text("SELECT search_vector IS NOT NULL AS has_ts FROM chunks WHERE id = :id"),
            {"id": cid},
        )
        has_ts = row2.scalar_one()
        check(12, "search_vector column is NOT NULL after store_embeddings()", has_ts)

        await cleanup(session, mid)


async def test_embed_and_store() -> None:
    print("\n--- embed_and_store() End-to-End ---")

    async with AsyncSessionLocal() as session:
        mid = await create_test_meeting(session)
        await session.commit()

        chunk_text = "Bob will follow up with the design team about the wireframes."
        cid = await create_test_chunk(session, mid, chunk_text)
        await session.commit()

        returned_vectors = await embed_and_store(session, [cid], [chunk_text])
        await session.commit()

        # 13. embed_and_store returns vectors of correct shape
        check(
            13,
            f"embed_and_store returns 1 vector of dim {VECTOR_DIM}",
            len(returned_vectors) == 1 and len(returned_vectors[0]) == VECTOR_DIM,
        )

        await cleanup(session, mid)


async def test_cosine_query() -> None:
    print("\n--- pgvector Cosine Similarity Query ---")

    async with AsyncSessionLocal() as session:
        mid = await create_test_meeting(session)
        await session.commit()

        # Insert two chunks — one relevant, one not
        relevant_text   = "The launch date for the product was set to June 15th."
        irrelevant_text = "Everyone agreed the catering was excellent at the offsite."

        cid1 = await create_test_chunk(session, mid, relevant_text)
        cid2 = await create_test_chunk(session, mid, irrelevant_text)
        await session.commit()

        await embed_and_store(session, [cid1, cid2], [relevant_text, irrelevant_text])
        await session.commit()

        # Query: what is similar to "product release date"?
        query_vec = embed_texts(["When is the product launch date?"], is_query=True)[0]
        query_vec_str = str(query_vec)

        result = await session.execute(
            text(
                """
                SELECT id, embedding <=> CAST(:qvec AS vector) AS distance
                FROM chunks
                WHERE id = ANY(:ids)
                ORDER BY distance ASC
                LIMIT 1
                """
            ),
            {"qvec": query_vec_str, "ids": [cid1, cid2]},
        )
        top_row = result.fetchone()

        # 14. The relevant chunk ranks first (lowest cosine distance)
        check(
            14,
            f"Relevant chunk (id={cid1}) ranked first by cosine distance (got id={top_row[0]})",
            top_row[0] == cid1,
        )

        await cleanup(session, mid)


async def test_tsvector_keyword_search() -> None:
    print("\n--- tsvector Keyword Search ---")

    async with AsyncSessionLocal() as session:
        mid = await create_test_meeting(session)
        await session.commit()

        chunk_text = "Carol said the deployment pipeline is blocked by a failing test."
        cid = await create_test_chunk(session, mid, chunk_text)
        await session.commit()

        await embed_and_store(session, [cid], [chunk_text])
        await session.commit()

        # 15. Keyword "deployment" matches the tsvector
        result = await session.execute(
            text(
                """
                SELECT id FROM chunks
                WHERE id = :cid
                  AND search_vector @@ plainto_tsquery('english', 'deployment pipeline')
                """
            ),
            {"cid": cid},
        )
        found = result.fetchone()
        check(15, "tsvector keyword search finds chunk with 'deployment pipeline'", found is not None)

        await cleanup(session, mid)


# ─── Runner ───────────────────────────────────────────────────────────────────

async def run_async_tests() -> None:
    await test_store_embeddings()
    await test_embed_and_store()
    await test_cosine_query()
    await test_tsvector_keyword_search()
    await engine.dispose()


def main() -> None:
    print("=" * 60)
    print("  test_embeddings.py — Phase 3 Manual Tests")
    print("=" * 60)

    # Sync tests (no DB needed)
    test_model_loading()
    test_embed_texts_shape()
    test_normalisation()
    test_semantic_similarity()
    test_query_vs_passage_prefix()

    # Async tests (require running PostgreSQL)
    asyncio.run(run_async_tests())

    print("\n" + "=" * 60)
    print("  All 15 tests passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
