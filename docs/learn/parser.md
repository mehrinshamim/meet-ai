# parser.py — How the Transcript Parser Works

> File: `backend/services/parser.py`
> Phase: 2 — File Parser + Chunker

---

## What it does in one sentence

It reads a raw transcript file (`.vtt` or `.txt`), breaks it into small pieces called **chunks**, and organises those chunks into a two-level structure (child + parent) that the rest of the RAG pipeline can work with.

---

## Why we need it

When someone uploads a meeting transcript, the file is too large to send directly to an LLM. A 2-hour meeting transcript is roughly 15,000–30,000 tokens. LLMs have context limits, and even if they didn't, stuffing the entire transcript into every answer would be slow and expensive.

Instead we:
1. Break the transcript into small, focused pieces (~400 tokens each)
2. Turn each piece into a vector embedding (Phase 3)
3. When a user asks a question, retrieve only the 5 most relevant pieces
4. Send just those pieces to the LLM as context

The parser is step 1 of this pipeline.

---

## The two file formats

### WebVTT (`.vtt`)
The standard subtitle/caption format exported by Zoom, Teams, Google Meet.

```
WEBVTT

00:00:01.000 --> 00:00:08.000
<v Alice>Good morning. Let us start with the roadmap.</v>

00:00:09.000 --> 00:00:14.000
<v Bob>I agree. Dashboard should be top priority.</v>
```

- Each block is called a **cue**. It has a start time, end time, and text.
- Speaker is encoded in the `<v Speaker>` tag (W3C format) — or as `Speaker: text` (common Zoom export).
- We use the `webvtt-py` library to read cues. It strips the `<v>` tags from `caption.text`, so we read speaker from `caption.raw_text` instead.

### Plain text (`.txt`)
Simpler meeting notes format, often from manual transcription tools.

```
[00:02:10] Alice: Good morning. Let us start with the roadmap.
[00:02:18] Bob: I agree.
```

- No library needed — we use a regular expression to detect the pattern `[timestamp] Speaker: text`.
- Lines that don't start with a speaker name are appended to the previous turn.
- Timestamps are optional — the parser handles both `Speaker: text` and `[HH:MM:SS] Speaker: text`.

---

## Data structures

### `Turn`
The rawest unit — a single speaker utterance directly as parsed from the file, before any chunking decisions.

```python
@dataclass
class Turn:
    speaker: str | None      # "Alice", "Bob", or None if not detected
    start_time: str | None   # "00:02:10" (HH:MM:SS)
    end_time: str | None     # "00:02:18"
    text: str                # "Good morning. Let us start with the roadmap."
```

Think of a `Turn` as one line in the raw transcript — it's just "what was said and by whom".

### `ChunkData`
The unit that gets stored in the database. After chunking, each `Turn` becomes one or more `ChunkData` objects.

```python
@dataclass
class ChunkData:
    speaker: str | None
    start_time: str | None
    end_time: str | None
    text: str
    token_count: int          # estimated token count for this chunk
    is_parent: bool           # False = child chunk, True = parent chunk
    parent_index: int | None  # index into the parent_chunks list
```

The `is_parent` flag is the key distinction — explained fully below.

### `ParseResult`
What the pipeline task receives after calling `parse_transcript()`.

```python
@dataclass
class ParseResult:
    child_chunks: list[ChunkData]   # small chunks for embedding + retrieval
    parent_chunks: list[ChunkData]  # large 5-min windows for LLM context
    speaker_names: list[str]        # ["Alice", "Bob"] in order of appearance
    word_count: int                 # total words in the meeting
    meeting_date: datetime | None   # extracted from filename if present
```

---

## Step-by-step workflow

```
raw file content
      │
      ▼
 parse_vtt()          ← uses webvtt-py, reads <v> tags from raw_text
 or parse_txt()       ← regex match on [HH:MM:SS] Speaker: text
      │
      ▼
 list[Turn]           ← one Turn per speaker utterance
      │
      ▼
 chunk_turns()        ← merge short turns, split long turns
      │
      ▼
 list[ChunkData]      ← child chunks, ~400 tokens each, is_parent=False
      │
      ▼
 build_parent_chunks()← group by 5-minute windows
      │
      ▼
 (child_chunks, parent_chunks)  ← children have parent_index set
      │
      ▼
 extract_metadata()   ← speakers, word_count, date from filename
      │
      ▼
 ParseResult          ← returned to the Celery pipeline task
```

---

## Chunking logic in detail

### Why not just split by character count or word count?

Splitting mid-sentence or mid-thought destroys meaning. A chunk like:

> "…and that's why we decided to"

has no semantic value on its own. Embedding it would produce a vector that doesn't represent any real concept.

We split at **speaker-turn boundaries** instead — each chunk starts and ends at a natural speech boundary.

### Pass 1: Merge short same-speaker turns

If Alice says "Yes." and then "I agree with that." in two consecutive turns, we merge them into one chunk — they're semantically the same thought, and a 2-word chunk would produce a weak, low-information embedding.

**Rule:** If consecutive turns have the same speaker AND the merged size stays under 400 tokens → merge.

### Pass 2: Split long turns

If Alice delivers a 5-minute monologue (800+ tokens), one giant chunk is also bad — the embedding would be too diluted to retrieve precisely.

**Rule:** If a turn exceeds 600 tokens, split at sentence boundaries (`. ? !`), targeting ~400 tokens per chunk. Timestamps are interpolated linearly across the split.

### Why 400 tokens?

This is the RAG sweet spot for English prose:
- Small enough that the embedding represents one focused idea
- Large enough to contain sufficient context for a meaningful answer
- Leaves plenty of room in the LLM context window for multiple retrieved chunks

---

## Parent-child chunk pattern

This is the most important architectural decision in the chunking system.

### The problem it solves

Vector retrieval finds the most semantically similar chunk to the user's question. But a 400-token chunk often lacks enough context — the answer might reference something said 2 minutes earlier.

Sending a larger chunk (e.g. 2000 tokens) would hurt retrieval precision — the embedding would average across too many different topics.

**Solution:** store two levels simultaneously.

### How it works

```
Meeting transcript (60 minutes)
│
├── Parent chunk 0 (00:00:00 – 00:05:00)  ← 5-minute window, ~1200 tokens
│     contains all text from minutes 0–5
│     ├── child chunk 0  (00:00:01, Alice)  ← ~400 tokens, has embedding
│     ├── child chunk 1  (00:00:45, Bob)    ← ~400 tokens, has embedding
│     └── child chunk 2  (00:02:10, Alice)  ← ~400 tokens, has embedding
│
├── Parent chunk 1 (00:05:00 – 00:10:00)
│     ├── child chunk 3  (00:05:20, Bob)
│     └── child chunk 4  (00:07:45, Alice)
│
└── ...
```

**Retrieval (Phase 6):**
1. User asks a question → embed the question
2. Find the 20 most similar *child* chunks by cosine distance (precise retrieval)
3. Rerank to top 5 using a cross-encoder
4. For each winning child, fetch its *parent* chunk
5. Send the parent text (full 5-min window) to the LLM as context

The child chunk gets you to the right *place* in the meeting. The parent chunk gives the LLM the *surrounding conversation* needed to formulate a complete answer.

### In the database

The `chunks` table has:
- `is_parent: bool` — distinguishes the two levels
- `parent_id: bigint` — child rows point to their parent row's id
- `embedding: vector(1024)` — only set on child chunks (parents don't need it)

The `parent_index` field in `ChunkData` is a temporary list index resolved to a real database `id` by the Celery pipeline task (Phase 4).

---

## Timestamp handling

All timestamps are normalised to `HH:MM:SS` (no milliseconds).

- VTT gives `00:00:01.500` → stripped to `00:00:01`
- Short form `02:10` → padded to `00:02:10`

For parent chunks, the `start_time` is the first child's start and `end_time` is the last child's end.

For split long turns (Pass 2 of chunking), timestamps are linearly interpolated:

```
original turn: 00:01:00 → 00:06:00 (300 seconds total)
text is 600 words total

split chunk 1 covers words 0–300 (first half)
  → start: 00:01:00, end: 00:03:30

split chunk 2 covers words 300–600 (second half)
  → start: 00:03:30, end: 00:06:00
```

Not exact (words don't map perfectly to time), but good enough for citation display in the UI.

---

## Metadata extraction

### Speaker names

Collected by scanning all `Turn` objects and recording each new speaker in order of first appearance. The order matters — it's used to assign colors in the sentiment timeline UI.

### Word count

Total words across all turns. Used in the meeting cards on the dashboard (e.g. "4,200 words").

### Meeting date

Looked for in the filename using the pattern `YYYY-MM-DD` or `YYYY_MM_DD`. If found, stored in `meetings.meeting_date`. If not, stored as `NULL`.

Examples:
- `standup_2024-03-15.vtt` → `2024-03-15`
- `team_sync_2024_07_22.txt` → `2024-07-22`
- `recording.vtt` → `NULL`

---

## Where this fits in the full system

```
Upload (Phase 5)
  → File saved, Meeting row created, Celery task enqueued

Celery pipeline task (Phase 4)
  → Calls parse_transcript(content, filename)       ← this file
  → Calls embed_chunks(child_chunks)                 (Phase 3)
  → Inserts parent chunks to DB, get their IDs
  → Inserts child chunks with parent_id set
  → Calls Groq for extractions + sentiment          (Phase 4)
  → Marks meeting.processed = True

RAG query (Phase 6)
  → Embeds user question
  → Finds top child chunks by cosine + keyword search
  → Fetches parent chunks for LLM context
  → Reranks with cross-encoder
  → Passes to Groq with citations                   (Phase 7)
```

The parser output (`ParseResult`) is what the Celery task receives and acts on. Everything downstream — embeddings, retrieval, citations — depends on the quality of the chunks produced here.
