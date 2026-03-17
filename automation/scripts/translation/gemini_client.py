"""Gemini API client for subtitle translation with multi-key rotation."""

import logging
import re
import time
from typing import Callable, List, Tuple

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from translation.srt_parser import Cue, batch_cues

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 3_000  # chars per batch — large batches cause None responses
DEFAULT_MODEL = "gemini-2.5-pro"

# Session-scoped set of exhausted API keys
_exhausted_keys: set = set()

# Cached model instances per (api_key, model, source_lang, target_lang)
_model_cache: dict = {}

# Pre-compiled regex for stripping numbered prefixes from response lines
_NUMBER_PREFIX_RE = re.compile(r"^\d+[\s]*[:.)\-]\s*")


class GeminiQuotaExhausted(Exception):
    """All Gemini API keys have exhausted their quota."""


def reset_exhausted_keys():
    """Reset the set of exhausted keys and model cache (call at session start)."""
    _exhausted_keys.clear()
    _model_cache.clear()


def has_available_keys(api_keys: list) -> bool:
    """Check if any API key is still available."""
    return any(k not in _exhausted_keys for k in api_keys)


def _get_model(api_key: str, source_lang: str, target_lang: str,
               model: str = DEFAULT_MODEL):
    """Get or create a cached GenerativeModel for the given key and languages."""
    cache_key = (api_key, model, source_lang, target_lang)
    if cache_key not in _model_cache:
        genai.configure(api_key=api_key)
        _model_cache[cache_key] = genai.GenerativeModel(
            model_name=model,
            system_instruction=(
                f"You are a subtitle translator. Translate each numbered line "
                f"from {source_lang} to {target_lang}. Return ONLY the translations, "
                f"one per line, numbered to match. Preserve line count exactly."
            ),
        )
    return _model_cache[cache_key]


def _build_prompt(cues: List[Cue], source_lang: str, target_lang: str) -> str:
    """Build the numbered translation prompt from cues."""
    lines = []
    for i, cue in enumerate(cues, 1):
        # Encode multi-line cues as <br> for transport
        text = cue.text.replace("\n", "<br>")
        lines.append(f"{i}: {text}")
    header = f"Translate from {source_lang} to {target_lang}:\n"
    return header + "\n".join(lines)


def _parse_response(response_text: str, expected_count: int) -> List[str]:
    """Parse numbered response lines back into text strings."""
    lines = response_text.strip().split("\n")
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Strip leading number + colon/dot/paren prefix
        cleaned = _NUMBER_PREFIX_RE.sub("", line)
        # Decode <br> back to newlines
        cleaned = cleaned.replace("<br>", "\n")
        results.append(cleaned)
    # If we got fewer results than expected, pad with empty strings
    while len(results) < expected_count:
        results.append("")
    # If we got more, truncate
    return results[:expected_count]


def _translate_batch(
    api_keys: list,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    model: str = DEFAULT_MODEL,
) -> List[str]:
    """Translate a batch of cues, rotating keys on quota exhaustion."""
    prompt = _build_prompt(cues, source_lang, target_lang)

    for api_key in api_keys:
        if api_key in _exhausted_keys:
            continue

        client = _get_model(api_key, source_lang, target_lang, model)

        try:
            response = client.generate_content(prompt)
            if response.text is None:
                log.warning("Gemini returned empty response (safety filter?), trying next key")
                _exhausted_keys.add(api_key)
                continue
            return _parse_response(response.text, len(cues))
        except ResourceExhausted as e:
            error_msg = str(e).lower()
            is_rate_limit = "per_minute" in error_msg or "rate" in error_msg
            if is_rate_limit:
                # Transient RPM limit — retry once after short sleep
                log.warning("Gemini RPM rate limit hit, retrying in 4s...")
                time.sleep(4)
                try:
                    response = client.generate_content(prompt)
                    return _parse_response(response.text, len(cues))
                except ResourceExhausted:
                    log.warning("Gemini key %s...%s exhausted (RPM retry failed)",
                                api_key[:8], api_key[-4:])
                    _exhausted_keys.add(api_key)
                    continue
            else:
                # Daily quota or other quota limit
                log.warning("Gemini key %s...%s exhausted (daily quota)",
                            api_key[:8], api_key[-4:])
                _exhausted_keys.add(api_key)
                continue

    raise GeminiQuotaExhausted("All Gemini API keys have exhausted their quota")


def translate_srt_cues(
    api_keys: list,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_MODEL,
) -> Tuple[List[Cue], int]:
    """Translate SRT cues via Gemini, batching to stay under size limits.

    Returns (translated_cues, total_chars_used).
    Raises GeminiQuotaExhausted if all keys are exhausted.
    """
    if not cues:
        return [], 0

    total_chars = 0
    translated_cues = []

    for batch in batch_cues(cues, batch_size):
        texts = [c.text for c in batch]
        total_chars += sum(len(t) for t in texts)
        translated_texts = _translate_batch(
            api_keys, batch, source_lang, target_lang, model
        )
        for cue, translated_text in zip(batch, translated_texts):
            translated_cues.append(Cue(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                text=translated_text,
            ))

    return translated_cues, total_chars
