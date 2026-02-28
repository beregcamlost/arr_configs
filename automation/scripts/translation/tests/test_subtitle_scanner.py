"""Tests for subtitle scanner."""

import os
from translation.subtitle_scanner import (
    find_best_source_srt,
    find_missing_langs_on_disk,
    parse_missing_subtitles,
)


def test_parse_missing_subtitles_python_repr():
    """Parse Bazarr's Python-repr missing_subtitles format."""
    assert parse_missing_subtitles("['es']") == ["es"]
    assert parse_missing_subtitles("['en', 'es']") == ["en", "es"]
    assert parse_missing_subtitles("[]") == []


def test_parse_missing_subtitles_json():
    """Parse JSON-format missing_subtitles."""
    assert parse_missing_subtitles('["es"]') == ["es"]
    assert parse_missing_subtitles('["en", "es"]') == ["en", "es"]


def test_parse_missing_subtitles_with_forced():
    """Parse missing_subtitles with forced/hi suffixes."""
    result = parse_missing_subtitles("['es:forced', 'en:hi']")
    assert result == ["es:forced", "en:hi"]


def test_parse_missing_subtitles_empty():
    """Parse empty/null missing_subtitles."""
    assert parse_missing_subtitles("") == []
    assert parse_missing_subtitles(None) == []


def test_find_best_source_srt(tmp_path):
    """find_best_source_srt returns largest non-target, non-forced SRT."""
    stem = "Movie.2024.1080p"
    # Create SRTs of different sizes
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n" * 100)
    es_srt = tmp_path / f"{stem}.es.srt"
    es_srt.write_text("small")
    forced = tmp_path / f"{stem}.en.forced.srt"
    forced.write_text("forced sub")

    # Looking for French — should pick English (largest non-forced)
    result = find_best_source_srt(str(tmp_path), stem, target_lang="fr")
    assert result is not None
    assert result.endswith(".en.srt")


def test_find_best_source_srt_skips_target(tmp_path):
    """find_best_source_srt skips SRTs matching target language."""
    stem = "Movie"
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("English text " * 50)
    es_srt = tmp_path / f"{stem}.es.srt"
    es_srt.write_text("Spanish text " * 100)  # larger but it's the target

    result = find_best_source_srt(str(tmp_path), stem, target_lang="es")
    assert result.endswith(".en.srt")


def test_find_best_source_srt_prefers_english(tmp_path):
    """find_best_source_srt prefers English source when sizes are similar."""
    stem = "Movie"
    en_srt = tmp_path / f"{stem}.en.srt"
    en_srt.write_text("A" * 1000)
    fr_srt = tmp_path / f"{stem}.fr.srt"
    fr_srt.write_text("B" * 1050)  # slightly larger but not English

    result = find_best_source_srt(str(tmp_path), stem, target_lang="es")
    # English preferred when within 20% size of largest
    assert result.endswith(".en.srt")


def test_find_best_source_srt_none(tmp_path):
    """find_best_source_srt returns None when no candidates exist."""
    result = find_best_source_srt(str(tmp_path), "nonexistent", "es")
    assert result is None


def test_find_missing_langs_on_disk(tmp_path):
    """find_missing_langs_on_disk returns profile langs without SRT on disk."""
    stem = "Movie"
    # English SRT exists, Spanish does not
    (tmp_path / f"{stem}.en.srt").write_text("English")
    profile_langs = ["en", "es", "fr"]
    missing = find_missing_langs_on_disk(str(tmp_path), stem, profile_langs)
    assert "es" in missing
    assert "fr" in missing
    assert "en" not in missing
