"""Local Ollama proofreader for Spanish subtitle corrections."""

import json
import os
import logging
import re
import urllib.request
import urllib.error
from difflib import SequenceMatcher
from typing import List

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "subtitler:v2")

# Strip leading label prefixes the model might echo back
_LABEL_RE = re.compile(r'^(?:corrected|spanish|traducción)[:\s]+', re.IGNORECASE)


def _parse_proofread_response(raw: str) -> str:
    """Strip whitespace, surrounding quotes, and label prefixes from model output."""
    text = raw.strip()
    # Remove surrounding quotes (single or double)
    if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
        text = text[1:-1].strip()
    # Remove leading label if model echoed "Corrected: ..." or "Spanish: ..."
    text = _LABEL_RE.sub("", text).strip()
    return text


def _is_safe_correction(original: str, corrected: str, threshold: float = 0.3) -> bool:
    """Return True if corrected text is similar enough to original to be safe.

    SequenceMatcher ratio < threshold means the proofreader hallucinated
    something totally unrelated — reject the correction and keep original.
    """
    if not corrected:
        return False
    ratio = SequenceMatcher(None, original.lower(), corrected.lower()).ratio()
    return ratio >= threshold


def proofread_cues(
    source_texts: List[str],
    translated_texts: List[str],
    indices: List[int],
    base_url: str,
    model: str = DEFAULT_MODEL,
    timeout: int = 120,
) -> List[str]:
    """Proofread specific flagged cues via local Ollama model.

    Returns a NEW list where flagged indices are replaced with proofread text.
    Safety: if the corrected text has SequenceMatcher ratio < 0.3 vs the original
    translated text, reject the correction and keep original (prevents hallucination).
    If Ollama is unreachable, log warning and return translated_texts unchanged.
    """
    if not indices:
        return list(translated_texts)

    result = list(translated_texts)
    url = f"{base_url.rstrip('/')}/api/generate"

    for idx in indices:
        if idx < 0 or idx >= len(translated_texts):
            log.warning("proofread_cues: index %d out of range (len=%d)", idx, len(translated_texts))
            continue

        en = source_texts[idx] if idx < len(source_texts) else ""
        es = translated_texts[idx]

        prompt = f"English: {en}\nSpanish: {es}\nCorrected:"
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
                raw = data.get("response", "")
        except urllib.error.URLError as e:
            log.warning("proofread_cues: Ollama unreachable: %s — skipping proofreading", e)
            return result
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("proofread_cues: bad response from Ollama: %s — skipping cue %d", e, idx)
            continue

        corrected = _parse_proofread_response(raw)

        if not _is_safe_correction(es, corrected):
            log.debug(
                "proofread_cues: rejected correction for cue %d "
                "(ratio < 0.3): original=%r corrected=%r",
                idx, es, corrected,
            )
            continue

        if corrected != es:
            log.info(
                "proofread_cues: cue %d: %r -> %r",
                idx, es, corrected,
            )
        result[idx] = corrected

    return result


def is_proofreader_available(base_url: str, model: str = DEFAULT_MODEL) -> bool:
    """Return True if the proofreader model is available on the Ollama server.

    Calls /api/tags and checks if the model name is listed.
    Returns False on any network or parse error.
    """
    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        models = data.get("models", [])
        # Match by prefix so "phi4-mini-proofread" matches "phi4-mini-proofread:latest"
        model_base = model.split(":")[0]
        for entry in models:
            name = entry.get("name", "")
            if name == model or name.startswith(model_base + ":"):
                return True
        return False
    except Exception as e:
        log.warning("is_proofreader_available: could not reach %s: %s", url, e)
        return False
