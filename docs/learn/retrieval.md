# Phase 6 — RAG Retrieval: Concepts & Walkthrough

> Every concept introduced while building `services/retrieval.py`.

---

## What is RAG?

RAG stands for **Retrieval-Augmented Generation**. Instead of asking an LLM to answer from its training data (which doesn't know your meetings), you:

1. Search your own database for the most relevant passages.
2. Paste those passages into the LLM's prompt as context.
3. Ask the LLM to answer *only* using the provided context, and cite its sources.

This gives you:
- Accurate answers grounded in your actual meeting transcripts.
- Traceable citations (filename, timestamp, speaker).
- No hallucination about things that weren't said.

---

## Why Two Search Methods? (Hybrid Retrieval)

### Semantic Search (pgvector)
Embedding models turn text into vectors. Similar meaning → similar vectors → small cosine distance. This finds passages that *mean* the same thing as the query, even if they use different words.

**Strength:** "What was decided about the timeline?" finds "We agreed to ship by Q2" even though neither sentence shares words.

**Weakness:** Rare proper nouns, project codes, and acronyms get diluted across 1024 dimensions. Searching for "PROJ-42" semantically might not find "PROJ-42" in the text.

### Keyword Search (tsvector / BM25-style)
PostgreSQL's full-text search tokenises text into lexemes (root forms), builds an index, and scores matches by term frequency. This finds exact keyword matches.

**Strength:** "Find mentions of PROJ-42" or "What did Alice say about Redis?" — exact term matching.

**Weakness:** Finds zero results if the user phrases the question differently from how the meeting used those concepts.

**Hybrid wins because each search covers the other's blind spots.**

---

## What is RRF (Reciprocal Rank Fusion)?

We have two ranked lists of chunk IDs. We can't compare their scores directly:
- Cosine distance ranges 0–2 (lower = better).
- ts_rank ranges 0–∞ (higher = better).
- Different scales, different directions.

**RRF converts both lists into a unified score:**

```
rrf_score(chunk) = Σ over each list of  1 / (k + rank)
```

Where `rank` is 1-indexed position in that list, and `k = 60` (the standard value — smoothing constant that prevents top-ranked items from completely dominating).

### Why k=60?
- If k were 0, rank 1 gets score 1.0, rank 2 gets 0.5 — a huge cliff.
- k=60 makes rank 1 → 1/61 ≈ 0.016, rank 2 → 1/62 ≈ 0.016. Scores are close, so the second list can still influence the final ranking.
- 60 is empirically the best value across dozens of TREC retrieval benchmarks.

**Example:**
- Chunk A: rank 1 in semantic, rank 3 in keyword → 1/61 + 1/63 ≈ 0.032
- Chunk B: rank 2 in semantic only → 1/62 ≈ 0.016
- Chunk C: rank 1 in keyword only → 1/61 ≈ 0.016

Chunk A wins because it appears in both lists, even though it wasn't first in either.

---

## What is Cross-Encoder Reranking?

### Bi-encoder (what we use for search)
The embedding model encodes the query and each passage **independently**. Similarity is just the dot product of the resulting vectors. Fast — O(1) per query after index built.

**Problem:** The model never sees the query and passage together, so it can't model their interaction precisely.

### Cross-encoder (what we use for reranking)
A cross-encoder takes `(query, passage)` as a single input and outputs one relevance score. It can model exactly how the passage answers the query.

**Much more accurate, but slower:** O(N) full model forward passes.

**Strategy:** Use bi-encoder to get top-20 candidates fast. Then run the cross-encoder only on those 20. You get accuracy-near-cross-encoder with cost-near-bi-encoder.

### Model used
`cross-encoder/ms-marco-MiniLM-L-6-v2` — 6-layer transformer, trained on the MS MARCO passage retrieval dataset (real search queries + human relevance judgments). Returns a float score (higher = more relevant).

---

## Why Parent-Child Chunks?

### Child chunks (~400 tokens)
Small. High retrieval precision — a 400-token chunk about "the deadline decision" is very likely to match a query about deadlines. Small chunks score well on the cross-encoder.

**Problem:** Small chunks lack context. The LLM sees "We agreed to Friday" with no idea what "we" refers to or what was agreed for Friday.

### Parent chunks (~5-minute windows)
Large. Span multiple speaker turns. Contain the surrounding context: the full discussion, the reasoning, the back-and-forth.

**Problem:** Too big to retrieve accurately. A 5-minute window touches many topics; it wouldn't score as well on a specific query.

### Solution: search children, prompt with parents
1. Find the best child chunks by cosine + keyword + rerank.
2. Swap each child for its parent before building the LLM prompt.
3. The LLM gets rich context; retrieval was still precise.

This is called the **parent-document retriever** pattern.

---

## What is Query Reformulation?

**Problem:** Follow-up questions contain references that are only meaningful in context.

Turn 1:
```
User: What did Alice say about the API?
Bot:  Alice said we should use REST for the public API and gRPC internally.
```

Turn 2:
```
User: Why did she choose that?
```

If we embed "Why did she choose that?" and search, we get random results — the embedding model doesn't know what "she" or "that" refers to.

**Solution:** Before searching, detect reference words (she, it, that, they, this, those, them, their, there). If found, ask Groq to rewrite the question as a standalone:

```
Why did Alice choose REST for the public API and gRPC internally?
```

Now the embedding search works correctly.

We only rewrite when necessary (reference words detected) to avoid adding Groq latency to every query.

---

## In-Memory Workflow

```
User question
     │
     ▼
reformulate_query()          ← detect pronouns → Groq rewrite if needed
     │
     ▼
embed_texts(is_query=True)   ← bge "Represent this question: " prefix
     │
     ├──────────────────────────────────┐
     ▼                                  ▼
_semantic_search()              _keyword_search()
pgvector cosine, top-20         tsvector @@, top-20
     │                                  │
     └──────────────┬───────────────────┘
                    ▼
               _rrf_merge()     ← unified ranked list
                    │
                    ▼
           _fetch_chunks()      ← load text for top-20
                    │
                    ▼
              _rerank()         ← cross-encoder, top-5
                    │
                    ▼
      _fetch_parent_chunks()    ← swap children for parents
                    │
                    ▼
    _format_context_block()     ← [Meeting: X | Time: Y | Speaker: Z]
                                   <parent text>
                    │
                    ▼
           RetrievalResult      ← context_blocks, chunks, citations
```

---

## System Workflow (End-to-End)

```
1. Upload .vtt or .txt → POST /api/meetings/upload
2. Celery pipeline:
     parse → chunk (child + parent) → embed children → store both
3. Later — POST /api/chat:
     question + meeting_id?
         → retrieve() [Phase 6]
         → assemble prompt
         → Groq llama-3.3-70b [Phase 7]
         → parse citations
         → save to chat_messages
         → return answer + citations
```

The retrieval module sits between the chat route and the database.
It is the only consumer of the chunks table.

---

## Key Decisions & Why

| Decision | Reason |
|---|---|
| `is_query=True` prefix for bge | bge is asymmetric — query and passage embeddings use different prefixes. Using the wrong prefix degrades recall by ~10%. |
| RRF k=60 | Empirically best value across IR benchmarks. Prevents top-rank domination. |
| RERANK_TOP_N = 20, FINAL_TOP_N = 5 | Cross-encoder is slow; 20 inputs is fast. 5 context chunks fits most LLM context windows with room for the answer. |
| `run_in_executor` for embedding + reranking | Both are synchronous CPU operations. Running them in an async route handler without `run_in_executor` blocks the event loop and freezes all concurrent requests. |
| `reformulate_question` lives in `ai.py` | All Groq calls must live in `ai.py` — CLAUDE.md rule. Retrieved from there by retrieval.py via import. |
| Parent chunks have no embedding | Parents are only fetched by ID after retrieval; they are never searched directly. Storing embeddings for them would waste 1024 floats × many rows. |
