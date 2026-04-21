"""Collect unpaired English subtitle cues from the media library.

Scans media roots for .en.srt files that do NOT have a matching .es.srt sibling.
Extracts individual subtitle cues, applies basic quality filters, and writes
up to --target cues as JSONL with {"english": "..."} format.

The output is consumed by distill_from_winner.py (runs on WSL GPU) to generate
Spanish translations for self-distillation training data.

Usage:
    python3 -m translation.collect_unpaired_english \\
        --output /tmp/distill-input.jsonl \\
        --target 50000
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Iterator, List, Optional

import click

from translation.srt_parser import Cue, parse_srt

# ---------------------------------------------------------------------------
# Default media roots (same as mine_parallel_subs)
# ---------------------------------------------------------------------------

DEFAULT_ROOTS = [
    "/APPBOX_DATA/storage/media/tv",
    "/APPBOX_DATA/storage/media/tvanimated",
    "/APPBOX_DATA/storage/media/movies",
    "/APPBOX_DATA/storage/media/moviesanimated",
]

# ---------------------------------------------------------------------------
# Compiled regexes (same cleanup as mine_parallel_subs)
# ---------------------------------------------------------------------------

_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_ASS_TAGS = re.compile(r"\{[^}]+\}")
_RE_WHITESPACE = re.compile(r"[ \t]+")
_RE_PUNCT_ONLY = re.compile(r"^[\W\d_]+$", re.UNICODE)
_RE_MUSIC_ONLY = re.compile(r"^[♪\s]+$|^\s*music\s*$", re.IGNORECASE)
_RE_SOUND_CUE = re.compile(r"^\[.*\]$", re.DOTALL)

_SPANISH_CHARS = frozenset("áéíóúñüÁÉÍÓÚÑÜ")


# ---------------------------------------------------------------------------
# Cue normalization / filtering
# ---------------------------------------------------------------------------

def _normalize_cue(text: str) -> str:
    """Strip HTML/ASS tags, collapse whitespace."""
    text = _RE_HTML_TAGS.sub("", text)
    text = _RE_ASS_TAGS.sub("", text)
    text = text.replace("\n", " ")
    text = _RE_WHITESPACE.sub(" ", text)
    return text.strip()


def _cue_is_usable(text: str, min_length: int = 15) -> bool:
    """Return True if a cue is suitable as a distillation input."""
    if len(text) < min_length:
        return False
    if _RE_PUNCT_ONLY.match(text):
        return False
    if _RE_MUSIC_ONLY.match(text):
        return False
    if _RE_SOUND_CUE.match(text):
        return False
    # Contains URLs/HTML
    if any(tok in text for tok in ("http://", "https://", "www.", "<", ">")):
        return False
    # Likely already Spanish (has >5% Spanish accent chars)
    alpha = sum(1 for c in text if c.isalpha())
    if alpha > 0:
        sp_ratio = sum(1 for c in text if c in _SPANISH_CHARS) / alpha
        if sp_ratio > 0.05:
            return False
    return True


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_unpaired_english(roots: List[str]) -> Iterator[Path]:
    """Yield .en.srt paths that have no matching .es.srt sibling."""
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for en_path in root_path.rglob("*.en.srt"):
            name = en_path.name
            if not name.endswith(".en.srt"):
                continue
            stem = name[: -len(".en.srt")]
            es_path = en_path.parent / f"{stem}.es.srt"
            if not es_path.is_file():
                yield en_path


def _read_srt(path: Path) -> Optional[List[Cue]]:
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
    "--output",
    required=True,
    help="Output JSONL path: one {\"english\": \"...\"} per line.",
)
@click.option(
    "--target",
    type=int,
    default=50_000,
    show_default=True,
    help="Maximum cues to collect.",
)
@click.option(
    "--roots",
    multiple=True,
    default=DEFAULT_ROOTS,
    show_default=True,
    help="Media root directories to scan (repeat for multiple).",
)
@click.option(
    "--min-length",
    type=int,
    default=15,
    show_default=True,
    help="Minimum cue character length.",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for sampling when over target.",
)
def cli(
    output: str,
    target: int,
    roots: tuple,
    min_length: int,
    seed: int,
) -> None:
    """Collect unpaired English cues (no .es.srt sibling) for distillation input."""

    roots_list = list(roots)
    click.echo(f"Scanning roots: {roots_list}", err=True)

    files_scanned = 0
    files_skipped = 0
    raw_cue_count = 0
    kept: list[str] = []

    for en_path in scan_unpaired_english(roots_list):
        files_scanned += 1
        cues = _read_srt(en_path)
        if cues is None:
            files_skipped += 1
            continue

        for cue in cues:
            raw_cue_count += 1
            text = _normalize_cue(cue.text)
            if _cue_is_usable(text, min_length):
                kept.append(text)

        if files_scanned % 500 == 0:
            click.echo(
                f"  {files_scanned:,} files | {raw_cue_count:,} raw cues | {len(kept):,} kept",
                err=True,
            )

    click.echo(
        f"Scan complete: {files_scanned:,} files, {raw_cue_count:,} raw cues, "
        f"{len(kept):,} usable",
        err=True,
    )

    # Dedup
    deduped = list(dict.fromkeys(kept))  # preserve order, remove exact dups
    click.echo(f"After dedup: {len(deduped):,} unique cues", err=True)

    # Sample to target
    if len(deduped) > target:
        rng = random.Random(seed)
        deduped = rng.sample(deduped, target)
        click.echo(f"Sampled {target:,} from {len(deduped):,} (seed={seed})", err=True)

    output_count = len(deduped)

    # Write
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for cue in deduped:
            fh.write(json.dumps({"english": cue}, ensure_ascii=False) + "\n")

    click.echo("", err=True)
    click.echo("=== Unpaired English Collection Stats ===", err=True)
    click.echo(f"  Files scanned:    {files_scanned:>8,}", err=True)
    click.echo(f"  Files skipped:    {files_skipped:>8,}", err=True)
    click.echo(f"  Raw cues:         {raw_cue_count:>8,}", err=True)
    click.echo(f"  Output cues:      {output_count:>8,}", err=True)
    click.echo(f"  Written to: {output}", err=True)


if __name__ == "__main__":
    cli()
