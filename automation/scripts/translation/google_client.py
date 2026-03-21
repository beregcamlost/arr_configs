"""Google Translate client for subtitle translation (fallback provider)."""

import logging
from typing import List, Tuple

from googletrans import Translator

from translation.srt_parser import Cue, batch_cues

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 4000  # chars per batch


def create_translator() -> Translator:
    """Create a Google Translator instance (no API key needed)."""
    return Translator()


def translate_texts(
    translator: Translator,
    texts: List[str],
    source_lang: str,
    target_lang: str,
) -> List[str]:
    """Translate a list of texts via Google Translate. Returns translated strings."""
    if not texts:
        return []
    # Translate one at a time — googletrans batch mode is broken (returns None)
    results = []
    for text in texts:
        try:
            result = translator.translate(text, src=source_lang, dest=target_lang)
            results.append(result.text if result and result.text else text)
        except Exception as e:
            log.warning("Google Translate failed for cue, keeping original: %s", e)
            results.append(text)
    return results


def translate_srt_cues(
    translator: Translator,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[List[Cue], int]:
    """Translate SRT cues via Google Translate, batching to stay under size limits.

    Returns (translated_cues, total_chars_used).
    """
    if not cues:
        return [], 0

    total_chars = 0
    translated_cues = []

    for batch in batch_cues(cues, batch_size):
        texts = [c.text for c in batch]
        total_chars += sum(len(t) for t in texts)
        translated_texts = translate_texts(
            translator, texts, source_lang, target_lang
        )
        for cue, translated_text in zip(batch, translated_texts):
            translated_cues.append(Cue(
                index=cue.index,
                start=cue.start,
                end=cue.end,
                text=translated_text,
            ))

    return translated_cues, total_chars
