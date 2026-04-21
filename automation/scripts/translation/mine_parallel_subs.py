"""Mine aligned (English, Spanish) subtitle pairs for training data.

Walk media roots, find .en.srt + .es.srt sibling pairs, apply strict
alignment and quality filters, deduplicate, and emit newline-delimited JSON.

Usage:
    python3 -m translation.mine_parallel_subs --output /tmp/corpus.jsonl
"""

import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import List, Optional, Tuple

import click

from translation.srt_parser import Cue, parse_srt

# ---------------------------------------------------------------------------
# Default media roots
# ---------------------------------------------------------------------------
DEFAULT_ROOTS = [
    "/APPBOX_DATA/storage/media/tv",
    "/APPBOX_DATA/storage/media/tvanimated",
    "/APPBOX_DATA/storage/media/movies",
]

# Roots that use a TV-style hierarchy (root/Show Name/Season X/...)
_TV_ROOTS = {
    "/APPBOX_DATA/storage/media/tv",
    "/APPBOX_DATA/storage/media/tvanimated",
}
# Roots that use a movies-style hierarchy (root/Movie Name/...)
_MOVIE_ROOTS = {
    "/APPBOX_DATA/storage/media/movies",
}

# ---------------------------------------------------------------------------
# Compiled regexes (module-level for performance in tight loops)
# ---------------------------------------------------------------------------
_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_ASS_TAGS = re.compile(r"\{[^}]+\}")
_RE_WHITESPACE = re.compile(r"[ \t]+")
_RE_PUNCT_ONLY = re.compile(r"^[\W\d_]+$", re.UNICODE)
_RE_MUSIC_ONLY = re.compile(r"^[♪\s]+$|^\s*music\s*$", re.IGNORECASE)
_RE_SOUND_CUE = re.compile(r"^\[.*\]$", re.DOTALL)

_SPANISH_CHARS = frozenset("áéíóúñüÁÉÍÓÚÑÜ")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _ts_to_seconds(ts: str) -> float:
    """Convert SRT timestamp string 'HH:MM:SS,mmm' to float seconds."""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


# ---------------------------------------------------------------------------
# Core pipeline functions
# ---------------------------------------------------------------------------

def extract_series_key(path: Path, roots: List[str]) -> str:
    """Return a stable series/movie key for grouping per-series caps.

    For paths under a known TV root  (tv/, tvanimated/):
        root/Show Name/Season X/file  →  "Show Name"
    For paths under a known movie root (movies/):
        root/Movie Name/file          →  "MOVIE:<Movie Name>"
    For paths outside any known root (e.g. test tmpdir):
        uses the immediate parent directory name as a fallback key.

    The key is used solely for capping, not for output.
    """
    # Resolve to string for prefix comparison
    path_str = str(path)
    for root in roots:
        root_str = root.rstrip("/")
        if path_str.startswith(root_str + "/"):
            # relative parts after the root
            rel = Path(path_str[len(root_str) + 1 :])
            parts = rel.parts  # (Show Name, Season X, file) or (Movie Name, file)
            if not parts:
                break
            series_name = parts[0]
            if root_str in _MOVIE_ROOTS:
                return f"MOVIE:{series_name}"
            else:
                # TV / tvanimated
                return series_name
    # Fallback: use the parent directory name
    return path.parent.name or "UNKNOWN"


def apply_series_cap(
    pairs: List[Tuple[str, str, str]],
    max_per_series: int,
    roots: List[str],
    seed: int = 42,
) -> List[Tuple[str, str, str]]:
    """Cap pairs to max_per_series per series.  Random sample when over limit.

    pairs: list of (english, spanish, source_path)
    max_per_series: maximum pairs per series (0 = no cap, passthrough)
    roots: scan roots used for series key extraction
    seed: random seed for reproducible sampling
    """
    if max_per_series <= 0:
        return pairs

    # Group by series key
    grouped: defaultdict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for item in pairs:
        key = extract_series_key(Path(item[2]), roots)
        grouped[key].append(item)

    rng = random.Random(seed)
    result = []
    for key, items in grouped.items():
        if len(items) > max_per_series:
            items = rng.sample(items, max_per_series)
        result.extend(items)
    return result


def scan_pairs(roots: List[str]) -> List[Tuple[Path, Path]]:
    """Walk roots and return (en_srt, es_srt) Path pairs where both exist.

    Returns a list of (en_path, es_path) tuples.
    """
    pairs = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for en_path in root_path.rglob("*.en.srt"):
            # Derive sibling es path by replacing the .en.srt suffix
            name = en_path.name
            if not name.endswith(".en.srt"):
                continue
            stem = name[: -len(".en.srt")]
            es_path = en_path.parent / f"{stem}.es.srt"
            if es_path.is_file():
                pairs.append((en_path, es_path))
    return pairs


def check_alignment(en_cues: List[Cue], es_cues: List[Cue], strict_ms: int = 200) -> bool:
    """Return True if all cues are count-matched and within strict_ms timing tolerance.

    strict_ms: maximum allowed start/end delta in milliseconds (both must pass).
    """
    if len(en_cues) != len(es_cues):
        return False
    threshold = strict_ms / 1000.0
    for en, es in zip(en_cues, es_cues):
        if abs(_ts_to_seconds(en.start) - _ts_to_seconds(es.start)) >= threshold:
            return False
        if abs(_ts_to_seconds(en.end) - _ts_to_seconds(es.end)) >= threshold:
            return False
    return True


def _strip_and_normalize(text: str) -> str:
    """Strip HTML and ASS tags, collapse inline whitespace, join newlines with space."""
    text = _RE_HTML_TAGS.sub("", text)
    text = _RE_ASS_TAGS.sub("", text)
    # Replace newlines with a space, then collapse runs of spaces/tabs
    text = text.replace("\n", " ")
    text = _RE_WHITESPACE.sub(" ", text)
    return text.strip()


def extract_pairs(en_cues: List[Cue], es_cues: List[Cue]) -> List[Tuple[str, str]]:
    """Normalize and return (english, spanish) text pairs for aligned cue lists.

    Caller must have already verified alignment via check_alignment().
    """
    result = []
    for en, es in zip(en_cues, es_cues):
        en_text = _strip_and_normalize(en.text.strip())
        es_text = _strip_and_normalize(es.text.strip())
        result.append((en_text, es_text))
    return result


def quality_filter(english: str, spanish: str, min_length: int = 10) -> bool:
    """Return True if the cue pair passes all quality checks (i.e. is usable).

    Rejects:
    - Too short (below min_length characters)
    - Punctuation/numbers only
    - Length ratio out of bounds (Spanish/English < 0.3 or > 3.0)
    - Contains unstripped ASS positioning tags
    - Pure sound cues like [BANG] or [MUSIC]
    - Music-only indicators (♪ or 'music')
    - English text dominated by Spanish accent characters (>5% of alpha chars)
    """
    if len(english) < min_length or len(spanish) < min_length:
        return False

    # Punctuation/digits/non-word only
    if _RE_PUNCT_ONLY.match(english) or _RE_PUNCT_ONLY.match(spanish):
        return False

    # Length ratio
    ratio = len(spanish) / len(english)
    if ratio < 0.3 or ratio > 3.0:
        return False

    # Unstripped ASS styling tags still present
    if _RE_ASS_TAGS.search(english) or _RE_ASS_TAGS.search(spanish):
        return False

    # Pure sound cue (entire text is wrapped in square brackets)
    if _RE_SOUND_CUE.match(english) or _RE_SOUND_CUE.match(spanish):
        return False

    # Music-only indicator
    if _RE_MUSIC_ONLY.match(english) or _RE_MUSIC_ONLY.match(spanish):
        return False

    # High ratio of Spanish accent chars in English text → likely misaligned
    alpha_count = sum(1 for c in english if c.isalpha())
    if alpha_count > 0:
        spanish_char_ratio = sum(1 for c in english if c in _SPANISH_CHARS) / alpha_count
        if spanish_char_ratio > 0.05:
            return False

    return True


def dedup(
    pairs: List[Tuple[str, str, str]],
    max_copies: int = 3,
) -> List[Tuple[str, str, str]]:
    """Limit duplicate (english, spanish) pairs to max_copies occurrences.

    Input/output tuples are (english, spanish, source_path).
    """
    counts: Counter = Counter()
    result = []
    for en, es, source in pairs:
        key = (en, es)
        if counts[key] < max_copies:
            result.append((en, es, source))
            counts[key] += 1
    return result


def format_jsonl(english: str, spanish: str, source: str) -> str:
    """Format a cue pair as a JSONL training record."""
    record = {
        "instruction": "Translate English subtitle to natural Spanish.",
        "input": english,
        "output": spanish,
        "source": source,
    }
    return json.dumps(record, ensure_ascii=False)


# ---------------------------------------------------------------------------
# File reading helper
# ---------------------------------------------------------------------------

def _read_srt(path: Path) -> Optional[List[Cue]]:
    """Read and parse an SRT file; return None on error."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return parse_srt(content)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--roots",
    multiple=True,
    default=DEFAULT_ROOTS,
    show_default=True,
    help="Media root directories to scan (repeat for multiple).",
)
@click.option(
    "--output",
    default=None,
    help="Output JSONL file path. Defaults to stdout.",
)
@click.option(
    "--max-cues-per-pair",
    type=int,
    default=10000,
    show_default=True,
    help="Skip file pairs with more cues than this (pathological guard).",
)
@click.option(
    "--strict-ms",
    type=int,
    default=200,
    show_default=True,
    help="Max allowed timing delta in milliseconds for alignment check.",
)
@click.option(
    "--min-length",
    type=int,
    default=10,
    show_default=True,
    help="Minimum character length for each cue in a pair.",
)
@click.option(
    "--max-dup-copies",
    type=int,
    default=3,
    show_default=True,
    help="Maximum duplicate copies of the same (en, es) pair to keep.",
)
@click.option(
    "--max-per-series",
    type=int,
    default=0,
    show_default=True,
    help="Max pairs per series after quality filter (0 = no cap).  Applied before dedup.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Scan and count only — do not write output.",
)
def cli(
    roots: Tuple[str, ...],
    output: Optional[str],
    max_cues_per_pair: int,
    strict_ms: int,
    min_length: int,
    max_dup_copies: int,
    max_per_series: int,
    dry_run: bool,
) -> None:
    """Mine aligned (English, Spanish) subtitle pairs for training data."""
    roots_list = list(roots)

    # --- Phase 1: scan for pairs ---
    all_pairs = scan_pairs(roots_list)
    total_en = sum(1 for root in roots_list for _ in Path(root).rglob("*.en.srt") if Path(root).is_dir())
    has_es = len(all_pairs)

    click.echo(f"Scanned {total_en} .en.srt files", err=True)
    click.echo(f"  {has_es} had matching .es.srt", err=True)

    if dry_run:
        click.echo("  [dry-run] skipping alignment check, extraction, and output", err=True)
        return

    # --- Phase 2: alignment check, extraction, quality filter ---
    passed_alignment = 0
    rejected_alignment = 0
    raw_pairs: List[Tuple[str, str, str]] = []

    for en_path, es_path in all_pairs:
        en_cues = _read_srt(en_path)
        es_cues = _read_srt(es_path)
        if en_cues is None or es_cues is None:
            rejected_alignment += 1
            continue

        # Pathological size guard
        if len(en_cues) > max_cues_per_pair or len(es_cues) > max_cues_per_pair:
            rejected_alignment += 1
            continue

        if not check_alignment(en_cues, es_cues, strict_ms):
            rejected_alignment += 1
            continue

        passed_alignment += 1
        source_str = str(en_path)
        for en_text, es_text in extract_pairs(en_cues, es_cues):
            raw_pairs.append((en_text, es_text, source_str))

    click.echo(f"  {passed_alignment} passed strict alignment", err=True)
    click.echo(f"  {rejected_alignment} rejected (cue count or timing mismatch)", err=True)
    click.echo(f"Extracted: {len(raw_pairs)} cue pairs", err=True)

    # --- Phase 3: quality filter ---
    kept_pairs = [
        (en, es, src)
        for en, es, src in raw_pairs
        if quality_filter(en, es, min_length)
    ]
    kept_count = len(kept_pairs)
    total_count = len(raw_pairs)
    pct = int(kept_count / total_count * 100) if total_count else 0
    click.echo(f"  Quality-filtered: {kept_count} pairs kept ({pct}%)", err=True)

    # --- Phase 4: per-series cap (before dedup) ---
    if max_per_series > 0:
        capped_pairs = apply_series_cap(kept_pairs, max_per_series, roots_list)
        click.echo(
            f"Series cap ({max_per_series}/series): {len(capped_pairs)} pairs"
            f" (-{kept_count - len(capped_pairs)} capped)",
            err=True,
        )
    else:
        capped_pairs = kept_pairs

    # --- Phase 5: dedup ---
    deduped = dedup(capped_pairs, max_dup_copies)
    dupes_removed = len(capped_pairs) - len(deduped)
    click.echo(f"Deduplicated: {len(deduped)} unique pairs (-{dupes_removed} near-duplicates)", err=True)

    # --- Phase 6: output ---
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for en, es, src in deduped:
                fh.write(format_jsonl(en, es, src) + "\n")
        click.echo(f"Output written to: {output}", err=True)
    else:
        for en, es, src in deduped:
            sys.stdout.write(format_jsonl(en, es, src) + "\n")


if __name__ == "__main__":
    cli()
