"""
parser.py — Transcript file parser and chunker.

Responsibilities:
  1. parse_vtt()  — WebVTT → list[Turn]
  2. parse_txt()  — plain-text transcript → list[Turn]
  3. chunk_turns() — list[Turn] → list[ChunkData] (child chunks, ~400 tokens each)
  4. build_parent_chunks() — group child chunks into 5-minute parent windows
  5. extract_metadata() — speakers, word count, date from filename
  6. parse_transcript() — single entry point that calls all of the above

Why parent-child chunks?
  Vector search finds the most relevant *child* chunk (small, precise embedding).
  We then fetch its *parent* (5-min window, full context) to send to the LLM.
  This gives retrieval precision + LLM context richness at the same time.
"""

from __future__ import annotations

import re
import tempfile
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import webvtt


# ─── Constants ────────────────────────────────────────────────────────────────

TARGET_TOKENS = 400       # ideal child chunk size
MAX_TOKENS = 600          # split anything larger than this
MIN_TOKENS = 50           # merge anything smaller than this (if same speaker)
PARENT_WINDOW_SECONDS = 300  # 5-minute parent windows

# Matches speaker turns in plain-text transcripts.
# Supports:
#   "Alice: Hello"
#   "[00:02:10] Alice: Hello"
#   "00:02:10 Alice: Hello"
#   "ALICE (Manager): Hello"
_SPEAKER_RE = re.compile(
    r'(?:^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+)?'   # optional timestamp
    r'([A-Za-z][A-Za-z0-9 _\-(]{0,40}\S):\s+'       # speaker name + colon
    r'(.*)',                                           # spoken text
    re.MULTILINE,
)

_DATE_IN_FILENAME_RE = re.compile(r'(\d{4}[-_]\d{2}[-_]\d{2})')


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class Turn:
    """A single speaker turn as parsed directly from the file."""
    speaker: Optional[str]
    start_time: Optional[str]   # "HH:MM:SS" or None
    end_time: Optional[str]     # "HH:MM:SS" or None
    text: str


@dataclass
class ChunkData:
    """
    One chunk ready to be written to the `chunks` table.

    is_parent=False  → child chunk, gets an embedding, used for retrieval
    is_parent=True   → parent chunk, stores full 5-min context for the LLM

    parent_index: index into the list of parent chunks returned by
    build_parent_chunks().  Resolved to a real DB id by the pipeline task.
    """
    speaker: Optional[str]
    start_time: Optional[str]
    end_time: Optional[str]
    text: str
    token_count: int
    is_parent: bool = False
    parent_index: Optional[int] = None   # set by build_parent_chunks()


@dataclass
class ParseResult:
    """Everything the pipeline task needs after parsing a file."""
    child_chunks: list[ChunkData]
    parent_chunks: list[ChunkData]
    speaker_names: list[str]
    word_count: int
    meeting_date: Optional[datetime]


# ─── Time Utilities ───────────────────────────────────────────────────────────

def _normalize_ts(ts: str) -> str:
    """
    Normalise a VTT/text timestamp to "HH:MM:SS".
    Drops milliseconds, pads short forms.
      "00:05:10.500" → "00:05:10"
      "05:10"        → "00:05:10"
    """
    ts = ts.split(".")[0]
    parts = ts.split(":")
    if len(parts) == 2:
        return f"00:{parts[0].zfill(2)}:{parts[1].zfill(2)}"
    return f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2].zfill(2)}"


def _to_secs(ts: str) -> int:
    """'HH:MM:SS' → integer seconds."""
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _to_ts(secs: int) -> str:
    """Integer seconds → 'HH:MM:SS'."""
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"


# ─── Token Estimation ─────────────────────────────────────────────────────────

def _tokens(text: str) -> int:
    """
    Rough token estimate: words × 1.3.
    English prose averages ~0.75 words per token (GPT tokeniser research).
    We use this to avoid a tokeniser dependency — exact counts aren't critical
    for chunking decisions.
    """
    return max(1, int(len(text.split()) * 1.3))


# ─── VTT Parser ───────────────────────────────────────────────────────────────

def _speaker_from_cue(raw_text: str, clean_text: str) -> tuple[Optional[str], str]:
    """
    Extract speaker and clean text from a VTT cue.

    webvtt-py strips <v> tags from caption.text but keeps them in caption.raw_text.
    We read the speaker from raw_text, use clean_text as the actual content.

    Two formats handled:
      raw: <v Alice>text</v>   →  ("Alice", clean_text)
      raw: Alice: text          →  ("Alice", rest of clean_text)
    """
    # W3C voice span: <v Speaker Name>...</v>
    m = re.match(r"<v\s+([^>]+)>", raw_text.strip())
    if m:
        return m.group(1).strip(), clean_text.strip()

    # "Speaker: text" as first line of cue
    m = re.match(r"^([A-Za-z][A-Za-z0-9 _\-(]{0,40}\S):\s+(.*)", clean_text.strip(), re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return None, clean_text.strip()


def parse_vtt(content: str) -> list[Turn]:
    """
    Parse WebVTT content into speaker turns using webvtt-py.

    webvtt-py 0.5.x doesn't support StringIO buffers reliably, so we write
    to a NamedTemporaryFile and read it back.  The temp file is deleted after.
    """
    turns: list[Turn] = []

    # Write to temp file because webvtt.read() wants a path
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".vtt", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        tmp_path = f.name

    try:
        for caption in webvtt.read(tmp_path):
            speaker, text = _speaker_from_cue(caption.raw_text, caption.text)
            if not text:
                continue
            turns.append(Turn(
                speaker=speaker,
                start_time=_normalize_ts(caption.start),
                end_time=_normalize_ts(caption.end),
                text=text,
            ))
    finally:
        os.unlink(tmp_path)

    return turns


# ─── TXT Parser ───────────────────────────────────────────────────────────────

def parse_txt(content: str) -> list[Turn]:
    """
    Parse a plain-text transcript into speaker turns via regex.

    Lines that don't look like a new speaker turn are appended to the
    previous turn's text (handles multi-line speeches).

    Fills in approximate end_time for each turn from the next turn's
    start_time when timestamps are present.
    """
    turns: list[Turn] = []

    for m in _SPEAKER_RE.finditer(content):
        raw_ts = m.group(1)
        speaker = m.group(2).strip()
        text = m.group(3).strip()

        start_time: Optional[str] = None
        if raw_ts:
            start_time = _normalize_ts(raw_ts)

        turns.append(Turn(speaker=speaker, start_time=start_time, end_time=None, text=text))

    # Derive end_time from next turn's start
    for i in range(len(turns) - 1):
        if turns[i].start_time and turns[i + 1].start_time:
            turns[i].end_time = turns[i + 1].start_time

    return turns


# ─── Chunking ─────────────────────────────────────────────────────────────────

def _split_long_turn(turn: Turn) -> list[Turn]:
    """
    Split a turn that exceeds MAX_TOKENS at sentence boundaries.

    We split on sentence-ending punctuation (`. ? !`) followed by whitespace.
    Timestamps are interpolated linearly based on word count — not exact but
    good enough for the citation display (which shows minutes, not seconds).
    """
    sentences = re.split(r"(?<=[.!?])\s+", turn.text.strip())

    start_secs = _to_secs(turn.start_time) if turn.start_time else None
    end_secs   = _to_secs(turn.end_time)   if turn.end_time   else None
    total_words = max(len(turn.text.split()), 1)

    def interp(words_before: int, words_chunk: int) -> tuple[Optional[str], Optional[str]]:
        """Linear time interpolation for a sub-chunk."""
        if start_secs is None or end_secs is None:
            return turn.start_time, turn.end_time
        dur = end_secs - start_secs
        cs = start_secs + int(dur * words_before / total_words)
        ce = start_secs + int(dur * (words_before + words_chunk) / total_words)
        return _to_ts(cs), _to_ts(ce)

    result: list[Turn] = []
    buf: list[str] = []
    buf_tokens = 0
    words_before = 0

    for sentence in sentences:
        s_tokens = _tokens(sentence)
        if buf_tokens + s_tokens > TARGET_TOKENS and buf:
            chunk_text = " ".join(buf)
            chunk_words = len(chunk_text.split())
            cs, ce = interp(words_before - chunk_words, chunk_words)
            result.append(Turn(speaker=turn.speaker, start_time=cs, end_time=ce, text=chunk_text))
            buf = [sentence]
            buf_tokens = s_tokens
        else:
            buf.append(sentence)
            buf_tokens += s_tokens
        words_before += len(sentence.split())

    if buf:
        chunk_text = " ".join(buf)
        chunk_words = len(chunk_text.split())
        cs, ce = interp(words_before - chunk_words, chunk_words)
        result.append(Turn(speaker=turn.speaker, start_time=cs, end_time=ce, text=chunk_text))

    return result


def chunk_turns(turns: list[Turn]) -> list[ChunkData]:
    """
    Convert raw speaker turns into child chunks.

    Two-pass algorithm:
      Pass 1 — merge short consecutive same-speaker turns.
               Avoids a flood of tiny chunks from fast back-and-forth dialogue.
      Pass 2 — split any turn over MAX_TOKENS at sentence boundaries.
               Prevents a single long monologue from dominating retrieval.

    Why 400 tokens?
      Small enough that embeddings stay semantically focused (one topic per chunk).
      Large enough to contain enough context for a meaningful answer.
    """
    if not turns:
        return []

    # Pass 1: merge short same-speaker turns
    merged: list[Turn] = [turns[0]]
    for turn in turns[1:]:
        prev = merged[-1]
        same_spk = prev.speaker == turn.speaker or (prev.speaker is None and turn.speaker is None)
        prev_tok = _tokens(prev.text)
        curr_tok = _tokens(turn.text)

        if same_spk and prev_tok < MIN_TOKENS and (prev_tok + curr_tok) < TARGET_TOKENS:
            merged[-1] = Turn(
                speaker=prev.speaker,
                start_time=prev.start_time,
                end_time=turn.end_time,
                text=prev.text + " " + turn.text,
            )
        else:
            merged.append(turn)

    # Pass 2: split over-long turns
    final: list[Turn] = []
    for turn in merged:
        if _tokens(turn.text) > MAX_TOKENS and turn.start_time and turn.end_time:
            final.extend(_split_long_turn(turn))
        else:
            final.append(turn)

    return [
        ChunkData(
            speaker=t.speaker,
            start_time=t.start_time,
            end_time=t.end_time,
            text=t.text,
            token_count=_tokens(t.text),
            is_parent=False,
        )
        for t in final
    ]


# ─── Parent Chunk Builder ─────────────────────────────────────────────────────

def build_parent_chunks(
    child_chunks: list[ChunkData],
) -> tuple[list[ChunkData], list[ChunkData]]:
    """
    Group child chunks into 5-minute parent windows.

    Returns (child_chunks, parent_chunks):
      - child_chunks: same list, each chunk now has parent_index set
      - parent_chunks: one entry per 5-minute window, is_parent=True

    The parent's text is all children's text concatenated with speaker labels.
    This is what the LLM reads as context after retrieval picks the right child.

    Chunks with no timestamp are grouped into a single parent at the end.
    """
    if not child_chunks:
        return [], []

    parents: list[ChunkData] = []
    window_children: list[int] = []   # indices into child_chunks
    window_start: Optional[int] = None

    # Find the first chunk with a timestamp to anchor the first window
    for chunk in child_chunks:
        if chunk.start_time:
            window_start = _to_secs(chunk.start_time)
            break

    def _flush(indices: list[int]) -> ChunkData:
        children = [child_chunks[i] for i in indices]
        # Build readable text: "Speaker: text\nSpeaker: text\n..."
        lines = [
            f"{c.speaker}: {c.text}" if c.speaker else c.text
            for c in children
        ]
        # Speaker field: use the most common speaker, or None if mixed
        spk_counts: dict[str, int] = {}
        for c in children:
            if c.speaker:
                spk_counts[c.speaker] = spk_counts.get(c.speaker, 0) + 1
        parent_speaker = max(spk_counts, key=lambda k: spk_counts[k]) if spk_counts else None
        return ChunkData(
            speaker=parent_speaker,
            start_time=children[0].start_time,
            end_time=children[-1].end_time,
            text="\n".join(lines),
            token_count=sum(c.token_count for c in children),
            is_parent=True,
        )

    for i, chunk in enumerate(child_chunks):
        chunk_secs = _to_secs(chunk.start_time) if chunk.start_time else None

        # Check if this chunk starts a new 5-minute window
        if chunk_secs is not None and window_start is not None:
            if chunk_secs - window_start >= PARENT_WINDOW_SECONDS:
                if window_children:
                    parent = _flush(window_children)
                    pidx = len(parents)
                    parents.append(parent)
                    for j in window_children:
                        child_chunks[j].parent_index = pidx
                    window_children = []
                window_start = chunk_secs
        elif window_start is None and chunk_secs is not None:
            window_start = chunk_secs

        window_children.append(i)

    # Flush the final (or only) window
    if window_children:
        parent = _flush(window_children)
        pidx = len(parents)
        parents.append(parent)
        for j in window_children:
            child_chunks[j].parent_index = pidx

    return child_chunks, parents


# ─── Metadata Extraction ──────────────────────────────────────────────────────

def extract_metadata(turns: list[Turn], filename: str) -> dict:
    """
    Derive meeting metadata from the parsed turns and filename.

    speaker_names: unique speakers in order of first appearance
    word_count:    total words across all turns (for UI display)
    meeting_date:  parsed from filename pattern YYYY-MM-DD or YYYY_MM_DD
    """
    seen: set[str] = set()
    speaker_names: list[str] = []
    word_count = 0

    for turn in turns:
        word_count += len(turn.text.split())
        if turn.speaker and turn.speaker not in seen:
            seen.add(turn.speaker)
            speaker_names.append(turn.speaker)

    meeting_date: Optional[datetime] = None
    m = _DATE_IN_FILENAME_RE.search(filename)
    if m:
        date_str = m.group(1).replace("_", "-")
        try:
            meeting_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            pass

    return {
        "speaker_names": speaker_names,
        "word_count": word_count,
        "meeting_date": meeting_date,
    }


# ─── Public Entry Point ───────────────────────────────────────────────────────

def parse_transcript(content: str, filename: str) -> ParseResult:
    """
    Full parse pipeline for a single transcript file.

    Steps:
      1. Choose parser based on file extension (.vtt → parse_vtt, else → parse_txt)
      2. Merge short turns + split long turns → child chunks
      3. Group child chunks into 5-min parent windows
      4. Extract metadata (speakers, word count, date)

    Returns a ParseResult consumed by the Celery pipeline task.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"

    if ext == "vtt":
        turns = parse_vtt(content)
    else:
        turns = parse_txt(content)

    child_chunks = chunk_turns(turns)
    child_chunks, parent_chunks = build_parent_chunks(child_chunks)
    meta = extract_metadata(turns, filename)

    return ParseResult(
        child_chunks=child_chunks,
        parent_chunks=parent_chunks,
        speaker_names=meta["speaker_names"],
        word_count=meta["word_count"],
        meeting_date=meta["meeting_date"],
    )
