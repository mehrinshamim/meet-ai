"""
sentiment.py — Route for viewing per-speaker and per-segment sentiment scores.

Routes:
  GET /api/meetings/{id}/sentiment → JSON response
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Meeting, Sentiment
from backend.schemas import SentimentOut

router = APIRouter()


@router.get("/meetings/{meeting_id}/sentiment", response_model=SentimentOut)
async def get_sentiment(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Return sentiment scores for a meeting.

    Response shape:
      speaker_scores:  { "Alice": 0.82, "Bob": -0.14 }
                       One score per speaker — average across all their segments.
                       Range: -1.0 (very negative) to +1.0 (very positive).

      segment_scores:  [
                         {
                           "chunk_id":   42,          ← link back to the chunk row
                           "speaker":    "Alice",
                           "start_time": "00:02:10",
                           "score":      0.7,
                           "label":      "positive"   ← "positive" / "neutral" / "negative"
                         },
                         ...
                       ]

    The chunk_id in each segment_score is intentional — the frontend (Phase 9)
    uses it for "click to view": clicking a segment on the sentiment timeline
    shows the original transcript text for that chunk.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    if not meeting.processed:
        raise HTTPException(status_code=404, detail="Meeting not yet processed.")

    result = await db.execute(
        select(Sentiment).where(Sentiment.meeting_id == meeting_id)
    )
    sentiment = result.scalars().first()
    if sentiment is None:
        raise HTTPException(status_code=404, detail="No sentiment data found for this meeting.")

    return sentiment
