"""
test_chat.py — Manual tests for Phase 7: Chat Route

Tests are numbered and grouped:
  1-4  : ai.answer_question() — pure Python, no DB/Groq
  5-6  : ai.parse_citations() — pure Python
  7-9  : POST /api/chat — DB required, Groq required
  10-12: GET /api/chat/history — DB required
  13   : Multi-turn conversation — full end-to-end

How to run (from repo root):
  uv run python tests/manual/test_chat.py

Prerequisites:
  - PostgreSQL + pgvector running (docker compose up -d)
  - Redis running (docker compose up -d)
  - .env with GROQ_API_KEY set
  - At least one processed meeting in the DB (run pipeline first)
  - FastAPI server running: uv run uvicorn backend.main:app --reload

For DB tests, update MEETING_ID below to a real processed meeting.
"""

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dotenv import load_dotenv
load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
# Update this to a real processed meeting_id before running DB tests.
MEETING_ID = 1

BASE_URL = "http://localhost:8000"


# ─── Pure Python tests (no DB, no Groq) ───────────────────────────────────────

def test_1_answer_question_no_context():
    """
    Test 1: answer_question() with empty context returns a "no info" message.
    No Groq call is made — early return guards against empty context.
    """
    from backend.services.ai import answer_question

    result = answer_question(
        question="What was decided about the budget?",
        context_blocks=[],
    )
    assert "could not find" in result.lower() or "no" in result.lower(), (
        f"Expected 'no info' message, got: {result}"
    )
    print("Test 1 PASSED — empty context returns no-info message")


def test_2_parse_citations_empty():
    """
    Test 2: parse_citations() on a string with no citation markup returns [].
    """
    from backend.services.ai import parse_citations

    result = parse_citations("The meeting decided to launch next Friday.")
    assert result == [], f"Expected [], got: {result}"
    print("Test 2 PASSED — no citations → empty list")


def test_3_parse_citations_single():
    """
    Test 3: parse_citations() extracts one citation correctly.
    """
    from backend.services.ai import parse_citations

    text = (
        "Alice confirmed the launch date "
        "[[meeting: standup_2024.vtt, time: 00:05:30, speaker: Alice]]."
    )
    result = parse_citations(text)
    assert len(result) == 1, f"Expected 1 citation, got: {result}"
    assert result[0]["meeting"] == "standup_2024.vtt"
    assert result[0]["timestamp"] == "00:05:30"
    assert result[0]["speaker"] == "Alice"
    print("Test 3 PASSED — single citation extracted correctly")


def test_4_parse_citations_deduplication():
    """
    Test 4: parse_citations() deduplicates identical citations.
    """
    from backend.services.ai import parse_citations

    text = (
        "Bob said X [[meeting: m.vtt, time: 00:01:00, speaker: Bob]] "
        "and repeated it [[meeting: m.vtt, time: 00:01:00, speaker: Bob]]."
    )
    result = parse_citations(text)
    assert len(result) == 1, f"Expected 1 after dedup, got: {result}"
    print("Test 4 PASSED — duplicate citations deduplicated")


def test_5_parse_citations_multiple():
    """
    Test 5: parse_citations() extracts multiple distinct citations.
    """
    from backend.services.ai import parse_citations

    text = (
        "Alice said the deadline is Friday "
        "[[meeting: planning.vtt, time: 00:02:10, speaker: Alice]]. "
        "Bob agreed [[meeting: planning.vtt, time: 00:03:45, speaker: Bob]]."
    )
    result = parse_citations(text)
    assert len(result) == 2, f"Expected 2 citations, got: {result}"
    speakers = {c["speaker"] for c in result}
    assert speakers == {"Alice", "Bob"}
    print("Test 5 PASSED — multiple distinct citations extracted")


def test_6_answer_question_with_context():
    """
    Test 6: answer_question() with real context makes a Groq call.
    Requires GROQ_API_KEY in .env.

    We use a synthetic context block (not from DB) to test the Groq call
    without needing a processed meeting.
    """
    from backend.services.ai import answer_question

    context = (
        "[Meeting: test_meeting.vtt | Time: 00:05:00 | Speaker: Alice]\n"
        "Alice: We've decided to delay the launch by two weeks to fix the login bug."
    )
    result = answer_question(
        question="What was decided about the launch?",
        context_blocks=[context],
        meeting_scope="test_meeting.vtt",
    )
    assert isinstance(result, str) and len(result) > 10, f"Expected non-empty string, got: {result!r}"
    print(f"Test 6 PASSED — Groq answered: {result[:120]}...")


# ─── DB tests ─────────────────────────────────────────────────────────────────

async def test_7_post_chat_no_session():
    """
    Test 7: POST /api/chat without session_id → server generates one.
    Requires a processed meeting with MEETING_ID.
    """
    import urllib.request

    payload = json.dumps({
        "question": "What were the main topics discussed?",
        "meeting_id": MEETING_ID,
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    assert "session_id" in data and data["session_id"], "Expected auto-generated session_id"
    assert "answer" in data and data["answer"], "Expected non-empty answer"
    assert data["question"] == "What were the main topics discussed?"
    print(f"Test 7 PASSED — session_id={data['session_id'][:8]}... answer={data['answer'][:80]}...")
    return data["session_id"]


async def test_8_post_chat_cross_meeting():
    """
    Test 8: POST /api/chat with meeting_id=None → cross-meeting query.
    """
    import urllib.request

    payload = json.dumps({
        "question": "Summarise all key decisions made across meetings.",
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    assert data["meeting_id"] is None, "Expected meeting_id=None for cross-meeting query"
    assert data["answer"], "Expected non-empty answer"
    print(f"Test 8 PASSED — cross-meeting answer={data['answer'][:80]}...")


async def test_9_post_chat_invalid_meeting():
    """
    Test 9: POST /api/chat with non-existent meeting_id → 404.
    """
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "question": "Who was the speaker?",
        "meeting_id": 999999,
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        print("Test 9 FAILED — expected 404, got 200")
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"Expected 404, got {e.code}"
        print("Test 9 PASSED — 404 for non-existent meeting")


async def test_10_get_chat_history(session_id: str):
    """
    Test 10: GET /api/chat/history returns messages for the session.
    """
    import urllib.request

    url = f"{BASE_URL}/api/chat/history?session_id={session_id}"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())

    assert data["session_id"] == session_id
    assert isinstance(data["messages"], list) and len(data["messages"]) >= 1
    print(f"Test 10 PASSED — history has {len(data['messages'])} message(s)")


async def test_11_get_history_missing_session():
    """
    Test 11: GET /api/chat/history with unknown session_id → 404.
    """
    import urllib.request
    import urllib.error

    fake_id = str(uuid.uuid4())
    url = f"{BASE_URL}/api/chat/history?session_id={fake_id}"
    try:
        urllib.request.urlopen(url)
        print("Test 11 FAILED — expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"Expected 404, got {e.code}"
        print("Test 11 PASSED — 404 for unknown session_id")


async def test_12_multi_turn_conversation():
    """
    Test 12: Two-turn conversation — follow-up question uses prior history.

    Turn 1: Ask about decisions.
    Turn 2: Ask a follow-up with a pronoun ("Who made it?") — should be reformulated
            by the model into a standalone question using Turn 1 context.
    """
    import urllib.request

    session_id = str(uuid.uuid4())

    # Turn 1
    payload1 = json.dumps({
        "question": "What was the most important decision made?",
        "meeting_id": MEETING_ID,
        "session_id": session_id,
    }).encode()
    req1 = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload1,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req1) as resp:
        turn1 = json.loads(resp.read())
    print(f"  Turn 1 answer: {turn1['answer'][:100]}...")

    # Turn 2 — pronoun follow-up
    payload2 = json.dumps({
        "question": "Who proposed it?",
        "meeting_id": MEETING_ID,
        "session_id": session_id,
    }).encode()
    req2 = urllib.request.Request(
        f"{BASE_URL}/api/chat",
        data=payload2,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req2) as resp:
        turn2 = json.loads(resp.read())
    print(f"  Turn 2 answer: {turn2['answer'][:100]}...")

    # Verify history has 2 messages
    url = f"{BASE_URL}/api/chat/history?session_id={session_id}"
    with urllib.request.urlopen(url) as resp:
        history = json.loads(resp.read())

    assert len(history["messages"]) == 2, f"Expected 2 messages, got {len(history['messages'])}"
    print("Test 12 PASSED — multi-turn conversation completed, history has 2 messages")


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_pure_python_tests():
    print("\n=== Pure Python Tests (no DB/Groq for 1-5) ===")
    test_1_answer_question_no_context()
    test_2_parse_citations_empty()
    test_3_parse_citations_single()
    test_4_parse_citations_deduplication()
    test_5_parse_citations_multiple()


def run_groq_test():
    print("\n=== Groq Test (requires GROQ_API_KEY) ===")
    test_6_answer_question_with_context()


async def run_db_tests():
    print(f"\n=== DB + API Tests (MEETING_ID={MEETING_ID}, server at {BASE_URL}) ===")
    session_id = await test_7_post_chat_no_session()
    await test_8_post_chat_cross_meeting()
    await test_9_post_chat_invalid_meeting()
    await test_10_get_chat_history(session_id)
    await test_11_get_history_missing_session()
    await test_12_multi_turn_conversation()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "pure"

    if mode == "pure":
        run_pure_python_tests()
        print("\nRun with 'groq' to test Groq calls.")
        print("Run with 'db' to test DB + API (requires running server + processed meeting).")

    elif mode == "groq":
        run_pure_python_tests()
        run_groq_test()

    elif mode == "db":
        run_pure_python_tests()
        run_groq_test()
        asyncio.run(run_db_tests())

    else:
        print(f"Unknown mode: {mode}. Use 'pure', 'groq', or 'db'.")
        sys.exit(1)
