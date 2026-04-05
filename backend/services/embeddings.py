"""
embeddings.py — Local embedding service using BAAI/bge-large-en-v1.5.

Responsibilities:
  1. _load_model()     — load SentenceTransformer once, cache as module-level singleton
  2. embed_texts()     — batch embed a list of strings → list of 1024-dim float lists
  3. store_embeddings() — UPDATE chunk rows: set embedding + search_vector in PostgreSQL
  4. embed_and_store() — convenience wrapper: embed chunks then store them

Why bge-large-en-v1.5?
  - 1024-dimension vectors: richer semantic space than smaller models
  - Trained specifically for retrieval (not just similarity)
  - Requires a special prefix "Represent this sentence: " for passages (documents)
    and "Represent this question: " for queries — bge models are asymmetric
  - Runs locally, no API key, no per-token cost

Why singleton?
  Loading the model downloads ~1.3 GB of weights and takes 2-3 seconds.
  We load it once at import time (module-level variable) so every call after
  the first is instant.
"""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np
from sentence_transformers import SentenceTransformer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ─── Constants ────────────────────────────────────────────────────────────────

MODEL_NAME = "BAAI/bge-large-en-v1.5"

# bge models are asymmetric: documents and queries use different prefixes.
# At index time we embed documents (passages), so we use the passage prefix.
# At query time (retrieval.py) we use the query prefix.
PASSAGE_PREFIX = "Represent this passage: "

BATCH_SIZE = 32   # embed this many texts per forward pass through the model
VECTOR_DIM = 1024  # must match models.py VECTOR_DIM and pgvector column size

logger = logging.getLogger(__name__)

# ─── Singleton model ──────────────────────────────────────────────────────────

_model: SentenceTransformer | None = None


def get_model() -> SentenceTransformer:
    """
    Return the cached SentenceTransformer, loading it on first call.

    This is the singleton pattern: the first call pays the 2-3 second load
    cost; every subsequent call returns the already-loaded model instantly.
    Module-level state (_model) persists for the lifetime of the process.
    """
    global _model
    if _model is None:
        logger.info("Loading embedding model %s (first call — this takes a moment)", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Embedding model loaded. Vector dimension: %d", VECTOR_DIM)
    return _model


# ─── Embedding ────────────────────────────────────────────────────────────────

def embed_texts(texts: Sequence[str], *, is_query: bool = False) -> list[list[float]]:
    """
    Embed a list of strings and return a list of 1024-dim float vectors.

    Args:
        texts:    Strings to embed.  Must be non-empty.
        is_query: If True, apply the query prefix (used in retrieval.py).
                  If False (default), apply the passage prefix (used here at index time).

    Returns:
        List of vectors, one per input string.
        Each vector is a Python list of 1024 floats.

    Why normalize?
        We use cosine similarity for retrieval.  Normalizing vectors to unit length
        means cosine similarity becomes a simple dot product, which pgvector can
        compute faster with ivfflat index.
    """
    if not texts:
        return []

    model = get_model()
    prefix = "Represent this question: " if is_query else PASSAGE_PREFIX

    # Prepend the prefix bge expects.  We do it here so callers don't have to.
    prefixed = [prefix + t for t in texts]

    # encode() handles batching internally when batch_size is set.
    # normalize_embeddings=True → unit vectors → cosine sim = dot product.
    embeddings: np.ndarray = model.encode(
        prefixed,
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    # Convert numpy array → list of Python lists (JSON-serialisable, pgvector-compatible)
    return embeddings.tolist()


# ─── DB storage ───────────────────────────────────────────────────────────────

async def store_embeddings(
    session: AsyncSession,
    chunk_ids: list[int],
    vectors: list[list[float]],
    texts: list[str],
) -> None:
    """
    Write embedding vectors and tsvector search columns to chunk rows.

    For each chunk:
      - embedding     = the 1024-dim vector (pgvector type)
      - search_vector = PostgreSQL tsvector built from the chunk text
                        (used for BM25-style keyword search in Phase 6)

    We use raw SQL here because:
      1. pgvector's Python type doesn't support bulk UPDATE with different
         values per row via ORM easily.
      2. We want to set tsvector using PostgreSQL's to_tsvector() function,
         not compute it in Python.

    We batch the updates into a single executemany() call — one round-trip
    to the DB for all chunks, not N round-trips.

    Args:
        session:   AsyncSession (passed in from the calling task/route).
        chunk_ids: List of chunk primary-key IDs to update.
        vectors:   Parallel list of 1024-dim float vectors.
        texts:     Parallel list of chunk text strings (for tsvector).
    """
    if not chunk_ids:
        return

    # Build parameter list for executemany.
    # :vector must be cast to vector type explicitly with pgvector.
    params = [
        {
            "chunk_id": cid,
            "vector": str(vec),   # pgvector accepts Python list repr as string
            "text": txt,
        }
        for cid, vec, txt in zip(chunk_ids, vectors, texts)
    ]

    sql = text(
        """
        UPDATE chunks
        SET
            embedding     = CAST(:vector AS vector),
            search_vector = to_tsvector('english', :text)
        WHERE id = :chunk_id
        """
    )

    await session.execute(sql, params)
    # Caller is responsible for session.commit() — we don't commit here so the
    # caller can batch this with other writes in the same transaction.
    logger.info("Stored embeddings for %d chunks", len(chunk_ids))


# ─── Convenience wrapper ──────────────────────────────────────────────────────

async def embed_and_store(
    session: AsyncSession,
    chunk_ids: list[int],
    texts: list[str],
) -> list[list[float]]:
    """
    Embed texts and store them in the DB in one call.

    This is what the Celery pipeline task (Phase 4) will call.
    It returns the vectors so the caller can use them further if needed.

    Only embeds child chunks (is_parent=False) — parent chunks don't need
    embeddings because they're only fetched for context, never searched.
    """
    vectors = embed_texts(texts)
    await store_embeddings(session, chunk_ids, vectors, texts)
    return vectors
