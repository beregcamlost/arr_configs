"""Download + filter OPUS OpenSubtitles en-es parallel corpus.

Downloads the OPUS OpenSubtitles v2024 Moses en-es zip (~400 MB), streams
both sides line-by-line, applies quality filters, deduplicates, and writes up
to --target-pairs JSONL records.

Usage:
    python3 -m translation.mine_opensubtitles \\
        --output /tmp/layer-opensubs.jsonl \\
        --target-pairs 400000 \\
        --cache-dir /tmp/opus-cache
"""

from __future__ import annotations

import json
import random
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterator, Tuple

import click

from translation.corpus_filters import quality_filter_pair

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPUS_URL = (
    "https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2024/moses/en-es.txt.zip"
)
CACHE_FILENAME = "opensubs_en-es_v2024.zip"

EN_FILENAME = "OpenSubtitles.en-es.en"
ES_FILENAME = "OpenSubtitles.en-es.es"

INSTRUCTION = "Translate English subtitle to natural Spanish."


# ---------------------------------------------------------------------------
# Download with progress bar
# ---------------------------------------------------------------------------

def _download_with_progress(url: str, dest: Path) -> None:
    """Download url to dest, showing a stderr progress bar."""
    click.echo(f"Downloading {url} → {dest} ...", err=True)

    def _progress(block_num: int, block_size: int, total_size: int) -> None:
        downloaded = block_num * block_size
        if total_size > 0:
            pct = min(100.0, downloaded / total_size * 100)
            mb_done = downloaded / 1_048_576
            mb_total = total_size / 1_048_576
            sys.stderr.write(f"\r  {pct:5.1f}%  {mb_done:7.1f} / {mb_total:.1f} MB")
            sys.stderr.flush()
        else:
            mb_done = downloaded / 1_048_576
            sys.stderr.write(f"\r  {mb_done:7.1f} MB downloaded")
            sys.stderr.flush()

    urllib.request.urlretrieve(url, str(dest), reporthook=_progress)
    sys.stderr.write("\n")
    sys.stderr.flush()
    click.echo("Download complete.", err=True)


# ---------------------------------------------------------------------------
# Streaming pair reader from zip
# ---------------------------------------------------------------------------

def _stream_pairs(zip_path: Path) -> Iterator[Tuple[str, str]]:
    """Open the OPUS zip and stream (en, es) line pairs without loading all into memory."""
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        names = zf.namelist()

        # Find the en/es files — they may be in a subdirectory inside the zip
        en_member = next((n for n in names if n.endswith(EN_FILENAME)), None)
        es_member = next((n for n in names if n.endswith(ES_FILENAME)), None)

        if en_member is None or es_member is None:
            click.echo(
                f"ERROR: expected {EN_FILENAME} and {ES_FILENAME} in zip.\n"
                f"Found: {names[:10]}",
                err=True,
            )
            return

        with zf.open(en_member) as en_fh, zf.open(es_member) as es_fh:
            for en_bytes, es_bytes in zip(en_fh, es_fh):
                yield (
                    en_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                    es_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--output",
    required=True,
    help="Output JSONL file path.",
)
@click.option(
    "--target-pairs",
    type=int,
    default=400_000,
    show_default=True,
    help="Target number of output pairs (random-sampled to this if over).",
)
@click.option(
    "--cache-dir",
    default="/tmp/opus-cache",
    show_default=True,
    help="Directory to store the downloaded zip file.",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for sampling.",
)
def cli(output: str, target_pairs: int, cache_dir: str, seed: int) -> None:
    """Download + filter OPUS OpenSubtitles en-es to ~target-pairs high-quality pairs."""

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    zip_path = cache_path / CACHE_FILENAME

    # --- Download (skip if cached) ---
    if zip_path.exists():
        click.echo(f"Using cached zip: {zip_path}", err=True)
    else:
        _download_with_progress(OPUS_URL, zip_path)

    # --- Stream, filter, dedup ---
    click.echo("Streaming and filtering pairs...", err=True)

    raw_count = 0
    filtered_count = 0
    seen: set = set()
    kept: list[dict] = []

    # Collect at most 2× target to allow sampling diversity, then stop early.
    collect_cap = target_pairs * 2

    for en, es in _stream_pairs(zip_path):
        raw_count += 1

        if not quality_filter_pair(en, es):
            continue
        filtered_count += 1

        key = (en.lower().strip(), es.lower().strip())
        if key in seen:
            continue
        seen.add(key)

        kept.append({"en": en.strip(), "es": es.strip()})

        if raw_count % 1_000_000 == 0:
            click.echo(
                f"  {raw_count:,} raw | {filtered_count:,} filtered | {len(kept):,} deduped",
                err=True,
            )

        # Early stop once we have enough to sample from
        if len(kept) >= collect_cap:
            click.echo(
                f"  Early stop: collected {collect_cap:,} deduped pairs (target×2)",
                err=True,
            )
            break

    deduped_count = len(kept)

    # --- Sample to target ---
    if deduped_count > target_pairs:
        rng = random.Random(seed)
        kept = rng.sample(kept, target_pairs)
        click.echo(
            f"Sampled {target_pairs:,} from {deduped_count:,} deduped pairs (seed={seed})",
            err=True,
        )
    output_count = len(kept)

    # --- Write JSONL ---
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for pair in kept:
            record = {
                "instruction": INSTRUCTION,
                "input": pair["en"],
                "output": pair["es"],
                "source": "opensubtitles",
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --- Stats ---
    click.echo("", err=True)
    click.echo("=== OpenSubtitles Mining Stats ===", err=True)
    click.echo(f"  Raw pairs streamed:   {raw_count:>10,}", err=True)
    click.echo(f"  After quality filter: {filtered_count:>10,}", err=True)
    click.echo(f"  After dedup:          {deduped_count:>10,}", err=True)
    click.echo(f"  Output (sampled):     {output_count:>10,}", err=True)
    click.echo(f"  Written to: {output}", err=True)


if __name__ == "__main__":
    cli()
