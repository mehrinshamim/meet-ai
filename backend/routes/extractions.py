"""
extractions.py — Routes for viewing and exporting decisions + action items.

Routes:
  GET /api/meetings/{id}/extractions          → JSON response
  GET /api/meetings/{id}/extractions/export   → CSV file download
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Extraction, Meeting
from backend.schemas import ExtractionOut

router = APIRouter()


@router.get("/meetings/{meeting_id}/extractions", response_model=ExtractionOut)
async def get_extractions(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Return the decisions and action items extracted from a meeting.

    Both fields are JSONB arrays stored by the Celery pipeline after Groq
    processes the transcript.  We just read and return them — no computation here.

    Returns 404 if the meeting doesn't exist or hasn't been processed yet.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    if not meeting.processed:
        raise HTTPException(status_code=404, detail="Meeting not yet processed.")

    result = await db.execute(
        select(Extraction).where(Extraction.meeting_id == meeting_id)
    )
    extraction = result.scalars().first()
    if extraction is None:
        raise HTTPException(status_code=404, detail="No extractions found for this meeting.")

    return extraction


@router.get("/meetings/{meeting_id}/extractions/export")
async def export_extractions_csv(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Stream a CSV file containing action items for a meeting.

    Why StreamingResponse?
      We build the CSV in memory using io.StringIO and stream it back.
      The browser receives it as a file download (Content-Disposition: attachment).
      stdlib csv module only — no pandas, no external deps.

    CSV columns: type, text/task, speaker, assignee, due_date, timestamp
    Decisions and action items are written as separate rows with a "type" column
    so both appear in the same file.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")
    if not meeting.processed:
        raise HTTPException(status_code=404, detail="Meeting not yet processed.")

    result = await db.execute(
        select(Extraction).where(Extraction.meeting_id == meeting_id)
    )
    extraction = result.scalars().first()

    output = io.StringIO()
    writer = csv.writer(output)

    # Header row
    writer.writerow(["type", "text", "speaker", "assignee", "due_date", "timestamp"])

    if extraction:
        for d in (extraction.decisions or []):
            writer.writerow([
                "decision",
                d.get("text", ""),
                d.get("speaker", ""),
                "",                     # assignee — not applicable for decisions
                "",                     # due_date — not applicable for decisions
                d.get("timestamp", ""),
            ])
        for a in (extraction.action_items or []):
            writer.writerow([
                "action_item",
                a.get("task", ""),
                "",                     # speaker — not tracked per action item
                a.get("assignee", ""),
                a.get("due_date", ""),
                a.get("timestamp", ""),
            ])

    output.seek(0)

    filename = meeting.filename.rsplit(".", 1)[0]   # strip extension
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}_extractions.csv"'
    }
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers=headers,
    )
