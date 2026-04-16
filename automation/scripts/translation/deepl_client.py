"""DeepL API client for subtitle translation with multi-key failover rotation."""

import logging
import time
from typing import List, Tuple

import deepl

from translation.srt_parser import Cue, batch_cues

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 4000  # chars per batch (~4KB)

# Session-scoped set of exhausted API keys
_exhausted_keys: set = set()

# Cached Translator instances, keyed by api_key
_translator_cache: dict = {}


class DeeplKeysExhausted(Exception):
    """All DeepL API keys have exhausted their quota or are disabled."""


def reset_exhausted_keys():
    """Reset exhausted keys and translator cache (call at session start)."""
    _exhausted_keys.clear()
    _translator_cache.clear()


def _get_translator(api_key: str) -> deepl.Translator:
    """Get or create a cached Translator instance for the given key."""
    if api_key not in _translator_cache:
        _translator_cache[api_key] = deepl.Translator(api_key)
    return _translator_cache[api_key]


def get_usage(api_key: str) -> dict:
    """Get current API usage stats for a single key."""
    translator = _get_translator(api_key)
    usage = translator.get_usage()
    return {
        "character_count": usage.character.count if usage.character else 0,
        "character_limit": usage.character.limit if usage.character else 0,
    }


def translate_texts(
    api_keys: list,
    texts: List[str],
    source_lang: str,
    target_lang: str,
) -> Tuple[List[str], int]:
    """Translate a list of texts via DeepL API with key failover.

    Rotates through api_keys on AuthorizationException or QuotaExceededException.
    Returns (translated_strings, key_index) where key_index is the 0-based
    position within api_keys of the key that succeeded.
    Raises DeeplKeysExhausted if all keys fail.
    """
    if not texts:
        return [], None

    for pos, api_key in enumerate(api_keys):
        if api_key in _exhausted_keys:
            continue

        translator = _get_translator(api_key)
        key_label = f"{api_key[:6]}...{api_key[-4:]}"

        for attempt in range(2):
            try:
                results = translator.translate_text(
                    texts,
                    source_lang=source_lang,
                    target_lang=target_lang,
                )
                return [r.text for r in results], pos
            except deepl.AuthorizationException:
                log.warning("DeepL key %s disabled/unauthorized, skipping", key_label)
                _exhausted_keys.add(api_key)
                break
            except deepl.QuotaExceededException:
                log.warning("DeepL key %s quota exceeded, skipping", key_label)
                _exhausted_keys.add(api_key)
                break
            except deepl.TooManyRequestsException:
                if attempt == 0:
                    log.warning("DeepL key %s rate limited, retrying in 4s...", key_label)
                    time.sleep(4)
                    continue
                log.warning("DeepL key %s rate limit retry failed, skipping", key_label)
                _exhausted_keys.add(api_key)
                break
            except deepl.DeepLException as e:
                # Transient error — log and try next key without marking exhausted
                log.warning("DeepL key %s transient error: %s", key_label, e)
                break

    raise DeeplKeysExhausted("All DeepL API keys exhausted")


def translate_srt_cues(
    api_keys: list,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[List[Cue], int, int]:
    """Translate SRT cues via DeepL, batching to stay under size limits.

    Returns (translated_cues, total_chars_used, key_index) where key_index is
    the 0-based position within api_keys of the last key used (batches may
    rotate keys; tracking last is sufficient for budget accounting).
    Raises DeeplKeysExhausted if all keys are exhausted.
    """
    if not cues:
        return [], 0, None

    total_chars = 0
    translated_cues = []
    last_key_index = None

    for batch in batch_cues(cues, batch_size):
        texts = [c.text for c in batch]
        total_chars += sum(len(t) for t in texts)
        translated_texts, last_key_index = translate_texts(
            api_keys, texts, source_lang, target_lang
        )
        for cue, translated_text in zip(batch, translated_texts):
            translated_cues.append(Cue(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                text=translated_text,
            ))

    return translated_cues, total_chars, last_key_index
