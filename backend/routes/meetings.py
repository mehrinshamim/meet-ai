"""
routes/meetings.py — HTTP endpoints for meetings, projects, and dashboard stats.

All routes are async because our DB driver (asyncpg) is async.
Using sync DB calls inside an async route would block the entire event loop,
freezing all other requests while one DB query runs.

Route overview:
  POST   /api/meetings/upload          — upload a transcript file
  GET    /api/meetings/{id}/status     — poll processing progress
  GET    /api/projects                 — list projects with stats
  POST   /api/projects                 — create a project
  GET    /api/stats                    — global dashboard numbers

Why use APIRouter instead of putting routes directly on the app?
  A router is a mini-app you can mount at a prefix.  It keeps each domain
  (meetings, chat, extractions) in its own file.  main.py just collects them.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Extraction, Meeting, Project
from backend.schemas import MeetingOut, MeetingStatusOut, ProjectCreate, ProjectOut, StatsOut
from backend.tasks.celery_app import celery_app
from backend.tasks.pipeline import process_meeting

router = APIRouter()

# Allowed file extensions — reject anything else at the boundary
ALLOWED_EXTENSIONS = {"txt", "vtt"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _file_extension(filename: str) -> str:
    """Return the lowercased extension without the dot, e.g. 'vtt'."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _get_celery_task_status(task_id: str | None) -> str:
    """
    Ask Celery (via Redis) for the current state of a task.

    AsyncResult is a thin wrapper — it queries Redis synchronously.
    The call is so fast (sub-millisecond) that blocking the event loop
    briefly here is acceptable.  For production scale, wrap in
    asyncio.get_event_loop().run_in_executor() instead.

    States: PENDING → STARTED → SUCCESS / FAILURE / RETRY
    """
    if task_id is None:
        return "PENDING"
    result = celery_app.AsyncResult(task_id)
    return result.state


# ─── Upload ───────────────────────────────────────────────────────────────────

@router.post("/meetings/upload", response_model=MeetingOut, status_code=201)
async def upload_meeting(
    file: UploadFile = File(...),
    project_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept a .txt or .vtt transcript file and start background processing.

    Steps:
      1. Validate file extension — 422 if not .txt or .vtt
      2. Read file content into memory as a UTF-8 string
      3. Create a Meeting row in the DB (processed=False)
      4. Dispatch process_meeting Celery task (non-blocking)
      5. Save the Celery task_id on the meeting row
      6. Return the meeting immediately — don't wait for processing

    Why return before processing is done?
      Embedding + Groq calls take 10–30 seconds.  Making the client wait
      for that would feel broken.  Instead: respond instantly with the
      meeting ID, let the client poll /status until processed=True.

    Why Form() for project_id instead of JSON body?
      Multipart file uploads can't mix with a JSON body in HTTP.
      All non-file fields in a multipart request must use Form().
    """
    ext = _file_extension(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '.{ext}'. Only .txt and .vtt are accepted.",
        )

    # Read content — decode as UTF-8, fail loudly if the file is binary garbage
    raw_bytes = await file.read()
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail="File could not be decoded as UTF-8. Please upload a plain text file.",
        )

    async with db.begin():
        meeting = Meeting(
            project_id=project_id,
            filename=file.filename,
            file_format=ext,
            processed=False,
        )
        db.add(meeting)
        await db.flush()  # get the auto-generated meeting.id before we need it below

        # Enqueue the Celery task — returns immediately with a task handle
        task = process_meeting.delay(meeting.id, file.filename, content)

        # Save the task ID so the status endpoint can query Celery later
        meeting.task_id = task.id

    # db.begin() committed above; meeting is now in the DB
    return meeting


# ─── Status ───────────────────────────────────────────────────────────────────

@router.get("/meetings/{meeting_id}/status", response_model=MeetingStatusOut)
async def get_meeting_status(
    meeting_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Poll this endpoint after upload to know when a meeting is ready.

    Combines two sources of truth:
      - meeting.processed / meeting.error — written by the pipeline task
      - Celery task state                 — reflects the worker's live view

    The frontend polls every 2–3 seconds until processed=True.
    """
    meeting = await db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found.")

    task_status = _get_celery_task_status(meeting.task_id)

    return MeetingStatusOut(
        processed=meeting.processed,
        task_status=task_status,
        error=meeting.error,
    )


# ─── Projects ─────────────────────────────────────────────────────────────────

@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)):
    """
    Return all projects, each annotated with:
      - meeting_count:      how many transcripts have been uploaded
      - action_item_count:  total action items extracted across all meetings

    Uses correlated subqueries so everything comes back in a single SQL round-trip.

    A correlated subquery is a SELECT inside a SELECT that references a column
    from the outer query.  Here, for each project row, PostgreSQL runs the inner
    SELECT to count its meetings / action items.  It's slightly slower than a
    JOIN + GROUP BY for huge tables, but much easier to read.
    """
    # Subquery: count meetings belonging to this project
    meeting_count_sq = (
        select(func.count(Meeting.id))
        .where(Meeting.project_id == Project.id)
        .correlate(Project)
        .scalar_subquery()
    )

    # Subquery: sum of jsonb_array_length(action_items) across all extractions
    # for meetings in this project.
    # jsonb_array_length() returns the number of elements in a JSONB array.
    # We COALESCE the SUM to 0 in case there are no extractions yet.
    action_count_sq = (
        select(
            func.coalesce(
                func.sum(func.jsonb_array_length(Extraction.action_items)), 0
            )
        )
        .join(Meeting, Meeting.id == Extraction.meeting_id)
        .where(Meeting.project_id == Project.id)
        .where(Extraction.action_items.isnot(None))
        .correlate(Project)
        .scalar_subquery()
    )

    stmt = (
        select(
            Project,
            meeting_count_sq.label("meeting_count"),
            action_count_sq.label("action_item_count"),
        )
        .order_by(Project.created_at.desc())
    )

    rows = (await db.execute(stmt)).all()

    return [
        ProjectOut(
            id=project.id,
            name=project.name,
            description=project.description,
            created_at=project.created_at,
            meeting_count=meeting_count or 0,
            action_item_count=action_item_count or 0,
        )
        for project, meeting_count, action_item_count in rows
    ]


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new project container.

    Projects are optional — a meeting can be uploaded without a project
    (project_id=None).  Projects are useful for grouping related meetings
    (e.g. all Q2 planning calls, or all calls with a specific client).
    """
    async with db.begin():
        project = Project(name=body.name, description=body.description)
        db.add(project)
        await db.flush()  # get the auto-generated ID

    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        meeting_count=0,
        action_item_count=0,
    )


# ─── List Meetings ────────────────────────────────────────────────────────────

@router.get("/meetings", response_model=list[MeetingOut])
async def list_meetings(db: AsyncSession = Depends(get_db)):
    """Return all meetings newest first (up to 50), used by the dashboard."""
    result = await db.execute(
        select(Meeting).order_by(Meeting.created_at.desc()).limit(50)
    )
    return result.scalars().all()


# ─── Stats ────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsOut)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """
    Global dashboard numbers — all computed in a single async DB call each.

    These are the "at a glance" numbers shown at the top of the dashboard:
    how many meetings, how many decisions found, how many action items, etc.
    """
    total_meetings = (
        await db.execute(select(func.count(Meeting.id)))
    ).scalar() or 0

    processed_meetings = (
        await db.execute(
            select(func.count(Meeting.id)).where(Meeting.processed == True)  # noqa: E712
        )
    ).scalar() or 0

    total_projects = (
        await db.execute(select(func.count(Project.id)))
    ).scalar() or 0

    total_decisions = (
        await db.execute(
            select(
                func.coalesce(func.sum(func.jsonb_array_length(Extraction.decisions)), 0)
            ).where(Extraction.decisions.isnot(None))
        )
    ).scalar() or 0

    total_action_items = (
        await db.execute(
            select(
                func.coalesce(func.sum(func.jsonb_array_length(Extraction.action_items)), 0)
            ).where(Extraction.action_items.isnot(None))
        )
    ).scalar() or 0

    return StatsOut(
        total_meetings=total_meetings,
        processed_meetings=processed_meetings,
        total_projects=total_projects,
        total_decisions=total_decisions,
        total_action_items=total_action_items,
    )
