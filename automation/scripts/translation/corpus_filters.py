"""Shared quality-filter helpers for corpus mining scripts.

Used by mine_opensubtitles.py and mine_wmt_spanish.py to keep filter logic DRY.
"""

from __future__ import annotations


def quality_filter_pair(en: str, es: str) -> bool:
    """Return True if an (English, Spanish) pair passes all quality checks.

    Filters applied:
    - Strip whitespace, drop empty
    - Both en & es must be >= 15 chars
    - len_ratio len(es)/len(en) between 0.3 and 3.0
    - Reject if either contains URL or HTML-like content
    - Reject if non-alpha ratio > 20%
    - Reject if Spanish line has >40% ASCII-only chars (likely not real Spanish)
    """
    en = en.strip()
    es = es.strip()

    if not en or not es:
        return False

    if len(en) < 15 or len(es) < 15:
        return False

    ratio = len(es) / len(en)
    if ratio < 0.3 or ratio > 3.0:
        return False

    # URL / HTML / special structure rejection
    _BAD_TOKENS = ("http://", "https://", "www.", "<", ">", "{", "}", "\n")
    for tok in _BAD_TOKENS:
        if tok in en or tok in es:
            return False

    # Non-alpha ratio > 20%
    def _non_alpha_ratio(s: str) -> float:
        if not s:
            return 0.0
        non_alpha = sum(1 for c in s if not c.isalpha() and not c.isspace())
        return non_alpha / len(s)

    if _non_alpha_ratio(en) > 0.20 or _non_alpha_ratio(es) > 0.20:
        return False

    # Spanish line should have some non-ASCII (accented/ñ/ü) characters when long enough.
    # Very short strings (< 40 chars) are allowed to be all-ASCII (many common Spanish
    # phrases use only plain letters).  For longer strings, require at least one
    # accented character — pure-ASCII long lines are likely English mislabeled as Spanish.
    _SPANISH_ACCENTS = frozenset("áéíóúñüÁÉÍÓÚÑÜ¿¡")
    if len(es) >= 40:
        has_accent = any(c in _SPANISH_ACCENTS for c in es)
        if not has_accent:
            return False

    return True
