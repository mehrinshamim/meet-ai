"""
test_meetings.py — Manual tests for Phase 5 routes.

Run from the project root (with uv):

  # Terminal 1 — start the API server
  uv run uvicorn backend.main:app --reload --port 8000

  # Terminal 2 — start a Celery worker
  uv run celery -A backend.tasks.celery_app worker --loglevel=info

  # Terminal 3 — run these tests
  uv run python tests/manual/test_meetings.py

Prerequisites:
  - Docker containers running (PostgreSQL + Redis)
  - .env file populated
  - alembic upgrade head already applied
  - GROQ_API_KEY set in .env

Tests are numbered.  Run them in order — later tests depend on earlier ones.
The DB does NOT need to be empty — tests capture a baseline at startup and
assert relative to that baseline, so they work on a dirty DB.
"""

import json
import sys
import time

import httpx

BASE = "http://localhost:8000/api"

# Stores IDs created during this run + baseline counts captured at startup
state: dict = {}


def ok(label: str) -> None:
    print(f"  [PASS] {label}")


def fail(label: str, detail: str) -> None:
    print(f"  [FAIL] {label}: {detail}")
    sys.exit(1)


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        ok(label)
    else:
        fail(label, detail)


# ─── Baseline capture ─────────────────────────────────────────────────────────

def capture_baseline():
    """
    Record how many projects and meetings already exist before this test run.
    All "count increased by N" assertions are relative to these baseline values.
    This makes the tests safe to run on a DB that already has data from a
    previous run — no need to truncate tables between runs.
    """
    stats = httpx.get(f"{BASE}/stats").json()
    projects = httpx.get(f"{BASE}/projects").json()
    state["baseline_meetings"] = stats["total_meetings"]
    state["baseline_processed"] = stats["processed_meetings"]
    state["baseline_projects"] = len(projects)
    print(f"  baseline: {state['baseline_meetings']} meetings, "
          f"{state['baseline_projects']} projects, "
          f"{state['baseline_processed']} processed")


# ─── Test 1: GET /api/stats — shape check ─────────────────────────────────────

def test_01_stats_shape():
    print("\n[1] GET /api/stats — response shape")
    r = httpx.get(f"{BASE}/stats")
    check(r.status_code == 200, "status 200")
    data = r.json()
    for key in ["total_meetings", "total_projects", "total_decisions",
                "total_action_items", "processed_meetings"]:
        check(key in data, f"has {key} key")
    print("  stats:", json.dumps(data, indent=4))


# ─── Test 2: GET /api/projects — shape check ──────────────────────────────────

def test_02_list_projects_shape():
    print("\n[2] GET /api/projects — response shape")
    r = httpx.get(f"{BASE}/projects")
    check(r.status_code == 200, "status 200")
    check(isinstance(r.json(), list), "returns a list")
    ok(f"currently {len(r.json())} project(s) in DB (baseline)")


# ─── Test 3: POST /api/projects ───────────────────────────────────────────────

def test_03_create_project():
    print("\n[3] POST /api/projects")
    r = httpx.post(f"{BASE}/projects", json={"name": "Q2 Planning", "description": "All Q2 calls"})
    check(r.status_code == 201, f"status 201 (got {r.status_code})")
    data = r.json()
    check("id" in data, "has id")
    check(data["name"] == "Q2 Planning", "name matches")
    check(data["meeting_count"] == 0, "meeting_count=0 initially")
    check(data["action_item_count"] == 0, "action_item_count=0 initially")
    state["project_id"] = data["id"]
    print(f"  created project_id={state['project_id']}")


# ─── Test 4: POST /api/projects — no description ──────────────────────────────

def test_04_create_project_no_description():
    print("\n[4] POST /api/projects — no description (optional field)")
    r = httpx.post(f"{BASE}/projects", json={"name": "Other Project"})
    check(r.status_code == 201, f"status 201 (got {r.status_code})")
    check(r.json()["description"] is None, "description is null")


# ─── Test 5: POST /api/projects — missing name (validation error) ─────────────

def test_05_create_project_missing_name():
    print("\n[5] POST /api/projects — missing name (expect 422)")
    r = httpx.post(f"{BASE}/projects", json={"description": "No name given"})
    check(r.status_code == 422, f"status 422 (got {r.status_code})")


# ─── Test 6: GET /api/projects — count increased by 2 ────────────────────────

def test_06_list_projects_after_create():
    print("\n[6] GET /api/projects — count increased by 2 from baseline")
    r = httpx.get(f"{BASE}/projects")
    check(r.status_code == 200, "status 200")
    expected = state["baseline_projects"] + 2
    actual = len(r.json())
    check(actual == expected, f"project count = baseline+2 = {expected} (got {actual})")


# ─── Test 7: POST /api/meetings/upload — valid .vtt ───────────────────────────

VTT_CONTENT = b"""WEBVTT

00:00:01.000 --> 00:00:05.000
<v Alice>We need to decide on the project deadline.

00:00:06.000 --> 00:00:10.000
<v Bob>I think end of month is realistic.

00:00:11.000 --> 00:00:15.000
<v Alice>Agreed. Bob, can you update the roadmap by Friday?
"""


def test_07_upload_vtt():
    print("\n[7] POST /api/meetings/upload — valid .vtt file")
    r = httpx.post(
        f"{BASE}/meetings/upload",
        files={"file": ("standup_2024-03-01.vtt", VTT_CONTENT, "text/plain")},
        data={"project_id": state["project_id"]},
    )
    check(r.status_code == 201, f"status 201 (got {r.status_code}: {r.text})")
    data = r.json()
    check("id" in data, "has meeting id")
    check(data["processed"] is False, "processed=False immediately after upload")
    check(data["task_id"] is not None, "task_id is set (Celery task enqueued)")
    check(data["filename"] == "standup_2024-03-01.vtt", "filename stored")
    check(data["file_format"] == "vtt", "file_format=vtt")
    check(data["project_id"] == state["project_id"], "project_id matches")
    state["meeting_id"] = data["id"]
    state["task_id"] = data["task_id"]
    print(f"  meeting_id={state['meeting_id']}  task_id={state['task_id']}")


# ─── Test 8: POST /api/meetings/upload — valid .txt ───────────────────────────

TXT_CONTENT = b"""[00:00:01] Alice: We should review the budget.
[00:00:05] Bob: I'll prepare the report by Wednesday.
[00:00:10] Alice: Great. Decision: budget review scheduled for next week.
"""


def test_08_upload_txt():
    print("\n[8] POST /api/meetings/upload — valid .txt file (no project)")
    r = httpx.post(
        f"{BASE}/meetings/upload",
        files={"file": ("budget_review.txt", TXT_CONTENT, "text/plain")},
    )
    check(r.status_code == 201, f"status 201 (got {r.status_code}: {r.text})")
    data = r.json()
    check(data["file_format"] == "txt", "file_format=txt")
    check(data["project_id"] is None, "no project_id (optional)")


# ─── Test 9: POST /api/meetings/upload — unsupported format ───────────────────

def test_09_upload_invalid_format():
    print("\n[9] POST /api/meetings/upload — .pdf file (expect 422)")
    r = httpx.post(
        f"{BASE}/meetings/upload",
        files={"file": ("notes.pdf", b"%PDF-1.4", "application/pdf")},
    )
    check(r.status_code == 422, f"status 422 (got {r.status_code})")
    check("detail" in r.json(), "error detail present")
    print(f"  error: {r.json()['detail']}")


# ─── Test 10: GET /api/meetings/{id}/status — immediately after upload ────────

def test_10_status_immediately():
    print("\n[10] GET /api/meetings/{id}/status — right after upload")
    r = httpx.get(f"{BASE}/meetings/{state['meeting_id']}/status")
    check(r.status_code == 200, "status 200")
    data = r.json()
    check("processed" in data, "has processed")
    check("task_status" in data, "has task_status")
    check("error" in data, "has error")
    check(data["processed"] is False, "processed=False (not done yet)")
    print(f"  task_status={data['task_status']}  error={data['error']}")


# ─── Test 11: GET /api/meetings/{id}/status — 404 for unknown ─────────────────

def test_11_status_not_found():
    print("\n[11] GET /api/meetings/999999/status — expect 404")
    r = httpx.get(f"{BASE}/meetings/999999/status")
    check(r.status_code == 404, f"status 404 (got {r.status_code})")


# ─── Test 12: Poll until processed ────────────────────────────────────────────

def test_12_poll_until_processed(timeout_seconds=120):
    print(f"\n[12] Polling status until processed=True (timeout={timeout_seconds}s)")
    print("  (This tests the real Celery pipeline — embedding + Groq calls.)")
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > timeout_seconds:
            fail("processed within timeout", f"still not processed after {timeout_seconds}s")
            return

        r = httpx.get(f"{BASE}/meetings/{state['meeting_id']}/status")
        data = r.json()
        print(f"  t={elapsed:.0f}s  status={data['task_status']}  processed={data['processed']}")

        if data["error"]:
            fail("no pipeline error", data["error"])
            return

        if data["processed"]:
            ok("processed=True within timeout")
            break

        time.sleep(5)


# ─── Test 13: GET /api/projects — meeting_count updated ───────────────────────

def test_13_project_meeting_count():
    print("\n[13] GET /api/projects — meeting_count=1 for the project we created")
    r = httpx.get(f"{BASE}/projects")
    projects = {p["id"]: p for p in r.json()}
    p = projects.get(state["project_id"])
    if p is None:
        fail("project found in list", "project_id not in response")
        return
    check(p["meeting_count"] == 1, f"meeting_count=1 (got {p['meeting_count']})")
    print(f"  action_item_count={p['action_item_count']}")


# ─── Test 14: GET /api/stats — totals increased from baseline ─────────────────

def test_14_stats_after_processing():
    print("\n[14] GET /api/stats — totals increased from baseline")
    r = httpx.get(f"{BASE}/stats")
    data = r.json()
    # We uploaded 2 meetings in this run, so total should be baseline + 2
    expected_meetings = state["baseline_meetings"] + 2
    check(
        data["total_meetings"] >= expected_meetings,
        f"total_meetings >= baseline+2 = {expected_meetings} (got {data['total_meetings']})",
    )
    # At least the .vtt we uploaded should be processed by now
    expected_processed = state["baseline_processed"] + 1
    check(
        data["processed_meetings"] >= expected_processed,
        f"processed_meetings >= baseline+1 = {expected_processed} (got {data['processed_meetings']})",
    )
    print("  stats:", json.dumps(data, indent=4))


# ─── Run all tests ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 5 — Manual Route Tests")
    print("=" * 60)
    print("Make sure uvicorn and celery worker are both running.\n")

    print("[baseline] Capturing current DB state...")
    capture_baseline()

    test_01_stats_shape()
    test_02_list_projects_shape()
    test_03_create_project()
    test_04_create_project_no_description()
    test_05_create_project_missing_name()
    test_06_list_projects_after_create()
    test_07_upload_vtt()
    test_08_upload_txt()
    test_09_upload_invalid_format()
    test_10_status_immediately()
    test_11_status_not_found()
    test_12_poll_until_processed()
    test_13_project_meeting_count()
    test_14_stats_after_processing()

    print("\n" + "=" * 60)
    print("All tests passed.")
    print("=" * 60)
