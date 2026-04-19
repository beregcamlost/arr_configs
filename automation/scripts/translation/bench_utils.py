"""Shared utilities for Ollama translation benchmark scripts."""

import difflib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import List, Tuple

from translation.srt_parser import parse_srt, Cue

DEFAULT_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://172.20.77.2:11434")
DEFAULT_CUES = 20


def load_cues(path: str, start: int, n: int) -> List[Cue]:
    """Parse an SRT file and return a slice of cues."""
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    cues = parse_srt(content)
    return cues[start:start + n]


def similarity(a: str, b: str) -> float:
    """Return SequenceMatcher similarity ratio between two strings."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def score_against_reference(
    translated: List[str],
    reference: List[str],
) -> Tuple[float, int]:
    """Score translated texts against a reference.

    Returns (avg_similarity, exact_match_count).
    """
    ratios = [similarity(t, r) for t, r in zip(translated, reference)]
    avg = sum(ratios) / len(ratios) if ratios else 0.0
    exact = sum(1 for t, r in zip(translated, reference) if t == r)
    return avg, exact


def unload_model(base_url: str, model: str) -> None:
    """POST keep_alive=0 to evict the model from memory, then sleep 5s."""
    url = f"{base_url.rstrip('/')}/api/generate"
    data = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        time.sleep(5)
    except Exception:
        pass


def tok_per_sec(texts: List[str], elapsed: float) -> float:
    """Return chars/sec as a proxy for tokens/sec."""
    if elapsed <= 0:
        return 0.0
    return sum(len(t) for t in texts) / elapsed
