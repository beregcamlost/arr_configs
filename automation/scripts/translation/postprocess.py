"""Post-processing pipeline for Ollama subtitle translations."""

import re
import logging
from difflib import SequenceMatcher
from typing import List

log = logging.getLogger(__name__)

_ENGLISH_PASSLIST = frozenset({
    "ok", "okay", "hey", "man", "baby", "cool", "yeah", "yes", "no",
    "shit", "fuck", "fuckin", "fucking", "damn", "hell", "god",
    "everyone", "everything", "something", "someone", "nothing",
    "the", "and", "but", "for", "not", "you", "all", "can", "had",
    "her", "was", "one", "our", "out", "are", "has", "his", "how",
    "its", "let", "may", "new", "now", "old", "see", "way", "who",
    "did", "get", "got", "him", "just", "come", "could", "good",
    "know", "like", "look", "make", "over", "such", "take", "than",
    "them", "then", "very", "when", "what", "with", "have", "from",
    "they", "been", "said", "each", "will", "into", "about",
    "after", "back", "down", "more", "only", "some", "that", "these",
    "this", "were", "your", "which",
    # Proper nouns / brands
    "youtuber", "youtube", "instagram", "tiktok", "twitter", "facebook",
    "netflix", "spotify", "amazon", "google", "twitch", "reddit", "discord",
    "whatsapp", "telegram", "zoom", "microsoft", "apple", "samsung", "sony",
    "nintendo", "playstation", "xbox",
    # Informal/slang English that survives translation
    "bastard", "bitch", "asshole", "crap", "dude", "guys", "guy", "bro", "sis",
    "lol", "omg", "wtf", "btw", "bruh", "nah", "yep", "yup", "nope", "eh",
    "huh", "wow", "whoa", "ouch", "oops", "ugh", "shhh", "psst",
    "mom", "dad", "mama", "papa", "bye",
    # Common English verbs/adjectives that tend to leak through translation
    "please", "thanks", "welcome", "sorry", "alright", "right", "wrong",
    "nice", "sweet", "awesome", "fine", "hello", "hi",
})

SPANISH_PATTERN_FIXES = [
    (re.compile(r'\bMi madre\b'), 'Tu madre'),
    (re.compile(r'\bMi padre\b'), 'Tu padre'),
    (re.compile(r'\bmi madre\b'), 'tu madre'),
    (re.compile(r'\bmi padre\b'), 'tu padre'),
]

_DASH_FIX_RE = re.compile(r'(\w)\.\-\s*')


def apply_pattern_fixes(text: str, target_lang: str) -> str:
    if target_lang.lower() not in ("spanish", "español"):
        return text
    result = text
    for pattern, replacement in SPANISH_PATTERN_FIXES:
        result = pattern.sub(replacement, result)
    result = _DASH_FIX_RE.sub(r'\1. - ', result)
    return result


def apply_hunspell(texts: List[str], source_texts: List[str]) -> List[str]:
    from translation.spell_check import validate_translated_cues

    issues = validate_translated_cues(texts, source_texts)
    if not issues:
        return texts

    result = list(texts)
    for issue in issues:
        idx = issue["index"]
        corrected = result[idx]
        for bad in issue["bad_words"]:
            word = bad["word"]
            if word.lower() in _ENGLISH_PASSLIST:
                log.debug("Skipping English word '%s' in line %d", word, idx)
                continue
            if not bad["suggestions"]:
                continue
            suggestion = bad["suggestions"][0]
            if ' ' in suggestion:
                log.debug("Rejecting '%s' -> '%s' (contains space) in line %d", word, suggestion, idx)
                continue
            ratio = SequenceMatcher(None, word.lower(), suggestion.lower()).ratio()
            if ratio < 0.80:
                log.debug("Rejecting '%s' -> '%s' (similarity %.2f) in line %d", word, suggestion, ratio, idx)
                continue
            corrected = re.sub(r'\b' + re.escape(word) + r'\b', suggestion, corrected)
            log.info("Fixed '%s' -> '%s' (similarity %.2f) in line %d", word, suggestion, ratio, idx)
        result[idx] = corrected
    return result


def postprocess_translations(
    translated_texts: List[str],
    source_texts: List[str],
    target_lang: str,
) -> List[str]:
    result = [apply_pattern_fixes(t, target_lang) for t in translated_texts]
    result = apply_hunspell(result, source_texts)
    return result
