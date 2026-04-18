# Phase 8 — Extractions & Sentiment Routes

## What these routes do

The Celery pipeline (Phase 4) already extracted decisions, action items, and
sentiment scores from every uploaded meeting. They've been sitting in the DB
the whole time. These routes simply **read that data and return it**.

No Groq calls happen here. No ML inference. Just DB reads.

```
Pipeline (Phase 4) → extractions table → GET /api/meetings/{id}/extractions
                   → sentiments table  → GET /api/meetings/{id}/sentiment
```

---

## The extractions response

```json
{
  "id": 1,
  "meeting_id": 16,
  "decisions": [
    {
      "text": "Project deadline set to end of month",
      "speaker": "Alice",
      "timestamp": "00:00:01"
    }
  ],
  "action_items": [
    {
      "task": "Update the roadmap",
      "assignee": "Bob",
      "due_date": "Friday",
      "timestamp": "00:00:11"
    }
  ],
  "created_at": "2026-04-18T17:15:25Z"
}
```

Both `decisions` and `action_items` are JSONB arrays in PostgreSQL.  SQLAlchemy
maps them to Python lists automatically — we return them as-is.

---

## The sentiment response

```json
{
  "speaker_scores": {
    "Alice": 0.82,
    "Bob": 0.41
  },
  "segment_scores": [
    {
      "chunk_id": 42,
      "speaker": "Alice",
      "start_time": "00:00:01",
      "score": 0.7,
      "label": "positive"
    }
  ]
}
```

**speaker_scores** — one score per speaker, averaged across all their turns.
Range: `-1.0` (very negative) → `+1.0` (very positive).

**segment_scores** — one entry per child chunk (speaker turn).
Each entry has a `chunk_id` which links back to the `chunks` table.
The frontend (Phase 9) uses this to implement "click a segment → see transcript":
it sends the `chunk_id` and the backend looks up the original text.

---

## CSV export

`GET /api/meetings/{id}/extractions/export` returns a downloadable `.csv` file.

```
type,text,speaker,assignee,due_date,timestamp
decision,Project deadline set to end of month,Alice,,,00:00:01
action_item,Update the roadmap,,Bob,Friday,00:00:11
```

Both decisions and action items appear in the same file with a `type` column to
tell them apart.  Columns that don't apply to a row type are left blank.

### How streaming works

```python
output = io.StringIO()          # in-memory text buffer
writer = csv.writer(output)     # stdlib csv writer
writer.writerow(["type", ...])  # write header
writer.writerow([...])          # write each row

output.seek(0)                  # rewind to start

return StreamingResponse(
    iter([output.getvalue()]),  # wrap content in an iterator
    media_type="text/csv",
    headers={"Content-Disposition": 'attachment; filename="..."'}
)
```

`io.StringIO` is an in-memory file — it behaves like a file but lives in RAM.
`StreamingResponse` tells FastAPI to send the content as a stream rather than
loading it all into a JSON response.  The browser sees the
`Content-Disposition: attachment` header and triggers a file download instead
of displaying the content.

Why `iter([output.getvalue()])`? `StreamingResponse` expects an iterable. We
wrap the single string in a one-element list so it satisfies that interface.

---

## Why 404 on unprocessed meetings

Both routes return `404` if `meeting.processed is False`.

This is intentional: an unprocessed meeting has no extractions row and no
sentiments row yet (the pipeline hasn't written them).  Returning `404` is
more honest than returning an empty response — it tells the client "this data
doesn't exist yet, try again later".

The frontend should check `processed=True` via the status endpoint before
navigating to the extractions or sentiment views.

---

## In-memory workflow

```
Client: GET /api/meetings/16/extractions
    ↓
FastAPI calls get_extractions(meeting_id=16)
    ↓
db.get(Meeting, 16)
  → check meeting exists (404 if not)
  → check meeting.processed (404 if False)
    ↓
SELECT * FROM extractions WHERE meeting_id = 16
  → get the Extraction ORM object
    ↓
Pydantic serialises it → ExtractionOut JSON
    ↓
Client receives { decisions: [...], action_items: [...] }
```

```
Client: GET /api/meetings/16/extractions/export
    ↓
FastAPI calls export_extractions_csv(meeting_id=16)
    ↓
Same DB checks as above
    ↓
Build CSV string in io.StringIO
Write header row + one row per decision + one row per action_item
    ↓
Return StreamingResponse with Content-Disposition: attachment
    ↓
Browser saves file as "standup_2024-03-01_extractions.csv"
```
