from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from backend.database import Base

VECTOR_DIM = 1024  # bge-large-en-v1.5 output dimension


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    meetings: Mapped[list["Meeting"]] = relationship("Meeting", back_populates="project")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_format: Mapped[str] = mapped_column(String(10), nullable=False)  # "vtt" or "txt"
    speaker_names: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # ["Alice", "Bob"]
    word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meeting_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Celery task ID
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    project: Mapped["Project | None"] = relationship("Project", back_populates="meetings")
    chunks: Mapped[list["Chunk"]] = relationship("Chunk", back_populates="meeting")
    extractions: Mapped[list["Extraction"]] = relationship("Extraction", back_populates="meeting")
    sentiments: Mapped[list["Sentiment"]] = relationship("Sentiment", back_populates="meeting")


class Chunk(Base):
    """
    Stores speaker-turn segments of a transcript.

    Two levels:
    - child chunks (~400 tokens): used for vector retrieval
    - parent chunks (~5-min windows): fetched after retrieval to give the LLM full context

    Each child chunk points to its parent via parent_id.
    """
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "HH:MM:SS"
    end_time: Mapped[str | None] = mapped_column(String(20), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Vector embedding (child chunks only — parents don't need it)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(VECTOR_DIM), nullable=True)

    # Full-text search column — populated by PostgreSQL to_tsvector() in migration
    # Updated via trigger or explicit UPDATE after insert
    search_vector: Mapped[object | None] = mapped_column(TSVECTOR, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="chunks")
    children: Mapped[list["Chunk"]] = relationship("Chunk", foreign_keys=[parent_id])


class Extraction(Base):
    """
    Decisions and action items extracted by Groq from a meeting.

    decisions: [{"text": "...", "timestamp": "HH:MM:SS", "speaker": "..."}]
    action_items: [{"task": "...", "assignee": "...", "due_date": "...", "timestamp": "..."}]
    """
    __tablename__ = "extractions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    decisions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    action_items: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="extractions")


class Sentiment(Base):
    """
    Sentiment scores for a meeting.

    speaker_scores: {"Alice": 0.82, "Bob": -0.14}
    segment_scores: [{"chunk_id": 42, "speaker": "Alice", "start_time": "00:02:10",
                       "score": 0.7, "label": "positive"}]
    """
    __tablename__ = "sentiments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False
    )
    speaker_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    segment_scores: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    meeting: Mapped["Meeting"] = relationship("Meeting", back_populates="sentiments")


class ChatMessage(Base):
    """
    One Q&A turn in a chat session.

    session_id groups messages into a conversation thread.
    citations: [{"meeting": "filename", "timestamp": "HH:MM:SS", "speaker": "Alice"}]
    """
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False)
    meeting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True
    )  # NULL means cross-meeting query
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
