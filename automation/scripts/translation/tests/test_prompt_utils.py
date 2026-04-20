"""Tests for prompt_utils — prompt building, response parsing, and note stripping."""

import pytest

from translation.prompt_utils import _strip_translator_notes, parse_response


class TestStripTranslatorNotes:
    def test_strip_full_parenthetical(self):
        assert _strip_translator_notes("(translation incomplete)") == ""
        assert _strip_translator_notes("[translator note: skip]") == ""
        assert _strip_translator_notes("   (inaudible)   ") == ""

    def test_strip_trailing_note(self):
        assert _strip_translator_notes("Yo... (translation incomplete)") == "Yo..."
        assert _strip_translator_notes("Hola [audio unclear]") == "Hola"
        assert _strip_translator_notes("Adiós (note: shortened)") == "Adiós"

    def test_strip_trailing_note_variants(self):
        assert _strip_translator_notes("Okay (unable to translate)") == "Okay"
        assert _strip_translator_notes("Sure (untranslated)") == "Sure"
        assert _strip_translator_notes("Hmm (no context)") == "Hmm"
        assert _strip_translator_notes("Hmm (no_context)") == "Hmm"
        assert _strip_translator_notes("Hmm (no-context)") == "Hmm"
        assert _strip_translator_notes("Shh [inaudible]") == "Shh"
        assert _strip_translator_notes("Sí (n/a)") == "Sí"

    def test_strip_case_insensitive(self):
        assert _strip_translator_notes("Yo (TRANSLATION INCOMPLETE)") == "Yo"
        assert _strip_translator_notes("Hey [Audio Unclear]") == "Hey"
        assert _strip_translator_notes("   (INAUDIBLE)   ") == ""

    def test_preserves_real_dialogue(self):
        # Regular dialogue with parens — don't touch
        assert _strip_translator_notes("Is this (really) happening?") == "Is this (really) happening?"
        # SDH sound cue at start — stay
        assert _strip_translator_notes("[laughing] I love it!") == "[laughing] I love it!"
        # Middle parenthetical stage direction — stay
        assert _strip_translator_notes("Yeah (sighs), okay.") == "Yeah (sighs), okay."
        # Parenthetical not at end — stay
        assert _strip_translator_notes("(Un momento) espera.") == "(Un momento) espera."

    def test_empty_and_plain(self):
        assert _strip_translator_notes("") == ""
        assert _strip_translator_notes("Hola") == "Hola"
        assert _strip_translator_notes("   ") == "   "


class TestParseResponseStripsNotes:
    def test_parse_response_strips_trailing_note(self):
        resp = "1: Hola (translation incomplete)\n2: Adiós\n3: (inaudible)"
        texts = parse_response(resp, 3)
        assert texts[0] == "Hola"
        assert texts[1] == "Adiós"
        assert texts[2] == ""  # full-parenthetical → empty string; empty-cue retry handles it

    def test_parse_response_leaves_real_dialogue_intact(self):
        resp = "1: Is this (really) happening?\n2: [laughing] I love it!\n3: Yeah (sighs), okay."
        texts = parse_response(resp, 3)
        assert texts[0] == "Is this (really) happening?"
        assert texts[1] == "[laughing] I love it!"
        assert texts[2] == "Yeah (sighs), okay."

    def test_parse_response_pads_missing(self):
        resp = "1: Hola\n2: Adiós"
        texts = parse_response(resp, 4)
        assert len(texts) == 4
        assert texts[2] == ""
        assert texts[3] == ""

    def test_parse_response_truncates_extra(self):
        resp = "1: Hola\n2: Adiós\n3: Buenos días"
        texts = parse_response(resp, 2)
        assert len(texts) == 2

    def test_rooster_fighter_regression(self):
        """Regression: cue 473 of Rooster Fighter S01E06 produced 'Yo... (translaticio incompleta)'."""
        resp = "473: Yo... (translation incomplete)"
        texts = parse_response(resp, 1)
        assert texts[0] == "Yo..."
