"""Build the v2.x training corpus by merging up to six layers.

Layers:
  A — parallel subtitle pairs  (mine_parallel_subs.py output)
  B — FreeDict word/phrase pairs  (mine_freedict.py output)
  C — curated hard cases  (curated_hard_cases.jsonl), optionally upsampled
  D — OpenSubtitles OPUS pairs  (mine_opensubtitles.py output)  [optional]
  E — WMT/Europarl pairs  (mine_wmt_spanish.py output)  [optional]
  F — self-distilled pairs  (distill_from_winner.py output)  [optional]

Usage:
    python3 -m translation.build_corpus \\
        --layer-a /tmp/corpus-v22-layerA.jsonl \\
        --layer-b /tmp/corpus-v22-layerB.jsonl \\
        --layer-c automation/scripts/translation/curated_hard_cases.jsonl \\
        --layer-c-upsample 3 \\
        --layer-d /tmp/layer-opensubs.jsonl \\
        --layer-e /tmp/layer-wmt.jsonl \\
        --layer-f /tmp/layer-distilled.jsonl \\
        --output /tmp/corpus-v23.jsonl

Curriculum ordering (--curriculum-order flag):
    When set, output is sorted easy-first rather than shuffled:
      easy   (input length < 20 chars)  → short phrases, word pairs
      medium (all other records)
      hard   (curated source, upsampled last)
    Within each tier the order is deterministic (stable sort by input length).
    --curriculum-order overrides --shuffle.
"""

import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

import click


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path) -> List[dict]:
    """Read all lines of a JSONL file and return as list of dicts.

    Skips blank lines.  Raises on malformed JSON.
    """
    records = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON — {exc}") from exc
    return records


def _write_jsonl(records: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Curriculum ordering
# ---------------------------------------------------------------------------

_EASY_LEN_THRESHOLD = 20  # chars in "input" field


def _curriculum_sort(records: List[dict]) -> List[dict]:
    """Sort records easy-first within three tiers (stable within tier).

    Tier 0 (easy):   input length < 20 — short phrases, word pairs
    Tier 1 (medium): everything else
    Tier 2 (hard):   source == "curated" — targeted hard cases last
    """
    def _tier(rec: dict) -> int:
        src = str(rec.get("source", ""))
        if "curated" in src:
            return 2
        if len(rec.get("input", "")) < _EASY_LEN_THRESHOLD:
            return 0
        return 1

    return sorted(records, key=lambda r: (_tier(r), len(r.get("input", ""))))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--layer-a",
    required=True,
    type=click.Path(exists=True),
    help="Layer A JSONL: parallel subtitle pairs.",
)
@click.option(
    "--layer-b",
    required=True,
    type=click.Path(exists=True),
    help="Layer B JSONL: FreeDict word/phrase pairs.",
)
@click.option(
    "--layer-c",
    required=True,
    type=click.Path(exists=True),
    help="Layer C JSONL: curated hard cases.",
)
@click.option(
    "--layer-c-upsample",
    type=int,
    default=3,
    show_default=True,
    help="Multiplier for layer C (e.g. 3 = triplicate layer C rows).",
)
@click.option(
    "--layer-d",
    default=None,
    type=click.Path(exists=True),
    help="Layer D JSONL: OpenSubtitles OPUS pairs (optional).",
)
@click.option(
    "--layer-e",
    default=None,
    type=click.Path(exists=True),
    help="Layer E JSONL: WMT/Europarl pairs (optional).",
)
@click.option(
    "--layer-f",
    default=None,
    type=click.Path(exists=True),
    help="Layer F JSONL: self-distilled pairs (optional).",
)
@click.option(
    "--shuffle/--no-shuffle",
    default=True,
    show_default=True,
    help="Shuffle the final corpus (ignored when --curriculum-order is set).",
)
@click.option(
    "--curriculum-order",
    is_flag=True,
    default=False,
    help=(
        "Sort output easy-first (short/simple) → medium → hard (curated). "
        "Overrides --shuffle."
    ),
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for shuffling.",
)
@click.option(
    "--output",
    required=True,
    help="Output JSONL path.",
)
def cli(
    layer_a: str,
    layer_b: str,
    layer_c: str,
    layer_c_upsample: int,
    layer_d: Optional[str],
    layer_e: Optional[str],
    layer_f: Optional[str],
    shuffle: bool,
    curriculum_order: bool,
    seed: int,
    output: str,
) -> None:
    """Merge up to six corpus layers into a single training JSONL file."""

    # --- Load required layers ---
    a_records = _read_jsonl(Path(layer_a))
    b_records = _read_jsonl(Path(layer_b))
    c_records = _read_jsonl(Path(layer_c))

    click.echo(f"Layer A (parallel subs): {len(a_records):,} records", err=True)
    click.echo(f"Layer B (freedict):      {len(b_records):,} records", err=True)
    click.echo(f"Layer C (curated):       {len(c_records):,} records", err=True)

    # --- Upsample layer C ---
    c_upsampled = c_records * max(1, layer_c_upsample)
    click.echo(
        f"Layer C after ×{layer_c_upsample} upsample: {len(c_upsampled):,} records",
        err=True,
    )

    # --- Load optional layers ---
    d_records: List[dict] = []
    e_records: List[dict] = []
    f_records: List[dict] = []

    if layer_d:
        d_records = _read_jsonl(Path(layer_d))
        click.echo(f"Layer D (opensubs):      {len(d_records):,} records", err=True)
    if layer_e:
        e_records = _read_jsonl(Path(layer_e))
        click.echo(f"Layer E (wmt):           {len(e_records):,} records", err=True)
    if layer_f:
        f_records = _read_jsonl(Path(layer_f))
        click.echo(f"Layer F (distilled):     {len(f_records):,} records", err=True)

    # --- Concatenate: A + B + C×upsample + D + E + F ---
    combined = a_records + b_records + c_upsampled + d_records + e_records + f_records

    click.echo(
        f"Combined (before ordering): {len(combined):,} records",
        err=True,
    )

    # --- Order / shuffle ---
    if curriculum_order:
        combined = _curriculum_sort(combined)
        click.echo("Applied curriculum ordering (easy → medium → hard)", err=True)
    elif shuffle:
        rng = random.Random(seed)
        rng.shuffle(combined)
        click.echo(f"Shuffled with seed={seed}", err=True)

    # --- Write ---
    out_path = Path(output)
    _write_jsonl(combined, out_path)
    click.echo(f"Written {len(combined):,} records to: {output}", err=True)

    # --- Stats summary ---
    click.echo("", err=True)
    click.echo("=== Corpus Stats ===", err=True)
    click.echo(f"  Layer A (parallel subs): {len(a_records):>8,}", err=True)
    click.echo(f"  Layer B (freedict):      {len(b_records):>8,}", err=True)
    click.echo(f"  Layer C (curated raw):   {len(c_records):>8,}", err=True)
    click.echo(f"  Layer C (×{layer_c_upsample} upsampled): {len(c_upsampled):>8,}", err=True)
    if layer_d:
        click.echo(f"  Layer D (opensubs):      {len(d_records):>8,}", err=True)
    if layer_e:
        click.echo(f"  Layer E (wmt):           {len(e_records):>8,}", err=True)
    if layer_f:
        click.echo(f"  Layer F (distilled):     {len(f_records):>8,}", err=True)
    click.echo(f"  TOTAL:                   {len(combined):>8,}", err=True)

    # Source breakdown
    source_counts: Counter = Counter()
    for rec in combined:
        src = str(rec.get("source", "unknown"))
        if "freedict" in src:
            source_counts["freedict"] += 1
        elif "curated" in src:
            source_counts["curated"] += 1
        elif "opensubtitles" in src:
            source_counts["opensubtitles"] += 1
        elif "wmt" in src:
            source_counts["wmt"] += 1
        elif "distilled" in src:
            source_counts["distilled"] += 1
        else:
            source_counts["parallel-sub"] += 1
    click.echo("", err=True)
    click.echo("  Source breakdown:", err=True)
    for src, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        pct = count / len(combined) * 100
        click.echo(f"    {src}: {count:>8,}  ({pct:.1f}%)", err=True)


if __name__ == "__main__":
    cli()
