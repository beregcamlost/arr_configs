"""Tests for postprocess module."""

import pytest

from translation.postprocess import _is_prefix_suffix_shift, _SPANISH_STOPWORDS


class TestIsPrefixSuffixShift:
    """Unit tests for the prefix/suffix shift rejection predicate."""

    def test_leading_char_removal_detected(self):
        """Sostente -> Ostente: suggestion is word with leading 'S' dropped."""
        assert _is_prefix_suffix_shift("Sostente", "Ostente")

    def test_trailing_char_removal_detected(self):
        """Cantamos -> Cantamo: suggestion is word with trailing 's' dropped."""
        assert _is_prefix_suffix_shift("Cantamos", "Cantamo")

    def test_suggestion_longer_than_word(self):
        """Word is a prefix of suggestion (chars appended at end)."""
        assert _is_prefix_suffix_shift("Cantar", "Cantara")

    def test_suggestion_leading_extension(self):
        """Suggestion is word with a leading char prepended."""
        assert _is_prefix_suffix_shift("Ostente", "Sostente")

    def test_internal_change_not_caught(self):
        """creaturas -> criaturas: single internal vowel swap, not prefix/suffix shift."""
        assert not _is_prefix_suffix_shift("creaturas", "criaturas")

    def test_internal_change_two_chars(self):
        """detenimos -> detenemos: internal vowel change, not prefix/suffix shift."""
        assert not _is_prefix_suffix_shift("detenimos", "detenemos")

    def test_same_word_not_shift(self):
        """Identical words are not a shift."""
        assert not _is_prefix_suffix_shift("Hola", "Hola")

    def test_large_length_difference_not_shift(self):
        """Difference > 2 chars is not caught by this rule (other filters apply)."""
        assert not _is_prefix_suffix_shift("Hola", "Holanda")

    def test_case_insensitive(self):
        """Case differences don't affect the comparison."""
        assert _is_prefix_suffix_shift("SOSTENTE", "ostente")
        assert not _is_prefix_suffix_shift("CREATURAS", "criaturas")


def test_spanish_stopwords_filter():
    assert 'de' in _SPANISH_STOPWORDS
    assert 'el' in _SPANISH_STOPWORDS
    assert 'no' in _SPANISH_STOPWORDS
    assert 'damn' not in _SPANISH_STOPWORDS  # must NOT be filtered


def test_hunspell_timeout_constant():
    from translation.spell_check import _HUNSPELL_TIMEOUT
    assert _HUNSPELL_TIMEOUT == 120
