"""Post-processing pipeline for Ollama subtitle translations."""

import re
import logging
from typing import List

log = logging.getLogger(__name__)

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
            if bad["suggestions"]:
                corrected = corrected.replace(bad["word"], bad["suggestions"][0])
                log.info("Fixed '%s' -> '%s' in line %d", bad["word"], bad["suggestions"][0], idx)
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
