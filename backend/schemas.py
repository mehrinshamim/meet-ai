"""
schemas.py — Pydantic models for API request/response validation.

Pydantic does two things for us:
  1. Validates incoming data (e.g. POST body must have a "name" field).
  2. Serialises outgoing data (e.g. converts a SQLAlchemy ORM object to JSON).

The "Out" suffix = response shape (what the API returns).
The "Create" suffix = request shape (what the client sends).

model_config = {"from_attributes": True}
  Tells Pydantic it can read attributes directly from SQLAlchemy ORM objects,
  not just from plain dicts.  Without this, you'd have to convert every ORM
  object to a dict manually before returning it.
"""

from datetime import datetime
from pydantic import BaseModel


# ─── Projects ─────────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: datetime
    meeting_count: int = 0
    action_item_count: int = 0

    model_config = {"from_attributes": True}


# ─── Meetings ─────────────────────────────────────────────────────────────────

class MeetingOut(BaseModel):
    id: int
    project_id: int | None
    filename: str
    file_format: str
    processed: bool
    task_id: str | None
    error: str | None
    speaker_names: list | None
    word_count: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MeetingStatusOut(BaseModel):
    """
    Returned by GET /api/meetings/{id}/status.

    task_status values (from Celery):
      PENDING  — task not yet picked up by a worker
      STARTED  — worker is actively running the pipeline
      SUCCESS  — pipeline finished; meeting.processed will be True
      FAILURE  — pipeline crashed; meeting.error will have the reason
      RETRY    — Groq call failed, worker is about to retry
    """
    processed: bool
    task_status: str
    error: str | None


# ─── Extractions ──────────────────────────────────────────────────────────────

class ExtractionOut(BaseModel):
    id: int
    meeting_id: int
    decisions: list | None
    action_items: list | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Sentiment ────────────────────────────────────────────────────────────────

class SentimentOut(BaseModel):
    id: int
    meeting_id: int
    speaker_scores: dict | None      # {"Alice": 0.82, "Bob": -0.14}
    segment_scores: list | None      # [{chunk_id, speaker, start_time, score, label}]
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Stats ────────────────────────────────────────────────────────────────────

class StatsOut(BaseModel):
    total_meetings: int
    processed_meetings: int
    total_projects: int
    total_decisions: int
    total_action_items: int


# ─── Chat ─────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    meeting_id: int | None = None      # None = cross-meeting query
    session_id: str | None = None      # None → server generates one


class ChatOut(BaseModel):
    id: int
    session_id: str
    meeting_id: int | None
    question: str
    answer: str
    citations: list | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatHistoryOut(BaseModel):
    session_id: str
    messages: list[ChatOut]
