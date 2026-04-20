# Frontend — MeetAI Phase 9

## Why Vanilla JS + ES Modules?

No framework, no bundler. Modern browsers support `<script type="module">` natively, which gives us `import`/`export` without webpack or npm. Each page loads only the script it needs.

**No bundler** means: open the HTML file in a browser and it works. No build step.

## api.js — Shared Fetch Wrapper

Every API call goes through one place (`api.js`). This means:
- The base URL is in one spot (`http://localhost:8000/api`)
- Error handling (non-200 → throw with the server's message) is centralized
- Changing the backend URL only requires editing one file

Three exported functions:
- `getJson(path)` — GET and parse JSON
- `postJson(path, body)` — POST with `Content-Type: application/json`
- `postForm(path, formData)` — POST multipart form (for file uploads, no Content-Type header set manually — browser does it with boundary)
- `downloadUrl(path)` — returns the full URL string (used for CSV export link `href`)

## Upload Flow

1. User drops/selects a file
2. `setFile()` validates the extension client-side first (fast feedback)
3. `postForm()` sends multipart: `file` + optional `project_id`
4. Server returns immediately with `meeting.id`
5. `startPolling()` calls `GET /api/meetings/{id}/status` every 2 seconds
6. When `processed=true` → show "View Meeting" link
7. If `error` is set → show error message

**Why poll?** The backend queues processing via Celery. It takes 20–60 seconds. Making the user wait for the HTTP response would timeout. Upload-and-poll is the standard pattern for async jobs.

## Meeting Page — Three Panels

The meeting page uses **tab switching**: clicking a tab toggles `display:none / display:block` on the corresponding `.panel` div. No routing, no framework.

### Panel 1: Extractions

Fetches `GET /api/meetings/{id}/extractions` → `{decisions, action_items}`.
- Decisions: rendered as a styled list with speaker + timestamp metadata
- Action items: rendered as a table (task, assignee, due_date, timestamp)
- Export CSV: just an `<a href="...">` pointing to the export endpoint — the browser triggers a file download natively

### Panel 2: Sentiment

Fetches `GET /api/meetings/{id}/sentiment` → `{speaker_scores, segment_scores}`.

**Bar chart (Canvas 2D):**
- `canvas.getContext("2d")` gives a 2D drawing API
- We draw horizontal bars from center (score=0). Score > 0 extends right (green), score < 0 extends left (red)
- The score range −1…+1 is mapped linearly to bar width
- We draw directly with `fillRect()` — no chart library

**Segment timeline:**
- Each segment is a colored `div` (green/gray/red based on `label`)
- Clicking shows details in a detail box below
- `chunk_id` is preserved so Phase 10 can add a fetch to show original text

### Panel 3: Chat

**session_id** — groups messages into a conversation thread. Stored in `localStorage` keyed by meeting id, so:
- First visit: no session, server generates one on the first `/api/chat` call
- Return visit: we load history from `GET /api/chat/history?session_id=...`

**Scope toggle:**
- "This meeting": sends `meeting_id` in the request → retrieval restricted to this file's chunks
- "All meetings": omits `meeting_id` → searches across all uploaded transcripts

**Citations**: the server returns `[{meeting, time, speaker}]` parsed from the LLM answer. We render them as small chips below each bot reply.

## CSS — No Framework

Single `style.css` using:
- **CSS custom properties** (`--primary`, `--bg`, etc.) for consistent colors
- **CSS Grid** for the stats bar and project cards
- **Flexbox** for the nav, buttons, chat layout
- **No class-heavy utility approach** — components have semantic class names (`.card`, `.tab`, `.badge`)

Everything is responsive at 720px (extractions grid collapses to 1 column).
