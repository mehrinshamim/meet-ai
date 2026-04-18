"""
ai.py — All Groq LLM calls. Nowhere else.

Public functions:
    extract_decisions_and_actions(transcript, filename) → dict
    analyze_sentiment(segments) → dict
"""

from __future__ import annotations

import json
import logging
import time

from groq import APIError, Groq, RateLimitError

from backend.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"
MAX_TRANSCRIPT_CHARS = 60_000   # leave headroom for system prompt + output
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1            # seconds; doubles each attempt: 1s, 2s, 4s
MAX_SENTIMENT_SEGMENTS = 80     # cap to avoid token overflow on long meetings

_client: Groq | None = None


def get_client() -> Groq:
    """Return the cached Groq client, creating it on first call."""
    global _client
    if _client is None:
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def _call_groq(messages: list[dict]) -> str:
    """
    Call Groq with exponential-backoff retry on rate limits and transient errors.
    Always uses json_object response format. Returns the raw JSON string.
    """
    client = get_client()
    last_err: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content

        except RateLimitError as exc:
            wait = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("Groq rate limit (attempt %d/%d) — waiting %ds", attempt + 1, MAX_RETRIES, wait)
            time.sleep(wait)
            last_err = exc

        except APIError as exc:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("Groq API error (attempt %d/%d) — waiting %ds: %s", attempt + 1, MAX_RETRIES, wait, exc)
                time.sleep(wait)
                last_err = exc
            else:
                raise

    raise last_err  # type: ignore[misc]


def extract_decisions_and_actions(transcript: str, filename: str) -> dict:
    """
    Extract decisions and action items from a meeting transcript.

    Returns:
        {
          "decisions":    [{"text", "timestamp", "speaker"}],
          "action_items": [{"task", "assignee", "due_date", "timestamp"}]
        }
    """
    truncated = transcript[:MAX_TRANSCRIPT_CHARS]
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        logger.warning("Transcript truncated from %d to %d chars", len(transcript), MAX_TRANSCRIPT_CHARS)

    system_prompt = (
        "You are a precise meeting analysis assistant. "
        "Extract only what is explicitly stated in the transcript. "
        "Do not infer or add information not present. "
        "Return only valid JSON."
    )

    user_prompt = f"""Extract all decisions and action items from this meeting transcript.

Meeting file: {filename}

Transcript:
{truncated}

Return JSON in exactly this format (no markdown, no explanation — just JSON):
{{
  "decisions": [
    {{
      "text": "description of the decision",
      "timestamp": "HH:MM:SS or empty string if unknown",
      "speaker": "name of person who made/announced the decision, or empty string"
    }}
  ],
  "action_items": [
    {{
      "task": "what needs to be done",
      "assignee": "who is responsible, or empty string",
      "due_date": "due date mentioned, or empty string",
      "timestamp": "HH:MM:SS when this was assigned, or empty string"
    }}
  ]
}}

If no decisions or action items are found, return empty arrays for those fields."""

    raw = _call_groq([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Groq returned non-JSON for extraction (file=%s): %.200s", filename, raw)
        data = {}

    return {
        "decisions":    data.get("decisions") or [],
        "action_items": data.get("action_items") or [],
    }


def analyze_sentiment(segments: list[dict]) -> dict:
    """
    Score sentiment per speaker and per chunk segment.

    Args:
        segments: list of {"index", "chunk_id", "speaker", "start_time", "text"}
                  chunk_id is not sent to Groq — used by pipeline.py to map results back.

    Returns:
        {
          "speaker_scores": {"Alice": 0.3, "Bob": -0.1},
          "segment_scores": [{"segment_index": 0, "score": 0.3, "label": "positive"}, ...]
        }
    """
    if not segments:
        return {"speaker_scores": {}, "segment_scores": []}

    capped = segments[:MAX_SENTIMENT_SEGMENTS]
    if len(segments) > MAX_SENTIMENT_SEGMENTS:
        logger.warning("Sentiment capped at %d segments (total: %d)", MAX_SENTIMENT_SEGMENTS, len(segments))

    # Format as numbered list so Groq can reference each segment by index in its output
    lines = [
        f"[{seg['index']}] [{seg.get('start_time') or '?'}] {seg.get('speaker') or 'Unknown'}: {seg.get('text', '')}"
        for seg in capped
    ]
    segments_text = "\n".join(lines)
    if len(segments_text) > MAX_TRANSCRIPT_CHARS:
        segments_text = segments_text[:MAX_TRANSCRIPT_CHARS]

    system_prompt = (
        "You are a sentiment analysis assistant for business meetings. "
        "Analyze tone, word choice, and emotional context — not just surface positivity. "
        "Return only valid JSON."
    )

    user_prompt = f"""Analyze the sentiment of each speaker and each segment in this meeting transcript.

Segments (format: [index] [timestamp] Speaker: text):
{segments_text}

Return JSON in exactly this format:
{{
  "speaker_scores": {{
    "SpeakerName": 0.5
  }},
  "segment_scores": [
    {{"segment_index": 0, "score": 0.3, "label": "positive"}}
  ]
}}

Rules:
- score: float from -1.0 (very negative) to 1.0 (very positive), 0.0 = neutral
- label: one of "positive", "neutral", "negative"
- Include a score for EVERY speaker mentioned and EVERY segment index provided
- speaker_scores values are the average sentiment across all that speaker's segments
- Be precise: distinguish between factual/neutral statements and emotionally charged language"""

    raw = _call_groq([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Groq returned non-JSON for sentiment: %.200s", raw)
        data = {}

    return {
        "speaker_scores": data.get("speaker_scores") or {},
        "segment_scores": data.get("segment_scores") or [],
    }


def reformulate_question(question: str, chat_history: list[dict]) -> str:
    """
    Rewrite a follow-up question into a standalone question using chat history.

    Args:
        question:     The user's latest message (may contain pronouns/references).
        chat_history: Prior turns [{"question": str, "answer": str}, ...] oldest first.

    Returns:
        A rewritten, self-contained question string.
        Falls back to original question if the model returns something unexpected.
    """
    # Build a condensed history string — last 3 turns is enough for context
    recent = chat_history[-3:]
    history_lines = []
    for turn in recent:
        history_lines.append(f"User: {turn['question']}")
        history_lines.append(f"Assistant: {turn['answer'][:200]}...")  # truncate long answers
    history_text = "\n".join(history_lines)

    system_prompt = (
        "You are a question rewriter. "
        "Given a conversation history and a follow-up question, rewrite the follow-up "
        "as a complete, standalone question that does not rely on the prior context. "
        "Return only the rewritten question as a plain string — no JSON, no explanation."
    )

    user_prompt = f"""Conversation so far:
{history_text}

Follow-up question: {question}

Rewrite this as a standalone question that can be understood without the conversation above.
Return ONLY the rewritten question."""

    # We don't use json_object format here — it's a plain string response.
    client = get_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.0,
    )
    rewritten = response.choices[0].message.content.strip()

    # Sanity check — if the model returned something very short or empty, use original
    if len(rewritten) < 5:
        return question

    return rewritten
