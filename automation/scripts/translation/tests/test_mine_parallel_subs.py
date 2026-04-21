"""Tests for mine_parallel_subs — alignment, quality filter, dedup, and e2e."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from translation.mine_parallel_subs import (
    apply_series_cap,
    check_alignment,
    cli,
    dedup,
    extract_pairs,
    extract_series_key,
    format_jsonl,
    quality_filter,
    scan_pairs,
)
from translation.srt_parser import Cue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cue(index: int, start: str, end: str, text: str) -> Cue:
    return Cue(index=index, start=start, end=end, text=text)


def _cues(entries):
    """Build a list of Cue objects from (start, end, text) triples."""
    return [
        _make_cue(i + 1, start, end, text)
        for i, (start, end, text) in enumerate(entries)
    ]


_BASE_EN = [
    ("00:00:01,000", "00:00:03,000", "Hello there."),
    ("00:00:04,000", "00:00:06,000", "How are you?"),
    ("00:00:07,000", "00:00:09,000", "I am fine, thanks."),
]
_BASE_ES = [
    ("00:00:01,050", "00:00:03,050", "Hola."),
    ("00:00:04,080", "00:00:06,080", "¿Cómo estás?"),
    ("00:00:07,100", "00:00:09,100", "Estoy bien, gracias."),
]


# ---------------------------------------------------------------------------
# check_alignment
# ---------------------------------------------------------------------------

class TestCheckAlignment:
    def test_perfect_match(self):
        """Identical timestamps pass alignment."""
        en = _cues(_BASE_EN)
        es = _cues(_BASE_EN)
        assert check_alignment(en, es, strict_ms=200) is True

    def test_within_tolerance(self):
        """Small timing offset within 200 ms passes."""
        en = _cues(_BASE_EN)
        es = _cues(_BASE_ES)
        assert check_alignment(en, es, strict_ms=200) is True

    def test_count_mismatch_fails(self):
        """Different cue counts always reject."""
        en = _cues(_BASE_EN)
        es = _cues(_BASE_ES[:2])
        assert check_alignment(en, es, strict_ms=200) is False

    def test_start_outside_tolerance_fails(self):
        """Start offset > threshold rejects the whole pair."""
        en = _cues([("00:00:01,000", "00:00:03,000", "Hello.")])
        # 500 ms offset — exceeds 200 ms threshold
        es = _cues([("00:00:01,500", "00:00:03,000", "Hola.")])
        assert check_alignment(en, es, strict_ms=200) is False

    def test_end_outside_tolerance_fails(self):
        """End offset > threshold rejects."""
        en = _cues([("00:00:01,000", "00:00:03,000", "Hello.")])
        es = _cues([("00:00:01,000", "00:00:03,500", "Hola.")])
        assert check_alignment(en, es, strict_ms=200) is False

    def test_exact_boundary_passes(self):
        """Offset exactly at threshold (exclusive < strict_ms) passes."""
        en = _cues([("00:00:01,000", "00:00:03,000", "Hello.")])
        es = _cues([("00:00:01,199", "00:00:03,199", "Hola.")])
        assert check_alignment(en, es, strict_ms=200) is True

    def test_at_strict_threshold_fails(self):
        """Offset equal to strict_ms fails (threshold is exclusive)."""
        en = _cues([("00:00:01,000", "00:00:03,000", "Hello.")])
        es = _cues([("00:00:01,200", "00:00:03,200", "Hola.")])
        assert check_alignment(en, es, strict_ms=200) is False

    def test_empty_lists_pass(self):
        """Two empty SRTs trivially align."""
        assert check_alignment([], [], strict_ms=200) is True

    def test_custom_strict_ms(self):
        """Custom strict_ms=50 is more restrictive."""
        en = _cues([("00:00:01,000", "00:00:03,000", "Hello.")])
        # 100 ms offset — ok for 200 ms, but fails for 50 ms
        es = _cues([("00:00:01,100", "00:00:03,100", "Hola.")])
        assert check_alignment(en, es, strict_ms=200) is True
        assert check_alignment(en, es, strict_ms=50) is False


# ---------------------------------------------------------------------------
# quality_filter
# ---------------------------------------------------------------------------

class TestQualityFilter:
    def test_good_pair_passes(self):
        assert quality_filter("Hello, how are you?", "Hola, ¿cómo estás?") is True

    def test_too_short_english(self):
        assert quality_filter("Hi", "Hola, ¿cómo estás?") is False

    def test_too_short_spanish(self):
        assert quality_filter("Hello, how are you?", "Hola") is False

    def test_both_too_short(self):
        assert quality_filter("Hi", "Hola") is False

    def test_punctuation_only_english(self):
        assert quality_filter("...", "Hola, ¿cómo estás?") is False

    def test_punctuation_only_spanish(self):
        assert quality_filter("Hello there.", "...") is False

    def test_numbers_only(self):
        assert quality_filter("1234567890", "1234567890") is False

    def test_length_ratio_too_small(self):
        # Spanish is less than 30% of English length
        en = "This is a very long English sentence with lots of words in it."
        es = "Sí."
        assert quality_filter(en, es) is False

    def test_length_ratio_too_large(self):
        en = "Yes."
        es = "Esta es una frase extremadamente larga que supera con creces el doble de la longitud del inglés."
        assert quality_filter(en, es) is False

    def test_ass_tag_rejected(self):
        assert quality_filter("{\\an8}Hello there.", "Hola, ¿cómo estás?") is False

    def test_sound_cue_rejected(self):
        assert quality_filter("[GUNSHOT]", "[DISPARO]") is False

    def test_music_indicator_rejected(self):
        assert quality_filter("♪ ♪ ♪", "♪ ♪ ♪") is False

    def test_music_word_rejected(self):
        assert quality_filter("music", "música") is False

    def test_high_spanish_char_ratio_in_english(self):
        # More than 5% of alpha chars are Spanish accents
        assert quality_filter("Señorita, ¿cómo estás tú aquí?", "Señorita, ¿cómo estás tú aquí?") is False

    def test_custom_min_length(self):
        assert quality_filter("Hello.", "Hola.", min_length=4) is True
        assert quality_filter("Hi", "Hi", min_length=4) is False

    def test_html_tags_already_stripped(self):
        """Tags should have been stripped before calling quality_filter; plain text passes."""
        assert quality_filter("That is really cool.", "Eso es muy genial.") is True


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_unique_pairs_unchanged(self):
        pairs = [("a", "b", "s"), ("c", "d", "s"), ("e", "f", "s")]
        assert dedup(pairs, max_copies=3) == pairs

    def test_duplicates_limited(self):
        pairs = [("a", "b", "s1")] * 5
        result = dedup(pairs, max_copies=3)
        assert len(result) == 3
        assert all(p == ("a", "b", "s1") for p in result)

    def test_max_copies_one(self):
        pairs = [("a", "b", "s")] * 4 + [("c", "d", "s")] * 4
        result = dedup(pairs, max_copies=1)
        assert len(result) == 2

    def test_different_sources_same_text_deduped(self):
        """Same (en, es) text from different source paths still counts as dup."""
        pairs = [("hello", "hola", "/path/a"), ("hello", "hola", "/path/b")]
        result = dedup(pairs, max_copies=1)
        assert len(result) == 1

    def test_order_preserved(self):
        pairs = [("a", "1", "s"), ("b", "2", "s"), ("a", "1", "s")]
        result = dedup(pairs, max_copies=1)
        assert result == [("a", "1", "s"), ("b", "2", "s")]


# ---------------------------------------------------------------------------
# extract_pairs
# ---------------------------------------------------------------------------

class TestExtractPairs:
    def test_strips_html(self):
        en = _cues([("00:00:01,000", "00:00:02,000", "<i>Hello</i>")])
        es = _cues([("00:00:01,000", "00:00:02,000", "<i>Hola</i>")])
        pairs = extract_pairs(en, es)
        assert pairs[0] == ("Hello", "Hola")

    def test_strips_ass_tags(self):
        en = _cues([("00:00:01,000", "00:00:02,000", "{\\an8}Hello there.")])
        es = _cues([("00:00:01,000", "00:00:02,000", "{\\an8}Hola.")])
        pairs = extract_pairs(en, es)
        assert pairs[0] == ("Hello there.", "Hola.")

    def test_newlines_joined_with_space(self):
        en = _cues([("00:00:01,000", "00:00:02,000", "Hello\nthere")])
        es = _cues([("00:00:01,000", "00:00:02,000", "Hola\nallí")])
        pairs = extract_pairs(en, es)
        assert pairs[0] == ("Hello there", "Hola allí")

    def test_collapses_whitespace(self):
        en = _cues([("00:00:01,000", "00:00:02,000", "Hello   world")])
        es = _cues([("00:00:01,000", "00:00:02,000", "Hola   mundo")])
        pairs = extract_pairs(en, es)
        assert pairs[0] == ("Hello world", "Hola mundo")


# ---------------------------------------------------------------------------
# format_jsonl
# ---------------------------------------------------------------------------

class TestFormatJsonl:
    def test_valid_json(self):
        line = format_jsonl("Hello.", "Hola.", "/path/to/file.en.srt")
        obj = json.loads(line)
        assert obj["input"] == "Hello."
        assert obj["output"] == "Hola."
        assert obj["source"] == "/path/to/file.en.srt"
        assert "instruction" in obj

    def test_unicode_preserved(self):
        line = format_jsonl("Cool.", "¡Genial!", "/path")
        obj = json.loads(line)
        assert obj["output"] == "¡Genial!"


# ---------------------------------------------------------------------------
# scan_pairs
# ---------------------------------------------------------------------------

class TestScanPairs:
    def test_finds_matching_pair(self, tmp_path):
        srt_content = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
        (tmp_path / "Show.S01E01.en.srt").write_text(srt_content)
        (tmp_path / "Show.S01E01.es.srt").write_text(srt_content)
        pairs = scan_pairs([str(tmp_path)])
        assert len(pairs) == 1
        assert pairs[0][0].name == "Show.S01E01.en.srt"
        assert pairs[0][1].name == "Show.S01E01.es.srt"

    def test_ignores_missing_es(self, tmp_path):
        (tmp_path / "Show.S01E01.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        pairs = scan_pairs([str(tmp_path)])
        assert pairs == []

    def test_nonexistent_root_skipped(self):
        pairs = scan_pairs(["/nonexistent/path/that/does/not/exist"])
        assert pairs == []

    def test_recursive_walk(self, tmp_path):
        sub = tmp_path / "season1"
        sub.mkdir()
        srt = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
        (sub / "ep.en.srt").write_text(srt)
        (sub / "ep.es.srt").write_text(srt)
        pairs = scan_pairs([str(tmp_path)])
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# End-to-end CLI tests
# ---------------------------------------------------------------------------

_ALIGNED_EN_SRT = """\
1
00:00:01,000 --> 00:00:03,000
Hello, how are you?

2
00:00:04,000 --> 00:00:06,000
I am doing great today.

3
00:00:07,000 --> 00:00:09,000
That is really wonderful news.
"""

_ALIGNED_ES_SRT = """\
1
00:00:01,050 --> 00:00:03,050
Hola, ¿cómo estás?

2
00:00:04,080 --> 00:00:06,080
Estoy muy bien hoy en día.

3
00:00:07,100 --> 00:00:09,100
Eso es una noticia realmente maravillosa.
"""

_MISALIGNED_EN_SRT = """\
1
00:00:01,000 --> 00:00:03,000
Hello, how are you?

2
00:00:04,000 --> 00:00:06,000
Second line here.
"""

_MISALIGNED_ES_SRT = """\
1
00:00:01,000 --> 00:00:03,000
Hola, ¿cómo estás?

2
00:00:05,000 --> 00:00:07,000
Segunda línea aquí.
"""


class TestCli:
    def test_dry_run(self, tmp_path):
        (tmp_path / "ep.en.srt").write_text(_ALIGNED_EN_SRT)
        (tmp_path / "ep.es.srt").write_text(_ALIGNED_ES_SRT)
        runner = CliRunner()
        result = runner.invoke(cli, ["--roots", str(tmp_path), "--dry-run"])
        assert result.exit_code == 0, result.output + str(result.exception)
        assert "dry-run" in result.output.lower()
        # No JSONL emitted
        assert "{" not in result.output

    def test_aligned_pair_produces_output(self, tmp_path):
        (tmp_path / "ep.en.srt").write_text(_ALIGNED_EN_SRT)
        (tmp_path / "ep.es.srt").write_text(_ALIGNED_ES_SRT)
        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--roots", str(tmp_path),
            "--output", str(out_file),
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_file.read_text().strip().splitlines()
        assert len(lines) == 3
        obj = json.loads(lines[0])
        assert obj["input"] == "Hello, how are you?"
        assert obj["output"] == "Hola, ¿cómo estás?"

    def test_misaligned_pair_rejected(self, tmp_path):
        (tmp_path / "ep.en.srt").write_text(_MISALIGNED_EN_SRT)
        (tmp_path / "ep.es.srt").write_text(_MISALIGNED_ES_SRT)
        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--roots", str(tmp_path),
            "--output", str(out_file),
            "--strict-ms", "200",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        assert "rejected" in result.output
        # No output lines because pair was rejected
        assert not out_file.exists() or out_file.read_text().strip() == ""

    def test_dedup_limit_respected(self, tmp_path):
        # Create 3 different files with the same cue content
        for i in range(3):
            sub = tmp_path / f"s{i}"
            sub.mkdir()
            (sub / "ep.en.srt").write_text(_ALIGNED_EN_SRT)
            (sub / "ep.es.srt").write_text(_ALIGNED_ES_SRT)
        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--roots", str(tmp_path),
            "--output", str(out_file),
            "--max-dup-copies", "1",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_file.read_text().strip().splitlines()
        # 3 unique cue pairs, 1 copy each = 3 lines max
        assert len(lines) == 3

    def test_stdout_output(self, tmp_path):
        """When no --output is given, JSONL goes to stdout."""
        (tmp_path / "ep.en.srt").write_text(_ALIGNED_EN_SRT)
        (tmp_path / "ep.es.srt").write_text(_ALIGNED_ES_SRT)
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["--roots", str(tmp_path)])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        assert len(lines) == 3
        json.loads(lines[0])  # must be valid JSON


# ---------------------------------------------------------------------------
# extract_series_key
# ---------------------------------------------------------------------------

class TestExtractSeriesKey:
    def test_tv_root_returns_show_name(self):
        roots = ["/APPBOX_DATA/storage/media/tv"]
        path = Path("/APPBOX_DATA/storage/media/tv/Breaking Bad/Season 1/ep.en.srt")
        assert extract_series_key(path, roots) == "Breaking Bad"

    def test_movie_root_returns_movie_prefix(self):
        roots = ["/APPBOX_DATA/storage/media/movies"]
        path = Path("/APPBOX_DATA/storage/media/movies/Inception (2010)/Inception.en.srt")
        assert extract_series_key(path, roots) == "MOVIE:Inception (2010)"

    def test_tvanimated_root_returns_show_name(self):
        roots = ["/APPBOX_DATA/storage/media/tvanimated"]
        path = Path("/APPBOX_DATA/storage/media/tvanimated/Naruto/Season 1/ep.en.srt")
        assert extract_series_key(path, roots) == "Naruto"

    def test_unknown_root_fallback_uses_parent_dir(self, tmp_path):
        sub = tmp_path / "MyShow" / "Season 1"
        sub.mkdir(parents=True)
        ep = sub / "ep.en.srt"
        ep.touch()
        key = extract_series_key(ep, [str(tmp_path)])
        # Under tmp_path, first component is "MyShow"
        assert key == "MyShow"

    def test_no_matching_root_uses_parent_name(self, tmp_path):
        ep = tmp_path / "ep.en.srt"
        ep.touch()
        key = extract_series_key(ep, ["/some/other/root"])
        # No root matches; fallback = parent directory name
        assert key == tmp_path.name


# ---------------------------------------------------------------------------
# apply_series_cap
# ---------------------------------------------------------------------------

class TestApplySeriesCap:
    def _make_pairs(self, series_pairs):
        """Build (en, es, source) triples where source contains series path."""
        result = []
        for series, count in series_pairs:
            for i in range(count):
                src = f"/APPBOX_DATA/storage/media/tv/{series}/Season 1/ep{i}.en.srt"
                result.append((f"Hello {series} {i}", f"Hola {series} {i}", src))
        return result

    def test_no_cap_passthrough(self):
        """max_per_series=0 returns all pairs unchanged."""
        pairs = self._make_pairs([("ShowA", 100), ("ShowB", 50)])
        result = apply_series_cap(pairs, 0, ["/APPBOX_DATA/storage/media/tv"])
        assert result == pairs

    def test_cap_applied_per_series(self):
        """Each series is capped independently."""
        roots = ["/APPBOX_DATA/storage/media/tv"]
        pairs = self._make_pairs([("ShowA", 100), ("ShowB", 30)])
        result = apply_series_cap(pairs, 50, roots)
        from collections import Counter
        keys = [src.split("/tv/")[1].split("/")[0] for _, _, src in result]
        counts = Counter(keys)
        assert counts["ShowA"] == 50
        assert counts["ShowB"] == 30  # under cap, unchanged

    def test_cap_deterministic_with_seed(self):
        """Same seed produces same sample each run."""
        roots = ["/APPBOX_DATA/storage/media/tv"]
        pairs = self._make_pairs([("ShowA", 200)])
        r1 = apply_series_cap(pairs, 50, roots, seed=42)
        r2 = apply_series_cap(pairs, 50, roots, seed=42)
        assert r1 == r2

    def test_different_seeds_may_differ(self):
        """Different seeds can produce different samples (probabilistic)."""
        roots = ["/APPBOX_DATA/storage/media/tv"]
        pairs = self._make_pairs([("ShowA", 200)])
        r1 = apply_series_cap(pairs, 50, roots, seed=1)
        r2 = apply_series_cap(pairs, 50, roots, seed=999)
        # Very likely to differ with 200 items sampled to 50
        assert set(map(str, r1)) != set(map(str, r2))

    def test_total_count_bounded(self):
        """Total output never exceeds series_count × cap."""
        roots = ["/APPBOX_DATA/storage/media/tv"]
        pairs = self._make_pairs([("A", 1000), ("B", 1000), ("C", 1000)])
        result = apply_series_cap(pairs, 100, roots)
        assert len(result) <= 300


# ---------------------------------------------------------------------------
# CLI max-per-series integration
# ---------------------------------------------------------------------------

class TestCliMaxPerSeries:
    def test_max_per_series_limits_output(self, tmp_path):
        """When multiple files from same series exceed cap, output is capped."""
        # Create a "TV show" directory structure that matches tv root
        # We can't use the real root, so use tmp_path as the root and put
        # enough episodes to exceed cap=2
        show_dir = tmp_path / "My Show" / "Season 1"
        show_dir.mkdir(parents=True)
        for i in range(4):
            (show_dir / f"ep{i}.en.srt").write_text(_ALIGNED_EN_SRT)
            (show_dir / f"ep{i}.es.srt").write_text(_ALIGNED_ES_SRT)
        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--roots", str(tmp_path),
            "--output", str(out_file),
            "--max-per-series", "2",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        assert "Series cap" in result.output
        lines = out_file.read_text().strip().splitlines()
        # Cap is 2, so at most 2 pairs total (all from same "My Show" series)
        assert len(lines) <= 2

    def test_max_per_series_zero_no_cap(self, tmp_path):
        """--max-per-series 0 imposes no cap (all pairs kept)."""
        show_dir = tmp_path / "My Show" / "Season 1"
        show_dir.mkdir(parents=True)
        (show_dir / "ep.en.srt").write_text(_ALIGNED_EN_SRT)
        (show_dir / "ep.es.srt").write_text(_ALIGNED_ES_SRT)
        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--roots", str(tmp_path),
            "--output", str(out_file),
            "--max-per-series", "0",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_file.read_text().strip().splitlines()
        assert len(lines) == 3  # all 3 cues in the aligned SRT
