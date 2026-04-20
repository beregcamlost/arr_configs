"""Post-processing pipeline for Ollama subtitle translations."""

import re
import logging
from difflib import SequenceMatcher
from typing import List, Optional

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

_SURVIVOR_TOKEN_RE = re.compile(r"[A-Za-záéíóúüñ']+")

_SPANISH_STOPWORDS = frozenset({
    "de","el","la","los","las","no","en","es","a","al","se","me","te","lo","le",
    "con","para","un","una","y","o","ha","ya","si","tan","mi","su","tu","era",
    "eras","ser","soy","son","sea","fue","fui","fuera","del","por","que","qué",
    "como","cómo","ni","nos","vos","ti","ella","ellas","ellos","él","muy","bien",
    "bueno","mala","malo","grande","nuevo","viejo","solo","sólo","todo","toda",
    "todos","todas","cada","di","he","hay","hoy","voy","vas","va","vamos","ven",
    "ir","estar","ave","corona","presa","marinas","viva","favor","pronto",
    "persona","personas","amigos","hombre","gran","sake","pasado","ayer",
    "allí","aquí","esto","ese","esa","este","esta","eso","mas","más","pero",
    "dos","tres","sí",
})


def _is_prefix_suffix_shift(word: str, suggestion: str) -> bool:
    """Return True if suggestion is the word with 1-2 leading/trailing chars removed (or added).

    Blocks semantic-shift corrections like Sostente->Ostente (drops leading S)
    or Cantamos->Cantamo (drops trailing s). These are usually worse than the original.
    Internal typo fixes (creaturas->criaturas) are not caught by this rule.
    """
    w = word.lower()
    s = suggestion.lower()
    if w == s:
        return False
    if abs(len(w) - len(s)) > 2:
        return False
    return w.startswith(s) or w.endswith(s) or s.startswith(w) or s.endswith(w)


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
            if ratio < 0.90:
                log.debug("Rejecting '%s' -> '%s' (similarity %.2f) in line %d", word, suggestion, ratio, idx)
                continue
            if _is_prefix_suffix_shift(word, suggestion):
                log.debug("Rejecting '%s' -> '%s' (prefix/suffix shift) in line %d", word, suggestion, idx)
                continue
            corrected = re.sub(r'\b' + re.escape(word) + r'\b', suggestion, corrected)
            log.info("Fixed '%s' -> '%s' (similarity %.2f) in line %d", word, suggestion, ratio, idx)
        result[idx] = corrected
    return result


def apply_local_proofreader(
    texts: List[str],
    source_texts: List[str],
    target_lang: str,
    base_url: Optional[str] = None,
    enabled: bool = True,
) -> List[str]:
    """After hunspell, send remaining flagged cues through the local Ollama proofreader.

    'Flagged' = cues that still have hunspell issues after the hunspell pass.
    Only runs for Spanish target. Skipped when disabled or no base_url provided.
    """
    if not enabled or not base_url:
        return texts

    if target_lang.lower() not in ("spanish", "español"):
        return texts

    from translation.spell_check import validate_translated_cues, _detect_english_survivors

    issues = validate_translated_cues(texts, source_texts)
    es_flagged = {issue["index"] for issue in issues}

    # Collect all unique words across all translated texts for English survivor detection
    all_words = list({
        tok.lower()
        for text in texts
        for tok in _SURVIVOR_TOKEN_RE.findall(text)
    })
    english_survivors = _detect_english_survivors(all_words)
    # Filter out Spanish stopwords that hunspell-en wrongly marks as valid English
    actionable_survivors = english_survivors - _SPANISH_STOPWORDS

    # Flag cues that contain English survivor tokens (untranslated English words)
    survivor_flagged: set = set()
    if actionable_survivors:
        for i, text in enumerate(texts):
            tokens = {t.lower() for t in _SURVIVOR_TOKEN_RE.findall(text)}
            if tokens & actionable_survivors:
                survivor_flagged.add(i)

    survivor_only = survivor_flagged - es_flagged
    flagged_indices = sorted(es_flagged | survivor_flagged)

    if not flagged_indices:
        return texts

    log.info(
        "proofreader: %d cues flagged by hunspell-es, %d additional by English survivors (%d actionable)",
        len(es_flagged),
        len(survivor_only),
        len(actionable_survivors),
    )
    log.debug("apply_local_proofreader: %d flagged cue(s): %s", len(flagged_indices), flagged_indices)

    from translation.local_proofreader import proofread_cues
    return proofread_cues(
        source_texts=source_texts,
        translated_texts=texts,
        indices=flagged_indices,
        base_url=base_url,
    )


def postprocess_translations(
    translated_texts: List[str],
    source_texts: List[str],
    target_lang: str,
    proofread_base_url: Optional[str] = None,
    proofread_enabled: bool = True,
) -> List[str]:
    result = [apply_pattern_fixes(t, target_lang) for t in translated_texts]
    result = apply_hunspell(result, source_texts)
    result = apply_local_proofreader(result, source_texts, target_lang, proofread_base_url, proofread_enabled)
    return result
