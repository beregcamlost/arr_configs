"""Build the v2.2 training corpus by merging three layers.

Layers:
  A — parallel subtitle pairs  (mine_parallel_subs.py output)
  B — FreeDict word/phrase pairs  (mine_freedict.py output)
  C — curated hard cases  (curated_hard_cases.jsonl), optionally upsampled

Usage:
    python3 -m translation.build_corpus \\
        --layer-a /tmp/corpus-v22-layerA.jsonl \\
        --layer-b /tmp/corpus-v22-layerB.jsonl \\
        --layer-c automation/scripts/translation/curated_hard_cases.jsonl \\
        --layer-c-upsample 3 \\
        --output /tmp/corpus-v22.jsonl
"""

import json
import random
import sys
from pathlib import Path
from typing import List

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
    "--shuffle/--no-shuffle",
    default=True,
    show_default=True,
    help="Shuffle the final corpus.",
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
    shuffle: bool,
    seed: int,
    output: str,
) -> None:
    """Merge three corpus layers into a single shuffled JSONL training file."""

    # --- Load layers ---
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

    # --- Concatenate: A + B + C×upsample ---
    combined = a_records + b_records + c_upsampled

    click.echo(
        f"Combined (before shuffle): {len(combined):,} records",
        err=True,
    )

    # --- Shuffle ---
    if shuffle:
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
    click.echo(f"  TOTAL:                   {len(combined):>8,}", err=True)

    # Source breakdown
    from collections import Counter
    source_counts: Counter = Counter()
    for rec in combined:
        src = str(rec.get("source", "unknown"))
        if "freedict" in src:
            source_counts["freedict"] += 1
        elif "curated" in src:
            source_counts["curated"] += 1
        else:
            source_counts["parallel-sub"] += 1
    click.echo("", err=True)
    click.echo("  Source breakdown:", err=True)
    for src, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        pct = count / len(combined) * 100
        click.echo(f"    {src}: {count:>8,}  ({pct:.1f}%)", err=True)


if __name__ == "__main__":
    cli()
