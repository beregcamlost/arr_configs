"""Hunspell-based spell checking for translated subtitles."""

import logging
import re
import subprocess
from typing import List, Optional

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)
_ASS_BLOCK_RE = re.compile(r"\{[^}]*\}")
_ASS_TAG_RE = re.compile(r"\\(?:fn|fb|fi|fe)[^\\]*|\\[a-z]+\S*", re.IGNORECASE)
_HUNSPELL_DICT = "es_ES"
_HUNSPELL_DICT_EN = "en_US"
_HUNSPELL_TIMEOUT = 120  # seconds — applies to both en_US and es_ES batch calls


def _strip_ass_tags(text: str) -> str:
    text = _ASS_BLOCK_RE.sub(" ", text)
    return _ASS_TAG_RE.sub(" ", text)


def _detect_english_survivors(words: List[str]) -> set:
    """Check which words are valid English via hunspell -d en_US.

    Returns a set of lowercased words that hunspell-en considers correctly spelled.
    These should be skipped by the Spanish spell checker — they are English words
    the LLM left untranslated, not Spanish typos.

    Uses a single hunspell subprocess (batch mode). Returns empty set on error.
    """
    if not words:
        return set()

    # Lowercase at entry so the returned set is always lowercase-keyed
    lowered = [w.lower() for w in words]

    try:
        input_text = "\n".join(lowered)
        result = subprocess.run(
            ["hunspell", "-d", _HUNSPELL_DICT_EN, "-a"],
            input=input_text, capture_output=True, text=True, timeout=_HUNSPELL_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("hunspell (en_US) unavailable: %s", e)
        return set()

    # hunspell -a output: one header line, then for each input word one response
    # line followed by one blank separator line.
    #   * or + root  → valid
    #   & word ...   → misspelled with suggestions
    #   # word ...   → misspelled, no suggestions
    #   ?            → guesses (treat as misspelled)
    # Input words are NOT echoed — match by position.
    lines = result.stdout.splitlines()
    # Drop the version header (first line)
    response_lines = lines[1:] if lines else []

    survivors: set = set()
    word_idx = 0
    for line in response_lines:
        if word_idx >= len(lowered):
            break
        if line == "":
            # blank separator — advance to next word
            word_idx += 1
            continue
        # Response line for current word
        if line.startswith("*") or line.startswith("+"):
            survivors.add(lowered[word_idx])
        # & / # / ? → misspelled; do not add to survivors

    return survivors


def _run_hunspell_batch(words: List[str]) -> dict:
    """Send a list of unique words to a single hunspell process.

    Returns a dict mapping word -> {"word": str, "suggestions": list[str]}
    for every misspelled word. Correctly-spelled words are absent from the dict.
    """
    try:
        input_text = "\n".join(words)
        result = subprocess.run(
            ["hunspell", "-d", _HUNSPELL_DICT, "-a"],
            input=input_text, capture_output=True, text=True, timeout=_HUNSPELL_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("hunspell unavailable: %s", e)
        return {}

    misspelled = {}
    for line in result.stdout.splitlines():
        if line.startswith("&"):
            # & word count offset: suggestion1, suggestion2, ...
            parts = line.split(":", 1)
            word = line.split()[1]
            suggestions = [s.strip() for s in parts[1].split(",")] if len(parts) > 1 else []
            misspelled[word] = {"word": word, "suggestions": suggestions}
        elif line.startswith("#"):
            # # word offset — no suggestions
            word = line.split()[1]
            misspelled[word] = {"word": word, "suggestions": []}

    return misspelled


def validate_translated_cues(translated_texts: List[str],
                              source_texts: List[str]) -> List[dict]:
    """Validate translated subtitle texts against hunspell.

    Returns list of {"index": int, "text": str, "bad_words": list}
    for lines with misspelled words.
    Skips words that appear in the source text (proper nouns, shared terms).

    Uses a single hunspell subprocess for all cues (batch mode) to avoid
    N+1 process forks.
    """
    source_words = set()
    for text in source_texts:
        source_words.update(w.lower() for w in _WORD_RE.findall(_strip_ass_tags(text)))

    # Collect all unique candidate words across all cues in one pass
    cue_words: List[List[str]] = []
    all_unique_words: set = set()
    for translated in translated_texts:
        clean_text = _strip_ass_tags(translated)
        words = [w for w in _WORD_RE.findall(clean_text) if len(w) > 1 and not w.isupper()]
        cue_words.append(words)
        all_unique_words.update(words)

    if not all_unique_words:
        return []

    # Single hunspell call for every unique word
    misspelled_map = _run_hunspell_batch(sorted(all_unique_words))

    # Identify English survivors (valid en_US words) so we don't flag
    # untranslated English words as Spanish misspellings.
    english_survivors = _detect_english_survivors(sorted(all_unique_words))
    if english_survivors:
        log.debug("spell_check: %d English survivor(s) skipped: %s",
                  len(english_survivors), sorted(english_survivors))

    issues = []
    for i, words in enumerate(cue_words):
        bad = [
            misspelled_map[w]
            for w in words
            if w in misspelled_map
            and w.lower() not in source_words
            and w.lower() not in english_survivors
        ]
        # Deduplicate while preserving order (same word may appear multiple times)
        seen: set = set()
        deduped = []
        for b in bad:
            if b["word"] not in seen:
                seen.add(b["word"])
                deduped.append(b)
        if deduped:
            issues.append({
                "index": i,
                "text": translated_texts[i],
                "bad_words": deduped,
            })

    return issues
