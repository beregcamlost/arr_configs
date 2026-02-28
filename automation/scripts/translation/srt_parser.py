"""Parse and write SRT subtitle files."""

import re
from dataclasses import dataclass
from typing import List

TIMESTAMP_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass
class Cue:
    index: int
    start: str
    end: str
    text: str


def parse_srt(content: str) -> List[Cue]:
    """Parse SRT content into a list of Cue objects.

    Handles BOM, Windows line endings, and various spacing quirks.
    """
    # Strip BOM and normalize line endings
    content = content.lstrip("\ufeff").replace("\r\n", "\n").strip()
    if not content:
        return []

    cues = []
    # Split on blank lines (two or more newlines)
    blocks = re.split(r"\n\n+", content)

    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # Find the timestamp line (may or may not have index before it)
        ts_line_idx = None
        for i, line in enumerate(lines):
            if TIMESTAMP_RE.search(line):
                ts_line_idx = i
                break

        if ts_line_idx is None:
            continue

        match = TIMESTAMP_RE.search(lines[ts_line_idx])
        start, end = match.group(1), match.group(2)

        # Index is the line before the timestamp (if present and numeric)
        index = 0
        if ts_line_idx > 0 and lines[ts_line_idx - 1].strip().isdigit():
            index = int(lines[ts_line_idx - 1].strip())

        # Text is everything after the timestamp line
        text_lines = lines[ts_line_idx + 1:]
        text = "\n".join(text_lines).strip()

        if text:
            cues.append(Cue(index=index, start=start, end=end, text=text))

    return cues


def write_srt(cues: List[Cue]) -> str:
    """Write cues to SRT format string with sequential indices."""
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(f"{i}\n{cue.start} --> {cue.end}\n{cue.text}")
    return "\n\n".join(blocks) + "\n"
