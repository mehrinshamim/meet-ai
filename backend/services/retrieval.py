"""
retrieval.py — Hybrid RAG retrieval pipeline. Nothing else goes here.

Pipeline for every query:
  1. (Optional) Query reformulation — rewrite follow-up questions to standalone.
  2. Embed the query with the same bge model (query prefix, not passage prefix).
  3. Semantic search — pgvector cosine distance, top-20 child chunks.
  4. Keyword search — PostgreSQL tsvector @@, top-20 child chunks.
  5. RRF merge — combine both ranked lists into one score per chunk.
  6. Cross-encoder rerank — rescore top-20 fused results, keep top-5.
  7. Parent fetch — load the parent chunk for each of the top-5.
  8. Context assembly — format as speaker-labelled blocks with timestamps.

Public functions:
    retrieve(query, session, meeting_id=None, chat_history=None) → RetrievalResult
    reformulate_query(question, chat_history) → str

Why hybrid retrieval?
    Semantic search finds conceptually similar text even without exact keywords.
    Keyword search finds rare proper nouns, project names, and acronyms that
    embeddings may dilute. Combining both covers each other's blind spots.

Why RRF (Reciprocal Rank Fusion)?
    We can't directly compare cosine distances and BM25 scores — different scales.
    RRF converts each list into ranks and scores every chunk as sum(1/(k+rank))
    across lists. k=60 is the standard value that makes scores smooth and
    prevents top-ranked items from dominating too heavily.

Why cross-encoder rerank?
    Embedding search is asymmetric (query vs passage) and runs fast because it
    computes embeddings independently. A cross-encoder reads (query, passage) as
    a single input and scores relevance directly — far more accurate but slower.
    We run it only on the top-20 RRF candidates so it stays fast.

Why parent chunks?
    Child chunks (~400 tokens) are small for accurate retrieval.
    Parent chunks (~5-min windows) give the LLM enough surrounding context to
    answer coherently. We search children, then swap in their parents for the
    LLM prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sentence_transformers import CrossEncoder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.embeddings import embed_texts

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

SEMANTIC_TOP_K = 20       # candidates from pgvector cosine search
KEYWORD_TOP_K = 20        # candidates from tsvector keyword search
RRF_K = 60                # standard RRF smoothing constant
RERANK_TOP_N = 20         # feed this many RRF results to cross-encoder
FINAL_TOP_N = 5           # keep this many after cross-encoder rerank

# Cross-encoder model for reranking.
# ms-marco-MiniLM-L-6-v2 is fast (6 transformer layers) and well-calibrated
# for passage relevance. It takes (query, passage) pairs and outputs a score.
CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# ─── Singleton cross-encoder ──────────────────────────────────────────────────

_cross_encoder: CrossEncoder | None = None


def get_cross_encoder() -> CrossEncoder:
    """Load the cross-encoder once, cache it for the process lifetime."""
    global _cross_encoder
    if _cross_encoder is None:
        logger.info("Loading cross-encoder model %s", CROSS_ENCODER_MODEL)
        _cross_encoder = CrossEncoder(CROSS_ENCODER_MODEL)
        logger.info("Cross-encoder loaded")
    return _cross_encoder


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ChunkResult:
    """One retrieved chunk with its metadata."""
    chunk_id: int
    meeting_id: int
    filename: str
    speaker: Optional[str]
    start_time: Optional[str]
    end_time: Optional[str]
    text: str
    rrf_score: float = 0.0
    rerank_score: float = 0.0


@dataclass
class RetrievalResult:
    """
    What retrieve() returns to the chat route.

    context_blocks: ready-to-paste text blocks for the LLM prompt.
    chunks: the raw ChunkResult objects (for citation extraction).
    reformulated_query: the rewritten query (or original if no rewrite needed).
    """
    context_blocks: list[str]
    chunks: list[ChunkResult]
    reformulated_query: str
    citations: list[dict] = field(default_factory=list)


# ─── Query reformulation ──────────────────────────────────────────────────────

def reformulate_query(question: str, chat_history: list[dict]) -> str:
    """
    Rewrite a follow-up question into a standalone question.

    If there is no chat history (first turn), return the question unchanged.
    If the question is already self-contained (no pronouns, no "it", "that",
    "they" referring to prior context), return it unchanged.

    Uses Groq via ai.py for the rewrite.

    Args:
        question:     The user's latest message.
        chat_history: List of {"question": str, "answer": str} dicts, oldest first.

    Returns:
        A standalone question string.
    """
    if not chat_history:
        return question

    # Check if reformulation is likely needed — look for pronouns / references
    # that typically point back to prior context.
    reference_words = {"it", "that", "they", "them", "this", "those",
                       "he", "she", "its", "their", "there"}
    words = set(question.lower().split())
    needs_rewrite = bool(words & reference_words)

    if not needs_rewrite:
        return question

    # Import here to avoid circular import (ai.py may import nothing from retrieval.py).
    from backend.services.ai import reformulate_question  # type: ignore[import]
    try:
        return reformulate_question(question, chat_history)
    except Exception:
        logger.warning("Query reformulation failed — using original question", exc_info=True)
        return question


# ─── Search helpers ───────────────────────────────────────────────────────────

async def _semantic_search(
    session: AsyncSession,
    query_vector: list[float],
    meeting_id: Optional[int],
    top_k: int,
) -> list[tuple[int, float]]:
    """
    Run pgvector cosine similarity search on child chunks.

    Returns list of (chunk_id, distance) sorted by distance ascending
    (lower distance = more similar for cosine distance operator <=>).

    We filter is_parent = FALSE because parent chunks don't have embeddings —
    they exist only to provide context after retrieval.
    """
    vector_str = str(query_vector)

    if meeting_id is not None:
        sql = text(
            """
            SELECT id, embedding <=> CAST(:vector AS vector) AS distance
            FROM chunks
            WHERE is_parent = FALSE
              AND meeting_id = :meeting_id
              AND embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT :top_k
            """
        )
        result = await session.execute(
            sql,
            {"vector": vector_str, "meeting_id": meeting_id, "top_k": top_k},
        )
    else:
        sql = text(
            """
            SELECT id, embedding <=> CAST(:vector AS vector) AS distance
            FROM chunks
            WHERE is_parent = FALSE
              AND embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT :top_k
            """
        )
        result = await session.execute(sql, {"vector": vector_str, "top_k": top_k})

    return [(row.id, float(row.distance)) for row in result]


async def _keyword_search(
    session: AsyncSession,
    query: str,
    meeting_id: Optional[int],
    top_k: int,
) -> list[tuple[int, float]]:
    """
    Run PostgreSQL full-text search (tsvector @@) on child chunks.

    ts_rank_cd() scores each matching document by term frequency and coverage.
    Higher score = more keyword overlap.

    Returns list of (chunk_id, rank) sorted by rank descending.
    """
    if meeting_id is not None:
        sql = text(
            """
            SELECT id,
                   ts_rank_cd(search_vector, plainto_tsquery('english', :query)) AS rank
            FROM chunks
            WHERE is_parent = FALSE
              AND meeting_id = :meeting_id
              AND search_vector @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :top_k
            """
        )
        result = await session.execute(
            sql,
            {"query": query, "meeting_id": meeting_id, "top_k": top_k},
        )
    else:
        sql = text(
            """
            SELECT id,
                   ts_rank_cd(search_vector, plainto_tsquery('english', :query)) AS rank
            FROM chunks
            WHERE is_parent = FALSE
              AND search_vector @@ plainto_tsquery('english', :query)
            ORDER BY rank DESC
            LIMIT :top_k
            """
        )
        result = await session.execute(sql, {"query": query, "top_k": top_k})

    return [(row.id, float(row.rank)) for row in result]


# ─── RRF merge ────────────────────────────────────────────────────────────────

def _rrf_merge(
    semantic_results: list[tuple[int, float]],
    keyword_results: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    """
    Combine two ranked lists using Reciprocal Rank Fusion.

    For each unique chunk_id, compute:
        rrf_score = sum over each list of  1 / (k + rank)

    where rank is 1-indexed position in that list.
    Chunks that appear in only one list still get a score from that list.

    Returns list of (chunk_id, rrf_score) sorted by score descending.
    """
    scores: dict[int, float] = {}

    for rank, (chunk_id, _) in enumerate(semantic_results, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    for rank, (chunk_id, _) in enumerate(keyword_results, start=1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ─── Chunk detail fetch ───────────────────────────────────────────────────────

async def _fetch_chunks(
    session: AsyncSession,
    chunk_ids: list[int],
) -> dict[int, ChunkResult]:
    """
    Load text + metadata for a list of chunk IDs.

    Joins with meetings to get the filename for citations.
    Returns dict keyed by chunk_id.
    """
    if not chunk_ids:
        return {}

    sql = text(
        """
        SELECT c.id, c.meeting_id, c.speaker, c.start_time, c.end_time,
               c.text, c.parent_id, m.filename
        FROM chunks c
        JOIN meetings m ON c.meeting_id = m.id
        WHERE c.id = ANY(:ids)
        """
    )
    result = await session.execute(sql, {"ids": chunk_ids})
    rows = result.fetchall()

    return {
        row.id: ChunkResult(
            chunk_id=row.id,
            meeting_id=row.meeting_id,
            filename=row.filename,
            speaker=row.speaker,
            start_time=row.start_time,
            end_time=row.end_time,
            text=row.text,
        )
        for row in rows
    }


async def _fetch_parent_chunks(
    session: AsyncSession,
    child_chunks: list[ChunkResult],
) -> dict[int, ChunkResult]:
    """
    For each child chunk, load its parent chunk (if one exists).

    Returns dict mapping child chunk_id → parent ChunkResult.
    Children with no parent (parent_id IS NULL) are excluded — the caller
    will fall back to the child text for those.
    """
    # We need the parent_id for each child. Re-query to get it.
    child_ids = [c.chunk_id for c in child_chunks]
    if not child_ids:
        return {}

    sql = text(
        """
        SELECT c.id AS child_id, p.id AS parent_id,
               p.meeting_id, p.speaker, p.start_time, p.end_time,
               p.text, m.filename
        FROM chunks c
        JOIN chunks p ON c.parent_id = p.id
        JOIN meetings m ON p.meeting_id = m.id
        WHERE c.id = ANY(:ids)
          AND c.parent_id IS NOT NULL
        """
    )
    result = await session.execute(sql, {"ids": child_ids})
    rows = result.fetchall()

    return {
        row.child_id: ChunkResult(
            chunk_id=row.parent_id,
            meeting_id=row.meeting_id,
            filename=row.filename,
            speaker=row.speaker,
            start_time=row.start_time,
            end_time=row.end_time,
            text=row.text,
        )
        for row in rows
    }


# ─── Cross-encoder rerank ─────────────────────────────────────────────────────

def _rerank(
    query: str,
    candidates: list[ChunkResult],
    top_n: int,
) -> list[ChunkResult]:
    """
    Score (query, passage) pairs with the cross-encoder, return top_n.

    The cross-encoder reads both the query and the passage text together as
    a single sequence, so it can model their interaction directly.  This is
    slower than embedding similarity but substantially more accurate.

    We pass the child text here (not parent), because the cross-encoder
    scores relevance — we want to know if this exact chunk answers the query.
    The parent is only fetched for the LLM prompt context window.
    """
    if not candidates:
        return []

    cross_enc = get_cross_encoder()
    pairs = [(query, c.text) for c in candidates]
    scores: list[float] = cross_enc.predict(pairs).tolist()

    for chunk, score in zip(candidates, scores):
        chunk.rerank_score = score

    # Sort descending by rerank score, return top_n
    ranked = sorted(candidates, key=lambda c: c.rerank_score, reverse=True)
    return ranked[:top_n]


# ─── Context assembly ─────────────────────────────────────────────────────────

def _format_context_block(chunk: ChunkResult) -> str:
    """
    Format a chunk (or its parent) as a readable context block for the LLM.

    Format:
        [Meeting: filename.vtt | Time: 00:05:30 | Speaker: Alice]
        <text>

    The LLM is instructed to cite using this information.
    """
    parts = [f"Meeting: {chunk.filename}"]
    if chunk.start_time:
        parts.append(f"Time: {chunk.start_time}")
    if chunk.speaker:
        parts.append(f"Speaker: {chunk.speaker}")

    header = "[" + " | ".join(parts) + "]"
    return f"{header}\n{chunk.text.strip()}"


def _build_citations(chunks: list[ChunkResult]) -> list[dict]:
    """Build citation dicts from the final reranked chunks."""
    return [
        {
            "meeting": c.filename,
            "timestamp": c.start_time or "",
            "speaker": c.speaker or "",
        }
        for c in chunks
    ]


# ─── Public API ───────────────────────────────────────────────────────────────

async def retrieve(
    query: str,
    session: AsyncSession,
    meeting_id: Optional[int] = None,
    chat_history: Optional[list[dict]] = None,
) -> RetrievalResult:
    """
    Run the full retrieval pipeline for a query.

    Steps:
      1. Reformulate query if follow-up detected.
      2. Embed query (query prefix, not passage prefix).
      3. Semantic search (pgvector cosine, top-20).
      4. Keyword search (tsvector, top-20).
      5. RRF merge.
      6. Cross-encoder rerank (top-20 → top-5).
      7. Fetch parent chunks for context.
      8. Format context blocks + citations.

    Args:
        query:        User's question (raw).
        session:      Active AsyncSession — caller manages lifecycle.
        meeting_id:   Scope search to one meeting (None = all meetings).
        chat_history: Prior turns for reformulation. Each entry:
                      {"question": str, "answer": str}

    Returns:
        RetrievalResult with context_blocks, chunks, reformulated_query, citations.
    """
    history = chat_history or []

    # Step 1 — reformulate
    reformulated = reformulate_query(query, history)
    logger.info("Query: %r → reformulated: %r", query, reformulated)

    # Step 2 — embed (runs sentence-transformer synchronously; caller should
    # wrap in run_in_executor if inside a hot async path)
    import asyncio
    loop = asyncio.get_running_loop()
    query_vectors = await loop.run_in_executor(
        None, lambda: embed_texts([reformulated], is_query=True)
    )
    query_vector = query_vectors[0]

    # Step 3 — semantic search
    semantic_results = await _semantic_search(session, query_vector, meeting_id, SEMANTIC_TOP_K)
    logger.info("Semantic hits: %d", len(semantic_results))

    # Step 4 — keyword search
    keyword_results = await _keyword_search(session, reformulated, meeting_id, KEYWORD_TOP_K)
    logger.info("Keyword hits: %d", len(keyword_results))

    if not semantic_results and not keyword_results:
        logger.warning("No results found for query: %r", reformulated)
        return RetrievalResult(
            context_blocks=[],
            chunks=[],
            reformulated_query=reformulated,
            citations=[],
        )

    # Step 5 — RRF merge
    fused = _rrf_merge(semantic_results, keyword_results)
    logger.info("RRF unique chunks: %d", len(fused))

    # Step 6 — fetch text for top RERANK_TOP_N candidates
    top_ids = [chunk_id for chunk_id, _ in fused[:RERANK_TOP_N]]
    rrf_scores = {chunk_id: score for chunk_id, score in fused[:RERANK_TOP_N]}

    candidate_map = await _fetch_chunks(session, top_ids)

    # Attach RRF scores
    candidates: list[ChunkResult] = []
    for chunk_id in top_ids:
        if chunk_id in candidate_map:
            c = candidate_map[chunk_id]
            c.rrf_score = rrf_scores[chunk_id]
            candidates.append(c)

    # Step 7 — cross-encoder rerank
    reranked = await loop.run_in_executor(
        None, lambda: _rerank(reformulated, candidates, FINAL_TOP_N)
    )
    logger.info("After rerank: %d chunks", len(reranked))

    # Step 8 — fetch parent chunks
    parent_map = await _fetch_parent_chunks(session, reranked)

    # Build context blocks: prefer parent chunk text, fall back to child text
    context_blocks: list[str] = []
    for child in reranked:
        context_chunk = parent_map.get(child.chunk_id, child)
        block = _format_context_block(context_chunk)
        context_blocks.append(block)

    citations = _build_citations(reranked)

    return RetrievalResult(
        context_blocks=context_blocks,
        chunks=reranked,
        reformulated_query=reformulated,
        citations=citations,
    )
