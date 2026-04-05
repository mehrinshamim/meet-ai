# Model Choices — Why These Models and Not Others

---

## All Models Used in This System

| Model | Type | Where | Purpose |
|---|---|---|---|
| `BAAI/bge-large-en-v1.5` | Embedding (local) | `services/embeddings.py` | Convert transcript chunks + user queries into 1024-dim vectors |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | Re-ranker (local) | `services/retrieval.py` (Phase 6) | Score top-20 retrieved chunks → pick best 5 |
| `llama-3.3-70b-versatile` | LLM via Groq | `services/ai.py` (Phase 4+) | Extract decisions/action items, answer chat questions, generate citations |
| `llama-3.1-8b-instant` | LLM via Groq | `services/ai.py` (Phase 7) | Query reformulation only — rewrite follow-up questions into standalone ones |

---

## bge-large-en-v1.5 vs all-MiniLM-L6-v2

`all-MiniLM-L6-v2` is the model most tutorials and MVPs use. It's a reasonable default for quick projects. Here's why this project uses something better.

### Side-by-side comparison

| Property | all-MiniLM-L6-v2 | bge-large-en-v1.5 (ours) |
|---|---|---|
| Dimensions | 384 | 1024 |
| Parameters | 22M | 335M |
| Size on disk | ~90 MB | ~1.3 GB |
| Speed (CPU) | ~14,000 sentences/sec | ~1,200 sentences/sec |
| MTEB retrieval score | ~49 | ~54+ |
| Best for | Prototypes, speed-critical apps | Production retrieval, RAG |

### Why not MiniLM for this project

**1. Speed difference doesn't matter here**

MiniLM is ~10x faster, but that only matters at scale. A 30-minute meeting produces ~200 child chunks. At 1,200 sentences/sec, bge-large embeds all of them in under 1 second. Embedding runs once per upload in a background Celery task — the user never waits for it.

**2. Meeting transcripts are harder than general text**

Meeting language is messy: incomplete sentences, interruptions, jargon, project names, speaker references. bge-large was trained specifically for retrieval tasks on diverse, noisy text. The extra 640 dimensions give it more space to encode that nuance. MiniLM with 384 dims compresses too aggressively and loses subtle meaning.

**3. Retrieval quality is the bottleneck for the whole system**

In a RAG pipeline, the embedding model determines which chunks reach the LLM. If the wrong chunks are retrieved, no amount of LLM intelligence fixes it. We're passing results to a 70B model — it deserves accurate retrieval input.

**4. 384 dims weakens hybrid search**

This app uses hybrid retrieval: semantic search (pgvector) + keyword search (tsvector) merged with RRF. The semantic score needs to be strong enough to meaningfully contribute. With 384-dim vectors the scores are noisier, making the RRF merge less reliable.

**5. MTEB benchmark**

MTEB (Massive Text Embedding Benchmark) is the standard leaderboard for embedding models. bge-large-en-v1.5 consistently scores top-3 on retrieval tasks. all-MiniLM-L6-v2 is in the top-20 but trades accuracy for speed — a tradeoff that doesn't benefit us here.

### When would you use MiniLM instead?

- Embedding millions of documents (cost and time matter at that scale)
- Very constrained hardware (< 2 GB RAM)
- Building a quick demo to test an idea, not the real product
- Application where retrieval precision is not critical (e.g., fuzzy autocomplete)

---

## Why Two LLMs (70B + 8B)?

We use two different Groq-hosted models for different jobs:

**llama-3.3-70b-versatile** — the "smart" model
- Used for: extracting decisions + action items, answering user questions, generating citations
- Why 70B: These tasks require reasoning, following complex instructions (structured JSON + citation format), and nuanced language understanding. A smaller model makes more mistakes on extraction tasks.

**llama-3.1-8b-instant** — the "fast" model
- Used for: query reformulation only (rewriting "what did she mean by that?" into a standalone question)
- Why 8B: Reformulation is a simple rewriting task. It doesn't require the full reasoning power of 70B. Using 8B here is 5-10x cheaper and faster, with no quality loss for this specific job.

The pattern: use the smallest model that does the job correctly. Don't pay for 70B when 8B is enough.

---

## Why a Cross-Encoder Re-ranker?

The embedding model (bi-encoder) encodes the query and each document chunk **separately**, then compares their vectors. This is fast but approximate — it misses subtle relevance signals because it never sees the query and document together.

The cross-encoder (`ms-marco-MiniLM-L-6-v2`) sees the **query + document concatenated** and scores their relevance jointly. This is much more accurate but too slow to run on all chunks.

The two-stage flow:
```
All chunks → pgvector cosine search → top 20 candidates
           → cross-encoder scores each (query + chunk together)
           → top 5 most relevant chunks → sent to LLM
```

`ms-marco-MiniLM-L-6-v2` specifically: 22M parameters, trained on MS-MARCO (a massive passage retrieval dataset), runs in ~100ms for 20 candidates on CPU. Accurate/speed tradeoff is ideal for this use case.
