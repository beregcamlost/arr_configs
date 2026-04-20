"""Ollama client for local subtitle translation."""

import json
import logging
import re
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from translation.srt_parser import Cue, batch_cues
from translation.prompt_utils import build_prompt, parse_response, build_system_prompt, build_few_shot

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 400
DEFAULT_MODEL = "phi4-mini-subs"
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_WORKERS = 2


class OllamaUnavailable(Exception):
    """Ollama server is not reachable or returned an error."""


def _call_ollama(base_url: str, model: str, messages: list,
                 timeout: int = DEFAULT_TIMEOUT) -> str:
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": 0,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        content_parts = []
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line.decode("utf-8"))
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    content_parts.append(delta)
                if chunk.get("done"):
                    break
        return "".join(content_parts)
    except urllib.error.URLError as e:
        raise OllamaUnavailable(f"Ollama unreachable: {e}") from e
    except (KeyError, json.JSONDecodeError) as e:
        raise OllamaUnavailable(f"Ollama bad response: {e}") from e


def _translate_batch(
    base_url: str,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    system_prompt_baked: bool = False,
) -> List[str]:
    user_prompt = build_prompt(cues, source_lang, target_lang)
    if system_prompt_baked:
        messages = [{"role": "user", "content": user_prompt}]
    else:
        system_prompt = build_system_prompt(source_lang, target_lang)
        few_shot = build_few_shot(source_lang, target_lang)
        messages = [
            {"role": "system", "content": system_prompt},
            *few_shot,
            {"role": "user", "content": user_prompt},
        ]
    response_text = _call_ollama(base_url, model, messages, timeout)
    return parse_response(response_text, len(cues))


_EMPTY_RE = re.compile(r'^[\s\.\-\—\–\…\"\'\`]*$')


def _is_effectively_empty(text: str) -> bool:
    """Return True if text is empty or just punctuation/ellipsis."""
    return not text or not text.strip() or bool(_EMPTY_RE.match(text))


def _apply_corrections(target_lang: str, source_texts: List[str], translated_texts: List[str], base_url: str) -> List[str]:
    from translation.postprocess import postprocess_translations
    import os
    proofread_enabled = os.environ.get("TRANSLATION_PROOFREAD_ENABLED", "1") == "1"
    return postprocess_translations(
        translated_texts, source_texts, target_lang,
        proofread_base_url=base_url, proofread_enabled=proofread_enabled,
    )


def _retry_empty_cue(
    base_url: str,
    cue: Cue,
    source_lang: str,
    target_lang: str,
    model: str,
    timeout: int,
    max_attempts: int = 2,
) -> str:
    """Retry a single cue that came back empty. Returns translation or empty string on failure."""
    for attempt in range(max_attempts):
        try:
            result = _translate_batch(
                base_url=base_url,
                cues=[cue],
                source_lang=source_lang,
                target_lang=target_lang,
                model=model,
                timeout=timeout,
                system_prompt_baked=True,
            )
            if result and not _is_effectively_empty(result[0]):
                log.info("Retry %d succeeded for cue %s: '%s' -> '%s'",
                         attempt + 1, cue.index, cue.text[:40], result[0][:40])
                return result[0]
        except Exception as e:
            log.warning("Retry %d failed for cue %s: %s", attempt + 1, cue.index, e)
    log.warning("All retries exhausted for cue %s, returning empty", cue.index)
    return ""


def translate_srt_cues(
    base_url: str,
    cues: List[Cue],
    source_lang: str,
    target_lang: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_MODEL,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> Tuple[List[Cue], int, Optional[int]]:
    if not cues:
        return [], 0, None

    all_source_texts = [c.text for c in cues]
    batches = list(batch_cues(cues, batch_size))
    total_chars = sum(sum(len(c.text) for c in batch) for batch in batches)

    if len(batches) == 1:
        batch = batches[0]
        translated_texts = _translate_batch(base_url, batch, source_lang, target_lang, model, system_prompt_baked=True)
        for j, (cue, text) in enumerate(zip(batch, translated_texts)):
            if _is_effectively_empty(text):
                retried = _retry_empty_cue(base_url, cue, source_lang, target_lang, model, DEFAULT_TIMEOUT)
                if retried:
                    translated_texts[j] = retried
        translated_texts = _apply_corrections(target_lang, all_source_texts, translated_texts, base_url)
        return [
            Cue(index=cue.index, start=cue.start, end=cue.end, text=text)
            for cue, text in zip(batch, translated_texts)
        ], total_chars, None

    futures_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, batch in enumerate(batches):
            future = executor.submit(_translate_batch, base_url, batch, source_lang, target_lang, model, system_prompt_baked=True)
            futures_map[i] = (future, batch)

    results = []
    for i in range(len(batches)):
        future, batch = futures_map[i]
        translated_texts = future.result()
        for j, (cue, text) in enumerate(zip(batch, translated_texts)):
            if _is_effectively_empty(text):
                retried = _retry_empty_cue(base_url, cue, source_lang, target_lang, model, DEFAULT_TIMEOUT)
                if retried:
                    translated_texts[j] = retried
        translated_texts = _apply_corrections(target_lang, all_source_texts, translated_texts, base_url)
        results.append((batch, translated_texts))

    translated_cues = []
    for batch, translated_texts in results:
        for cue, text in zip(batch, translated_texts):
            translated_cues.append(Cue(index=cue.index, start=cue.start, end=cue.end, text=text))

    return translated_cues, total_chars, None
