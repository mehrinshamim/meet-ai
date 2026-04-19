# Phase 7 — Chat Route

## What this phase builds

A conversational Q&A endpoint that lets a user ask questions about meeting transcripts and get grounded answers with citations — backed by the hybrid RAG pipeline built in Phase 6.

---

## The two endpoints

### POST /api/chat

```
Request:  { question, meeting_id?, session_id? }
Response: { id, session_id, meeting_id, question, answer, citations, created_at }
```

`meeting_id` is optional. If given, retrieval is restricted to chunks from that meeting. If null, the query searches across all meetings.

`session_id` is optional. If the client doesn't send one, the server generates a UUID. Grouping messages by session_id is what makes multi-turn conversation possible.

### GET /api/chat/history?session_id=

Returns all Q&A turns for a session, oldest first.

---

## How a single chat turn works

```
Client  →  POST /api/chat
           │
           ├─ 1. Validate meeting_id (if given) → 404 if not found or not processed
           ├─ 2. Resolve session_id (use provided or generate UUID)
           ├─ 3. Load prior chat_messages for this session → list of {question, answer}
           ├─ 4. retrieve(query, session, meeting_id, chat_history)
           │       ├─ reformulate_query()  ← rewrites follow-ups if chat_history exists
           │       ├─ semantic search (pgvector cosine)
           │       ├─ keyword search (tsvector)
           │       ├─ RRF merge
           │       ├─ cross-encoder rerank → top-5
           │       └─ fetch parent chunks → context_blocks
           ├─ 5. answer_question(reformulated_query, context_blocks) → answer string
           ├─ 6. parse_citations(answer) → [{meeting, timestamp, speaker}, ...]
           ├─ 7. INSERT into chat_messages
           └─ 8. Return ChatOut
```

---

## What is a citation?

When the LLM answers, it's instructed to embed inline citations in this format:

```
Alice confirmed the launch would slip two weeks
[[meeting: standup_2024.vtt, time: 00:05:30, speaker: Alice]].
```

`parse_citations()` uses a regex to pull these out and returns a list of dicts, which is stored as JSONB in the `chat_messages` table. The frontend can use this to link parts of the answer back to the source transcript.

---

## Why store the original question, not the reformulated one?

The reformulated question is an internal artefact — it's what retrieval uses to find the right chunks. But the user typed the original question, so that's what appears in the chat UI. The reformulated version would look weird: "Who proposed the decision to delay the launch?" instead of "Who proposed it?".

---

## What is session_id?

A session is just a string that groups messages. There's no session table — we simply filter `chat_messages` by `session_id`.

The client generates one UUID when the user opens a chat window and sends it with every message. When the page reloads, the client can re-send the same `session_id` and call `GET /api/chat/history` to reconstruct the thread.

---

## Multi-turn conversation flow

Turn 1:
```
User: "What was the most important decision?"
→ No history → retrieval runs on the raw question
→ Answer: "The team decided to delay the launch by two weeks."
→ Saved to chat_messages
```

Turn 2:
```
User: "Who proposed it?"
→ History: [{question: "What was the most important decision?", answer: "...delay..."}]
→ reformulate_query() rewrites to: "Who proposed the decision to delay the launch?"
→ Retrieval runs on the rewritten question → finds relevant speaker chunk
→ Answer: "Alice proposed the delay during the status update."
```

Without reformulation, "it" has no referent and retrieval would return garbage results.

---

## Why answer_question() is synchronous

`_call_groq()` and `answer_question()` use the Groq SDK which is a synchronous HTTP client (`requests` under the hood). FastAPI is async, but it's fine to call sync functions from an async route for I/O-bound operations that are fast and infrequent (one per request). For heavy CPU work you'd use `run_in_executor`, but for a single HTTP call it's unnecessary overhead.

---

## In-memory workflow

```
answer_question(question, context_blocks) → str
  │
  ├─ if context_blocks is empty: return "could not find..." early
  ├─ join context_blocks with "---" separator
  ├─ build system + user prompt with citation instruction
  └─ call Groq (llama-3.3-70b-versatile, temperature=0.0) → raw string

parse_citations(answer) → list[dict]
  │
  ├─ regex: \[\[meeting:\s*(.*),\s*time:\s*(.*),\s*speaker:\s*(.*)\]\]
  ├─ iterate matches → (meeting, timestamp, speaker) tuples
  ├─ deduplicate using a seen set
  └─ return [{meeting, timestamp, speaker}, ...]
```

---

## System workflow

```
PostgreSQL (chat_messages)
  │
  ├─ session_id groups messages into threads
  ├─ meeting_id is nullable (NULL = cross-meeting)
  ├─ question = original user text
  ├─ answer = full LLM response string (includes [[...]] citation markers)
  └─ citations = JSONB list extracted by parse_citations()

FastAPI
  ├─ POST /api/chat  → creates one row per turn
  └─ GET  /api/chat/history  → reads rows by session_id, ordered by created_at
```
