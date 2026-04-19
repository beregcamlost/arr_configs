"""Ollama client for local subtitle translation."""

import json
import logging
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from translation.srt_parser import Cue, batch_cues
from translation.prompt_utils import build_prompt, parse_response, build_system_prompt, build_few_shot

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 1_500
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
        "stream": False,
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["message"]["content"]
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


def _apply_corrections(source_lang, target_lang, source_cues, translated_texts):
    from translation.postprocess import postprocess_translations
    source_texts = [c.text for c in source_cues]
    return postprocess_translations(translated_texts, source_texts, target_lang)


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

    batches = list(batch_cues(cues, batch_size))
    total_chars = sum(sum(len(c.text) for c in batch) for batch in batches)

    if len(batches) == 1:
        batch = batches[0]
        translated_texts = _translate_batch(base_url, batch, source_lang, target_lang, model, system_prompt_baked=True)
        translated_texts = _apply_corrections(source_lang, target_lang, batch, translated_texts)
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
        translated_texts = _apply_corrections(source_lang, target_lang, batch, translated_texts)
        results.append((batch, translated_texts))

    translated_cues = []
    for batch, translated_texts in results:
        for cue, text in zip(batch, translated_texts):
            translated_cues.append(Cue(index=cue.index, start=cue.start, end=cue.end, text=text))

    return translated_cues, total_chars, None
