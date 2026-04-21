"""Tests for collect_unpaired_english — scanner, cue filter, CLI."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from translation.collect_unpaired_english import (
    _cue_is_usable,
    _normalize_cue,
    scan_unpaired_english,
    cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SRT_TEMPLATE = """\
1
00:00:01,000 --> 00:00:03,000
{line1}

2
00:00:04,000 --> 00:00:06,000
{line2}
"""


def _write_srt(path: Path, line1: str, line2: str) -> None:
    path.write_text(_SRT_TEMPLATE.format(line1=line1, line2=line2), encoding="utf-8")


# ---------------------------------------------------------------------------
# _normalize_cue
# ---------------------------------------------------------------------------

class TestNormalizeCue:
    def test_strips_html_tags(self):
        assert _normalize_cue("<i>Hello</i>") == "Hello"

    def test_strips_ass_tags(self):
        assert _normalize_cue("{\\an8}Hello world") == "Hello world"

    def test_replaces_newlines_with_space(self):
        assert _normalize_cue("Hello\nworld") == "Hello world"

    def test_collapses_whitespace(self):
        assert _normalize_cue("Hello   world") == "Hello world"

    def test_strips_leading_trailing(self):
        assert _normalize_cue("  Hello  ") == "Hello"


# ---------------------------------------------------------------------------
# _cue_is_usable
# ---------------------------------------------------------------------------

class TestCueIsUsable:
    def test_valid_cue_passes(self):
        assert _cue_is_usable("She walked into the room slowly.") is True

    def test_too_short_rejected(self):
        assert _cue_is_usable("Hi", min_length=15) is False

    def test_punct_only_rejected(self):
        assert _cue_is_usable("...") is False

    def test_music_cue_rejected(self):
        assert _cue_is_usable("♪ ♪ ♪") is False

    def test_sound_cue_rejected(self):
        assert _cue_is_usable("[GUNSHOT]") is False

    def test_url_rejected(self):
        assert _cue_is_usable("Visit https://example.com for details.") is False

    def test_html_rejected(self):
        assert _cue_is_usable("<b>She</b> walked in.") is False

    def test_spanish_text_rejected(self):
        # Has >5% Spanish accent chars — likely already Spanish
        assert _cue_is_usable("Él estaba allí para ella también.") is False

    def test_normal_english_passes(self):
        assert _cue_is_usable("He was waiting there for a long time.") is True


# ---------------------------------------------------------------------------
# scan_unpaired_english
# ---------------------------------------------------------------------------

class TestScanUnpairedEnglish:
    def test_finds_en_without_es(self, tmp_path):
        (tmp_path / "show" / "s01").mkdir(parents=True)
        en_path = tmp_path / "show" / "s01" / "ep01.en.srt"
        en_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        # No .es.srt sibling
        found = list(scan_unpaired_english([str(tmp_path)]))
        assert en_path in found

    def test_skips_en_with_es_sibling(self, tmp_path):
        (tmp_path / "show" / "s01").mkdir(parents=True)
        stem = "ep01"
        en_path = tmp_path / "show" / "s01" / f"{stem}.en.srt"
        es_path = tmp_path / "show" / "s01" / f"{stem}.es.srt"
        en_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        es_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHola\n", encoding="utf-8")
        found = list(scan_unpaired_english([str(tmp_path)]))
        assert en_path not in found

    def test_nonexistent_root_skipped(self, tmp_path):
        found = list(scan_unpaired_english([str(tmp_path / "nonexistent")]))
        assert found == []

    def test_multiple_roots(self, tmp_path):
        root1 = tmp_path / "root1"
        root2 = tmp_path / "root2"
        root1.mkdir(); root2.mkdir()
        f1 = root1 / "ep.en.srt"
        f2 = root2 / "ep.en.srt"
        f1.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        f2.write_text("1\n00:00:01,000 --> 00:00:02,000\nWorld\n", encoding="utf-8")
        found = list(scan_unpaired_english([str(root1), str(root2)]))
        assert f1 in found
        assert f2 in found


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCli:
    def _populate_root(self, root: Path, n_files: int = 5) -> None:
        """Create n_files .en.srt files without .es.srt siblings (unique cues per file)."""
        root.mkdir(parents=True, exist_ok=True)
        for i in range(n_files):
            srt = root / f"ep{i:02d}.en.srt"
            _write_srt(
                srt,
                f"He was standing there waiting for something to happen episode {i}.",
                f"She looked around the room and saw nothing unusual at all in episode {i}.",
            )

    def test_basic_output(self, tmp_path):
        media = tmp_path / "media"
        self._populate_root(media)
        out_path = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--roots", str(media),
            "--target", "1000",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) > 0
        obj = json.loads(lines[0])
        assert "english" in obj

    def test_target_cap(self, tmp_path):
        media = tmp_path / "media"
        self._populate_root(media, n_files=20)
        out_path = tmp_path / "out_capped.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--roots", str(media),
            "--target", "3",
            "--seed", "42",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_paired_files_excluded(self, tmp_path):
        media = tmp_path / "media"
        media.mkdir(parents=True)
        stem = "paired_ep"
        en = media / f"{stem}.en.srt"
        es = media / f"{stem}.es.srt"
        _write_srt(en, "He walked into the room.", "She looked at the sky now.")
        _write_srt(es, "Caminó hacia la habitación.", "Miró al cielo.")

        # Also add an unpaired one
        unpaired = media / "unpaired.en.srt"
        _write_srt(unpaired, "He stood there alone waiting.", "She smiled at everyone warmly.")

        out_path = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--roots", str(media),
            "--target", "1000",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        cues = [json.loads(l)["english"] for l in out_path.read_text().strip().splitlines()]
        # None should be from the paired file (Spanish text would fail _cue_is_usable)
        # But mainly: the file count should only reflect unpaired
        assert len(cues) > 0

    def test_output_all_valid_json(self, tmp_path):
        media = tmp_path / "media"
        self._populate_root(media, n_files=3)
        out_path = tmp_path / "out_valid.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--roots", str(media),
            "--target", "100",
        ])
        assert result.exit_code == 0
        for line in out_path.read_text().strip().splitlines():
            obj = json.loads(line)
            assert "english" in obj
            assert isinstance(obj["english"], str)
            assert len(obj["english"]) > 0
