"""
test_extractions.py — Manual tests for extractions and sentiment routes.

Run from the project root:

  uv run python tests/manual/test_extractions.py <meeting_id>

Pass a processed meeting_id as a command-line argument.
You can get one from a previous test run or by uploading a file via the API.

Prerequisites:
  - uvicorn running: uv run uvicorn backend.main:app --reload --port 8000
  - At least one meeting that has been fully processed (processed=True)
"""

import csv
import io
import json
import sys

import httpx

BASE = "http://localhost:8000/api"


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


# ─── Extractions ──────────────────────────────────────────────────────────────

def test_01_get_extractions(meeting_id: int):
    print(f"\n[1] GET /api/meetings/{meeting_id}/extractions")
    r = httpx.get(f"{BASE}/meetings/{meeting_id}/extractions")
    check(r.status_code == 200, f"status 200 (got {r.status_code}: {r.text})")
    data = r.json()
    check("decisions" in data, "has decisions key")
    check("action_items" in data, "has action_items key")
    check(isinstance(data["decisions"], list), "decisions is a list")
    check(isinstance(data["action_items"], list), "action_items is a list")
    print("  decisions:", json.dumps(data["decisions"], indent=4))
    print("  action_items:", json.dumps(data["action_items"], indent=4))


def test_02_extractions_not_found():
    print("\n[2] GET /api/meetings/999999/extractions — expect 404")
    r = httpx.get(f"{BASE}/meetings/999999/extractions")
    check(r.status_code == 404, f"status 404 (got {r.status_code})")


def test_03_export_csv(meeting_id: int):
    print(f"\n[3] GET /api/meetings/{meeting_id}/extractions/export")
    r = httpx.get(f"{BASE}/meetings/{meeting_id}/extractions/export")
    check(r.status_code == 200, f"status 200 (got {r.status_code})")
    check("text/csv" in r.headers.get("content-type", ""), "content-type is text/csv")
    check("attachment" in r.headers.get("content-disposition", ""), "content-disposition is attachment")

    # Parse the CSV and validate structure
    reader = csv.DictReader(io.StringIO(r.text))
    rows = list(reader)
    expected_cols = {"type", "text", "speaker", "assignee", "due_date", "timestamp"}
    check(expected_cols == set(reader.fieldnames or []), f"CSV has correct columns (got {reader.fieldnames})")

    for row in rows:
        check(row["type"] in ("decision", "action_item"), f"type is valid: {row['type']}")

    print(f"  CSV rows: {len(rows)}")
    print(f"  Content-Disposition: {r.headers.get('content-disposition')}")
    print("  CSV preview:")
    print("   ", ",".join(reader.fieldnames or []))
    for row in rows[:3]:
        print("   ", ",".join(row.values()))


def test_04_export_csv_not_found():
    print("\n[4] GET /api/meetings/999999/extractions/export — expect 404")
    r = httpx.get(f"{BASE}/meetings/999999/extractions/export")
    check(r.status_code == 404, f"status 404 (got {r.status_code})")


# ─── Sentiment ────────────────────────────────────────────────────────────────

def test_05_get_sentiment(meeting_id: int):
    print(f"\n[5] GET /api/meetings/{meeting_id}/sentiment")
    r = httpx.get(f"{BASE}/meetings/{meeting_id}/sentiment")
    check(r.status_code == 200, f"status 200 (got {r.status_code}: {r.text})")
    data = r.json()
    check("speaker_scores" in data, "has speaker_scores key")
    check("segment_scores" in data, "has segment_scores key")
    check(isinstance(data["speaker_scores"], dict), "speaker_scores is a dict")
    check(isinstance(data["segment_scores"], list), "segment_scores is a list")

    # Validate speaker scores are numeric
    for speaker, score in data["speaker_scores"].items():
        check(isinstance(score, (int, float)), f"speaker score is numeric: {speaker}={score}")
        check(-1.0 <= score <= 1.0, f"score in range [-1, 1]: {speaker}={score}")

    # Validate segment scores have required fields
    for seg in data["segment_scores"]:
        for field in ["chunk_id", "speaker", "start_time", "score", "label"]:
            check(field in seg, f"segment has '{field}' field")
        check(seg["label"] in ("positive", "neutral", "negative"),
              f"label is valid: {seg['label']}")

    print("  speaker_scores:", json.dumps(data["speaker_scores"], indent=4))
    print(f"  segment_scores: {len(data['segment_scores'])} segments")
    for seg in data["segment_scores"][:3]:
        print(f"    [{seg['start_time']}] {seg['speaker']}: {seg['label']} ({seg['score']})")


def test_06_sentiment_not_found():
    print("\n[6] GET /api/meetings/999999/sentiment — expect 404")
    r = httpx.get(f"{BASE}/meetings/999999/sentiment")
    check(r.status_code == 404, f"status 404 (got {r.status_code})")


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python tests/manual/test_extractions.py <meeting_id>")
        print("Pass the ID of a fully processed meeting.")
        sys.exit(1)

    meeting_id = int(sys.argv[1])
    print("=" * 60)
    print("Phase 8 — Extractions & Sentiment Route Tests")
    print("=" * 60)
    print(f"Using meeting_id={meeting_id}\n")

    test_01_get_extractions(meeting_id)
    test_02_extractions_not_found()
    test_03_export_csv(meeting_id)
    test_04_export_csv_not_found()
    test_05_get_sentiment(meeting_id)
    test_06_sentiment_not_found()

    print("\n" + "=" * 60)
    print("All tests passed.")
    print("=" * 60)
