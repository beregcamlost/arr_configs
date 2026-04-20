"""Tests for spell_check module."""

from translation.spell_check import validate_translated_cues, _detect_english_survivors


def test_english_survivors_skipped():
    """English words the LLM left untranslated should not get es_ES corrections."""
    # "everyone" and "hello" are valid English — should be skipped
    # "Mielévola" is a Spanish typo — should be flagged
    translated = ["everyone hello Mielévola malevola"]
    source = ["Everyone says hello to the evil one."]
    issues = validate_translated_cues(translated, source)

    # "malevola" appears in source (after lowercasing) — also skipped
    # "Mielévola" is NOT in source and is not valid English — must be flagged
    assert issues, "Expected at least one issue (Mielévola is a Spanish typo)"
    flagged = {b["word"] for b in issues[0]["bad_words"]}
    assert "everyone" not in flagged, "English word 'everyone' must not be flagged"
    assert "hello" not in flagged, "English word 'hello' must not be flagged"
    assert "Mielévola" in flagged or "mielévola" in flagged, \
        "Spanish typo 'Mielévola' must be flagged"


def test_detect_english_survivors_basic():
    """Valid English words are returned; non-English words are not."""
    survivors = _detect_english_survivors(["hello", "everyone", "translation", "mielévola", "creaturas"])
    assert "hello" in survivors
    assert "everyone" in survivors
    assert "translation" in survivors
    assert "mielévola" not in survivors
    assert "creaturas" not in survivors


def test_detect_english_survivors_empty():
    """Empty input returns empty set without errors."""
    assert _detect_english_survivors([]) == set()


def test_detect_english_survivors_case_normalized():
    """Input words are lowercased in the returned set."""
    survivors = _detect_english_survivors(["Hello", "TRANSLATION"])
    assert "hello" in survivors
    assert "translation" in survivors
