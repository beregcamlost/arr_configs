"""Tests for SRT parser."""

from translation.srt_parser import parse_srt, write_srt, Cue


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,000
Hello, world!

2
00:00:05,000 --> 00:00:08,500
This is a <i>test</i>
with multiple lines.

3
00:00:10,000 --> 00:00:12,000
Final cue.
"""


def test_parse_srt_basic():
    """Parse a simple SRT into cues."""
    cues = parse_srt(SAMPLE_SRT)
    assert len(cues) == 3
    assert cues[0].index == 1
    assert cues[0].start == "00:00:01,000"
    assert cues[0].end == "00:00:04,000"
    assert cues[0].text == "Hello, world!"


def test_parse_srt_multiline():
    """Parse preserves multiline cue text."""
    cues = parse_srt(SAMPLE_SRT)
    assert cues[1].text == "This is a <i>test</i>\nwith multiple lines."


def test_parse_srt_empty():
    """Parse empty string returns empty list."""
    assert parse_srt("") == []
    assert parse_srt("   \n\n  ") == []


def test_write_srt_roundtrip():
    """write_srt produces valid SRT that re-parses identically."""
    cues = parse_srt(SAMPLE_SRT)
    output = write_srt(cues)
    reparsed = parse_srt(output)
    assert len(reparsed) == len(cues)
    for orig, reparse in zip(cues, reparsed):
        assert orig.start == reparse.start
        assert orig.end == reparse.end
        assert orig.text == reparse.text


def test_write_srt_reindexes():
    """write_srt assigns sequential indices starting from 1."""
    cues = [
        Cue(index=5, start="00:00:01,000", end="00:00:02,000", text="A"),
        Cue(index=99, start="00:00:03,000", end="00:00:04,000", text="B"),
    ]
    output = write_srt(cues)
    assert output.startswith("1\n")
    assert "\n2\n" in output


def test_parse_srt_with_bom():
    """Parse handles UTF-8 BOM."""
    srt_with_bom = "\ufeff1\n00:00:01,000 --> 00:00:02,000\nHello\n"
    cues = parse_srt(srt_with_bom)
    assert len(cues) == 1
    assert cues[0].text == "Hello"


def test_total_chars():
    """Total character count of all cue text."""
    cues = parse_srt(SAMPLE_SRT)
    total = sum(len(c.text) for c in cues)
    assert total > 0
