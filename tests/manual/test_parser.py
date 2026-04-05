"""
tests/manual/test_parser.py — Manual tests for Phase 2: File Parser + Chunker

Run with:
    uv run python tests/manual/test_parser.py

Each test prints PASS or FAIL with a short reason.
No external dependencies — just imports from backend/services/parser.py.

Tests:
  1.  VTT speaker extraction (<v Speaker> format)
  2.  VTT speaker extraction (Speaker: text format)
  3.  VTT timestamp normalisation
  4.  VTT meeting date from filename
  5.  TXT speaker + timestamp extraction
  6.  TXT with no timestamps (speaker-only format)
  7.  TXT continuation lines merged into previous turn
  8.  Short same-speaker turns merged into one chunk
  9.  Long turn split at sentence boundary
  10. Parent chunks group child chunks into 5-min windows
  11. Child chunks carry correct parent_index
  12. Metadata: speaker list order of first appearance
  13. Metadata: word count
  14. No speakers → chunks still produced (graceful degradation)
  15. Empty content → empty result (no crash)
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.services.parser import (
    parse_transcript,
    parse_vtt,
    parse_txt,
    chunk_turns,
    build_parent_chunks,
    extract_metadata,
    Turn,
    _tokens,
    _normalize_ts,
    TARGET_TOKENS,
    MAX_TOKENS,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_results: list[tuple[str, str, str]] = []   # (name, status, detail)


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    _results.append((name, status, detail))
    tag = "  PASS" if condition else "  FAIL"
    line = f"{tag}  {name}"
    if not condition and detail:
        line += f"\n         → {detail}"
    print(line)


# ─── Sample Content ───────────────────────────────────────────────────────────

VTT_V_TAG = """\
WEBVTT

00:00:01.000 --> 00:00:08.000
<v Alice>Good morning. Let us review the roadmap.</v>

00:00:09.000 --> 00:00:14.000
<v Bob>I agree. Dashboard redesign should be top priority.</v>

00:00:15.000 --> 00:00:19.000
<v Alice>Agreed. Bob, please brief the frontend team by Friday.</v>

00:00:20.000 --> 00:00:23.000
<v Bob>Sure, will do.</v>
"""

VTT_COLON = """\
WEBVTT

00:00:01.000 --> 00:00:06.000
Alice: Let us begin the meeting.

00:00:07.000 --> 00:00:12.000
Bob: Sounds good. What is the agenda today?
"""

TXT_WITH_TIMESTAMPS = """\
[00:00:01] Alice: Good morning everyone. Let us start with the roadmap.
[00:00:09] Bob: I agree. The dashboard redesign is top priority.
[00:00:15] Alice: Bob, can you brief the frontend team by Friday?
[00:00:21] Bob: Sure, I will do that.
"""

TXT_NO_TIMESTAMPS = """\
Alice: Good morning everyone.
Bob: Good morning! Let us start.
Alice: First item on the agenda is the Q3 roadmap.
"""

TXT_CONTINUATION = """\
[00:00:01] Alice: This is the first line of Alice's speech.
This is a continuation of her speech that has no speaker prefix.
[00:00:20] Bob: And this is Bob speaking now.
"""


# ─── Test 1: VTT speaker extraction — <v> tag format ─────────────────────────

turns = parse_vtt(VTT_V_TAG)
check(
    "1. VTT <v> tag: speakers extracted",
    len([t for t in turns if t.speaker is not None]) == len(turns),
    f"speakers found: {[t.speaker for t in turns]}",
)
check(
    "2. VTT <v> tag: correct speaker names",
    [t.speaker for t in turns] == ["Alice", "Bob", "Alice", "Bob"],
    f"got: {[t.speaker for t in turns]}",
)


# ─── Test 2: VTT speaker extraction — "Speaker: text" colon format ───────────

turns2 = parse_vtt(VTT_COLON)
check(
    "3. VTT colon format: speakers extracted",
    [t.speaker for t in turns2] == ["Alice", "Bob"],
    f"got: {[t.speaker for t in turns2]}",
)


# ─── Test 3: VTT timestamp normalisation ─────────────────────────────────────

check(
    "4. VTT timestamps: normalised to HH:MM:SS",
    all(t.start_time and len(t.start_time) == 8 for t in turns),
    f"start times: {[t.start_time for t in turns]}",
)
check(
    "5. VTT timestamps: milliseconds stripped",
    all(t.start_time and "." not in t.start_time for t in turns),
    f"start times: {[t.start_time for t in turns]}",
)


# ─── Test 4: Meeting date from filename ──────────────────────────────────────

r = parse_transcript(VTT_V_TAG, "team_sync_2024-07-22.vtt")
check(
    "6. Date from filename: parsed correctly",
    r.meeting_date is not None and r.meeting_date.strftime("%Y-%m-%d") == "2024-07-22",
    f"got: {r.meeting_date}",
)

r_no_date = parse_transcript(VTT_V_TAG, "meeting.vtt")
check(
    "7. No date in filename: meeting_date is None",
    r_no_date.meeting_date is None,
    f"got: {r_no_date.meeting_date}",
)


# ─── Test 5: TXT with timestamps ─────────────────────────────────────────────

turns_txt = parse_txt(TXT_WITH_TIMESTAMPS)
check(
    "8. TXT: speaker names extracted",
    [t.speaker for t in turns_txt] == ["Alice", "Bob", "Alice", "Bob"],
    f"got: {[t.speaker for t in turns_txt]}",
)
check(
    "9. TXT: timestamps extracted",
    turns_txt[0].start_time == "00:00:01" and turns_txt[1].start_time == "00:00:09",
    f"got: {turns_txt[0].start_time}, {turns_txt[1].start_time}",
)
check(
    "10. TXT: end_time filled from next turn's start_time",
    turns_txt[0].end_time == "00:00:09",
    f"got end_time[0]: {turns_txt[0].end_time}",
)


# ─── Test 6: TXT without timestamps ──────────────────────────────────────────

turns_no_ts = parse_txt(TXT_NO_TIMESTAMPS)
check(
    "11. TXT no timestamps: still extracts speakers",
    [t.speaker for t in turns_no_ts] == ["Alice", "Bob", "Alice"],
    f"got: {[t.speaker for t in turns_no_ts]}",
)
check(
    "12. TXT no timestamps: start_time is None",
    all(t.start_time is None for t in turns_no_ts),
    f"got: {[t.start_time for t in turns_no_ts]}",
)


# ─── Test 7: Short same-speaker turns merged ─────────────────────────────────

short_turns = [
    Turn(speaker="Alice", start_time="00:00:01", end_time="00:00:03", text="Hello."),
    Turn(speaker="Alice", start_time="00:00:03", end_time="00:00:06", text="How are you?"),
    Turn(speaker="Bob",   start_time="00:00:06", end_time="00:00:09", text="I am fine thanks."),
]
chunks = chunk_turns(short_turns)
check(
    "13. Chunker: short same-speaker turns merged",
    len(chunks) == 2 and chunks[0].speaker == "Alice",
    f"got {len(chunks)} chunks: {[(c.speaker, c.text[:30]) for c in chunks]}",
)
check(
    "14. Chunker: merged chunk text contains both sentences",
    "Hello" in chunks[0].text and "How are you" in chunks[0].text,
    f"got text: {chunks[0].text!r}",
)


# ─── Test 8: Long turn split at sentence boundary ────────────────────────────

# Build a turn that is clearly over MAX_TOKENS (600 tokens ≈ 460 words)
long_text = ". ".join([f"This is sentence number {i} and it contains some extra words for padding" for i in range(60)]) + "."
long_turn = [Turn(speaker="Alice", start_time="00:01:00", end_time="00:06:00", text=long_text)]
long_chunks = chunk_turns(long_turn)
check(
    "15. Chunker: long turn produces multiple chunks",
    len(long_chunks) > 1,
    f"got {len(long_chunks)} chunks",
)
check(
    "16. Chunker: split chunks stay within MAX_TOKENS",
    all(c.token_count <= MAX_TOKENS for c in long_chunks),
    f"token counts: {[c.token_count for c in long_chunks]}",
)
check(
    "17. Chunker: all split chunks inherit speaker",
    all(c.speaker == "Alice" for c in long_chunks),
    f"speakers: {[c.speaker for c in long_chunks]}",
)


# ─── Test 9: Parent chunk windows ────────────────────────────────────────────

# Build child chunks spanning 12 minutes — should produce at least 2 parent windows
multi_turn_vtt = """\
WEBVTT

00:00:01.000 --> 00:02:00.000
<v Alice>This is at the start of the meeting. We are discussing the roadmap.</v>

00:05:00.000 --> 00:07:00.000
<v Bob>This is five minutes in. Let us talk about the dashboard.</v>

00:10:00.000 --> 00:12:00.000
<v Alice>Ten minutes in. Let us wrap up the action items.</v>
"""

r_multi = parse_transcript(multi_turn_vtt, "long_meeting.vtt")
check(
    "18. Parent chunks: 12-min meeting produces multiple parents",
    len(r_multi.parent_chunks) >= 2,
    f"got {len(r_multi.parent_chunks)} parent chunks",
)
check(
    "19. Parent chunks: all children have a parent_index",
    all(c.parent_index is not None for c in r_multi.child_chunks),
    f"parent_indexes: {[c.parent_index for c in r_multi.child_chunks]}",
)


# ─── Test 10: Child → parent index mapping ────────────────────────────────────

child_chunks, parents = build_parent_chunks(chunk_turns(parse_vtt(multi_turn_vtt)))
check(
    "20. Parent index: child_chunks reference valid parent indices",
    all(0 <= c.parent_index < len(parents) for c in child_chunks if c.parent_index is not None),
    f"parent_indexes: {[c.parent_index for c in child_chunks]}, parents: {len(parents)}",
)
check(
    "21. Parent chunks: is_parent flag is True",
    all(p.is_parent for p in parents),
    f"is_parent flags: {[p.is_parent for p in parents]}",
)
check(
    "22. Parent chunks: text contains speaker labels",
    all(":" in p.text for p in parents),
    f"parent texts: {[p.text[:60] for p in parents]}",
)


# ─── Test 11: Metadata ────────────────────────────────────────────────────────

r_meta = parse_transcript(VTT_V_TAG, "meeting_2025-01-10.vtt")
check(
    "23. Metadata: speaker order = order of first appearance",
    r_meta.speaker_names == ["Alice", "Bob"],
    f"got: {r_meta.speaker_names}",
)
check(
    "24. Metadata: word count > 0",
    r_meta.word_count > 0,
    f"got: {r_meta.word_count}",
)


# ─── Test 12: No speakers (plain narration) ───────────────────────────────────

no_speaker_vtt = """\
WEBVTT

00:00:01.000 --> 00:00:10.000
Welcome to the meeting. Today we will discuss Q3 targets.

00:00:11.000 --> 00:00:20.000
First topic is the dashboard redesign.
"""

r_no_spk = parse_transcript(no_speaker_vtt, "narration.vtt")
check(
    "25. No speakers: still produces child chunks",
    len(r_no_spk.child_chunks) > 0,
    f"got {len(r_no_spk.child_chunks)} chunks",
)
check(
    "26. No speakers: speaker_names is empty list",
    r_no_spk.speaker_names == [],
    f"got: {r_no_spk.speaker_names}",
)


# ─── Test 13: Empty content ───────────────────────────────────────────────────

r_empty = parse_transcript("WEBVTT\n\n", "empty.vtt")
check(
    "27. Empty VTT: no crash, zero chunks",
    len(r_empty.child_chunks) == 0 and len(r_empty.parent_chunks) == 0,
    f"got child={len(r_empty.child_chunks)} parent={len(r_empty.parent_chunks)}",
)

r_empty_txt = parse_transcript("", "empty.txt")
check(
    "28. Empty TXT: no crash, zero chunks",
    len(r_empty_txt.child_chunks) == 0,
    f"got child={len(r_empty_txt.child_chunks)}",
)


# ─── Summary ──────────────────────────────────────────────────────────────────

print()
total  = len(_results)
passed = sum(1 for _, s, _ in _results if "PASS" in s)
failed = total - passed

print(f"{'─' * 50}")
print(f"  {passed}/{total} passed", end="")
if failed:
    print(f"  |  {failed} FAILED ←")
else:
    print("  — all good")
print(f"{'─' * 50}")

if failed:
    sys.exit(1)
