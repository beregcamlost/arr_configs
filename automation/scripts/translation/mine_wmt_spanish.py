"""Fetch WMT/Europarl en-es parallel data for training corpus.

Tries the HuggingFace `datasets` library first (Helsinki-NLP/europarl + news_commentary).
Falls back to direct HuggingFace file downloads if `datasets` is not installed.

Usage:
    python3 -m translation.mine_wmt_spanish \\
        --output /tmp/layer-wmt.jsonl \\
        --target-pairs 150000
"""

from __future__ import annotations

import io
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

INSTRUCTION = "Translate English subtitle to natural Spanish."

# HuggingFace direct-download fallback URLs for Europarl en-es
# These are the individual language files from the Helsinki-NLP europarl dataset
HF_EUROPARL_EN_URL = (
    "https://huggingface.co/datasets/Helsinki-NLP/europarl/resolve/main/en-es/europarl-v7.es-en.en"
)
HF_EUROPARL_ES_URL = (
    "https://huggingface.co/datasets/Helsinki-NLP/europarl/resolve/main/en-es/europarl-v7.es-en.es"
)
# News commentary fallback
HF_NEWS_EN_URL = (
    "https://huggingface.co/datasets/Helsinki-NLP/news_commentary/resolve/main/en-es/"
    "news-commentary-v16.en-es.en"
)
HF_NEWS_ES_URL = (
    "https://huggingface.co/datasets/Helsinki-NLP/news_commentary/resolve/main/en-es/"
    "news-commentary-v16.en-es.es"
)

# OPUS Europarl as a reliable fallback
OPUS_EUROPARL_URL = (
    "https://object.pouta.csc.fi/OPUS-Europarl/v8/moses/en-es.txt.zip"
)
OPUS_NEWS_URL = (
    "https://object.pouta.csc.fi/OPUS-NewsCommentary/v16/moses/en-es.txt.zip"
)


# ---------------------------------------------------------------------------
# datasets-based loading
# ---------------------------------------------------------------------------

def _load_via_datasets() -> Iterator[Tuple[str, str]]:
    """Try loading Helsinki-NLP/europarl and news_commentary via `datasets`."""
    import datasets as ds  # type: ignore[import]

    sources = [
        ("Helsinki-NLP/europarl", "en-es"),
        ("Helsinki-NLP/news_commentary", "en-es"),
    ]

    for dataset_name, config in sources:
        try:
            click.echo(f"Loading {dataset_name} ({config}) via datasets...", err=True)
            dataset = ds.load_dataset(dataset_name, config, split="train", trust_remote_code=False)
            count = 0
            for row in dataset:
                translation = row.get("translation", {})
                en = translation.get("en", "")
                es = translation.get("es", "")
                if en and es:
                    yield en, es
                    count += 1
            click.echo(f"  {dataset_name}: {count:,} raw pairs", err=True)
        except Exception as exc:
            click.echo(f"  WARNING: failed to load {dataset_name}: {exc}", err=True)


# ---------------------------------------------------------------------------
# OPUS zip-based fallback
# ---------------------------------------------------------------------------

def _download_file(url: str, dest: Path) -> bool:
    """Download url to dest. Returns True on success, False on failure."""
    click.echo(f"Downloading {url} ...", err=True)
    try:
        def _progress(block_num: int, block_size: int, total_size: int) -> None:
            downloaded = block_num * block_size
            if total_size > 0:
                pct = min(100.0, downloaded / total_size * 100)
                mb_done = downloaded / 1_048_576
                mb_total = total_size / 1_048_576
                sys.stderr.write(f"\r  {pct:5.1f}%  {mb_done:7.1f} / {mb_total:.1f} MB")
            else:
                sys.stderr.write(f"\r  {downloaded / 1_048_576:7.1f} MB")
            sys.stderr.flush()

        urllib.request.urlretrieve(url, str(dest), reporthook=_progress)
        sys.stderr.write("\n")
        sys.stderr.flush()
        return True
    except Exception as exc:
        click.echo(f"\n  WARNING: download failed: {exc}", err=True)
        return False


def _stream_opus_zip(zip_path: Path) -> Iterator[Tuple[str, str]]:
    """Stream (en, es) pairs from an OPUS Moses zip."""
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        names = zf.namelist()
        en_member = next((n for n in names if n.endswith(".en")), None)
        es_member = next((n for n in names if n.endswith(".es")), None)
        if en_member is None or es_member is None:
            click.echo(f"WARNING: could not find .en/.es files in {zip_path}", err=True)
            return
        with zf.open(en_member) as en_fh, zf.open(es_member) as es_fh:
            for en_bytes, es_bytes in zip(en_fh, es_fh):
                yield (
                    en_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                    es_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                )


def _load_via_opus_fallback(cache_dir: Path) -> Iterator[Tuple[str, str]]:
    """Fall back to downloading OPUS Europarl + NewsCommentary zips directly."""
    sources = [
        ("europarl", OPUS_EUROPARL_URL),
        ("news_commentary", OPUS_NEWS_URL),
    ]
    for name, url in sources:
        zip_path = cache_dir / f"opus_{name}_en-es.zip"
        if not zip_path.exists():
            if not _download_file(url, zip_path):
                click.echo(f"  Skipping {name} (download failed)", err=True)
                continue
        else:
            click.echo(f"Using cached: {zip_path}", err=True)
        count = 0
        for pair in _stream_opus_zip(zip_path):
            yield pair
            count += 1
        click.echo(f"  {name}: {count:,} raw pairs", err=True)


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
    default=150_000,
    show_default=True,
    help="Target number of output pairs.",
)
@click.option(
    "--cache-dir",
    default="/tmp/wmt-cache",
    show_default=True,
    help="Directory for cached downloads.",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for sampling.",
)
def cli(output: str, target_pairs: int, cache_dir: str, seed: int) -> None:
    """Fetch WMT/Europarl en-es parallel data and filter to target-pairs."""

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    # --- Select loading strategy ---
    use_datasets = False
    try:
        import datasets  # noqa: F401
        use_datasets = True
        click.echo("datasets library found — using HuggingFace datasets API", err=True)
    except ImportError:
        click.echo("datasets not available — using OPUS direct download fallback", err=True)

    raw_iter = _load_via_datasets() if use_datasets else _load_via_opus_fallback(cache_path)

    # --- Filter and dedup ---
    raw_count = 0
    filtered_count = 0
    seen: set = set()
    kept: list[dict] = []

    for en, es in raw_iter:
        raw_count += 1

        if not quality_filter_pair(en, es):
            continue
        filtered_count += 1

        key = (en.lower().strip(), es.lower().strip())
        if key in seen:
            continue
        seen.add(key)

        kept.append({"en": en.strip(), "es": es.strip()})

        if raw_count % 500_000 == 0:
            click.echo(
                f"  {raw_count:,} raw | {filtered_count:,} filtered | {len(kept):,} deduped",
                err=True,
            )

    deduped_count = len(kept)

    # --- Sample to target ---
    if deduped_count > target_pairs:
        rng = random.Random(seed)
        kept = rng.sample(kept, target_pairs)
        click.echo(
            f"Sampled {target_pairs:,} from {deduped_count:,} (seed={seed})",
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
                "source": "wmt",
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --- Stats ---
    click.echo("", err=True)
    click.echo("=== WMT Mining Stats ===", err=True)
    click.echo(f"  Raw pairs processed:  {raw_count:>10,}", err=True)
    click.echo(f"  After quality filter: {filtered_count:>10,}", err=True)
    click.echo(f"  After dedup:          {deduped_count:>10,}", err=True)
    click.echo(f"  Output (sampled):     {output_count:>10,}", err=True)
    click.echo(f"  Written to: {output}", err=True)


if __name__ == "__main__":
    cli()
