"""DeepL API client for subtitle translation."""

import logging
from typing import List, Tuple

import deepl

from translation.srt_parser import Cue

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 4000  # chars per batch (~4KB)


def create_translator(api_key: str) -> deepl.Translator:
    """Create a DeepL Translator instance."""
    return deepl.Translator(api_key)


def get_usage(translator: deepl.Translator) -> dict:
    """Get current API usage stats."""
    usage = translator.get_usage()
    return {
        "character_count": usage.character.count if usage.character else 0,
        "character_limit": usage.character.limit if usage.character else 0,
    }


def translate_texts(
    translator: deepl.Translator,
    texts: List[str],
    source_lang: str,
    target_lang: str,
) -> List[str]:
    """Translate a list of texts via DeepL API. Returns translated strings."""
    if not texts:
        return []
    results = translator.translate_text(
        texts,
        source_lang=source_lang,
        target_lang=target_lang,
    )
    return [r.text for r in results]


def translate_srt_cues(
    translator: deepl.Translator,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[List[Cue], int]:
    """Translate SRT cues via DeepL, batching to stay under size limits.

    Returns (translated_cues, total_chars_used).
    """
    if not cues:
        return [], 0

    # Build batches by character count
    batches = []
    current_batch = []
    current_size = 0
    for cue in cues:
        text_len = len(cue.text)
        if current_batch and current_size + text_len > batch_size:
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(cue)
        current_size += text_len
    if current_batch:
        batches.append(current_batch)

    total_chars = 0
    translated_cues = []

    for batch in batches:
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
