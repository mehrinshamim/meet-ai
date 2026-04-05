# hardware-ram.md — Running on 8 GB RAM, No GPU

---

## Short Answer

Yes, this project runs fine on 8 GB RAM with no GPU. Peak usage is ~4.5 GB, leaving ~3.5 GB headroom.

---

## RAM Usage Breakdown (Everything Running at Once)

| Component | RAM |
|---|---|
| OS + browser + background apps | ~2.0 GB |
| Docker (PostgreSQL + Redis) | ~400 MB |
| FastAPI (uvicorn process) | ~150 MB |
| Celery worker process | ~200 MB |
| bge-large-en-v1.5 (loaded into Celery) | ~1.3 GB |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | ~100 MB |
| PyTorch CPU runtime overhead | ~300 MB |
| **Total peak** | **~4.5 GB** |

---

## No GPU — Is That a Problem?

No. All local models run on CPU without any changes:

- **bge-large** on CPU: ~1,200 sentences/second. A 30-min meeting (~200 chunks) embeds in under 1 second.
- **cross-encoder** on CPU: scores 20 candidate chunks in ~100ms at query time.
- **Groq LLMs (llama-3.3-70b, llama-3.1-8b)** run on Groq's servers — they never touch your RAM at all.

The only slow moment is the first embed after starting the Celery worker (~2-3 seconds to load the model). After that, the singleton keeps it in RAM and every call is fast.

---

## If You Run Into Memory Issues

If the machine becomes sluggish or the Celery worker crashes with `MemoryError`, switch from `bge-large` to `bge-base`. The quality difference is minimal (~1 MTEB point) but the RAM saving is ~870 MB.

### Comparison

| Property | bge-large (current) | bge-base (fallback) |
|---|---|---|
| Dimensions | 1024 | 768 |
| RAM in use | ~1.3 GB | ~430 MB |
| MTEB retrieval score | ~54 | ~53 |
| Speed (CPU) | ~1,200 sent/sec | ~3,500 sent/sec |

### Code changes required to switch

**1. `backend/services/embeddings.py`** — change model name and dimension:

```python
# Before
MODEL_NAME = "BAAI/bge-large-en-v1.5"
VECTOR_DIM = 1024

# After
MODEL_NAME = "BAAI/bge-base-en-v1.5"
VECTOR_DIM = 768
```

**2. `backend/models.py`** — change the vector column size:

```python
# Before
VECTOR_DIM = 1024

# After
VECTOR_DIM = 768
```

**3. Generate and run a new Alembic migration** to resize the `embedding` column:

```bash
uv run alembic revision --autogenerate -m "resize vector dim to 768"
uv run alembic upgrade head
```

> Warning: You must re-embed all existing chunks after this change. The old 1024-dim vectors are incompatible with a 768-dim column. If you have data in the DB, delete all chunk rows and re-process your meetings.

---

## When to Switch

Switch to bge-base only if:
- Celery worker crashes with `MemoryError` during embedding
- The machine is noticeably slow while processing uploads
- You're running on a machine with less than 6 GB RAM

Otherwise, keep bge-large. The accuracy is better and 8 GB is enough.
