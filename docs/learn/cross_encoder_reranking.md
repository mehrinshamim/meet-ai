# Cross-Encoder Reranking — Concept Explained

---

## The Problem It Solves

When you do a vector search, you retrieve the top-K chunks based on **approximate similarity** (cosine distance). This is fast but imprecise — the embedding model compresses meaning into a fixed vector, losing nuance.

You get back 10–20 candidates, but they are not perfectly ranked for your actual question.

---

## Two Types of Models

### Bi-encoder (what embeddings use)
- Encodes query → vector, encodes document → vector **independently**
- Similarity = cosine distance between two vectors
- Fast, but the query and document never "see" each other during encoding

### Cross-encoder (what reranking uses)
- Takes query + document **together** as a single input
- Outputs a **relevance score** (0–1)
- Much slower, but far more accurate — the model reasons about both simultaneously

---

## How the Pipeline Works

```
User query
    │
    ▼
Hybrid search (semantic + keyword)
    │  retrieves top 20 candidates (fast, approximate)
    ▼
Cross-encoder reranker
    │  scores each (query, chunk) pair
    │  re-sorts by relevance score descending
    ▼
Top 3–5 chunks
    │
    ▼
LLM gets these as context
```

---

## Why Not Use Cross-Encoder for Everything?

**Speed and cost.** A cross-encoder on 10,000 chunks = 10,000 model passes. That is too slow for a live search index.

The pattern is always **retrieve cheap, rerank expensive**:
- Bi-encoder: O(1) lookup via vector index
- Cross-encoder: O(n) on a small candidate set (e.g. 20 chunks)

This two-stage design gives you the speed of vector search and the accuracy of cross-encoder scoring.

---

## In This Project

We use `sentence-transformers` which ships cross-encoder models:

```python
from sentence_transformers import CrossEncoder

model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

# pairs = list of (query, chunk_text) tuples
pairs = [
    ("what did Alice say about the deadline?", chunk1_text),
    ("what did Alice say about the deadline?", chunk2_text),
    # ... up to 20 candidates
]

scores = model.predict(pairs)
# returns a list of floats, one per pair

# sort chunks by score descending, take top 3
ranked = sorted(zip(scores, chunks), reverse=True)
top_chunks = [chunk for _, chunk in ranked[:3]]
```

The top chunks are then passed to Groq as the LLM context.

---

## Where It Lives

- Implementation: `backend/services/retrieval.py`
- All cross-encoder calls stay in `retrieval.py` (same rule as Groq calls in `ai.py`)
- Called after hybrid search (BM25 + pgvector), before the LLM prompt is built

---

## Summary

| Stage | Method | Speed | Accuracy |
|---|---|---|---|
| Retrieval | Bi-encoder (vector index) | Fast | Approximate |
| Reranking | Cross-encoder | Slow | High |
| Generation | LLM (Groq) | Moderate | — |

Cross-encoder reranking is the bridge between "good enough" retrieval and "actually useful" answers.
