"""A/B test Ollama models for subtitle translation quality."""

import logging
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional

import click

from translation.srt_parser import parse_srt, Cue
from translation.ollama_client import _translate_batch, OllamaUnavailable
from translation.spell_check import validate_translated_cues
from translation.bench_utils import (
    DEFAULT_BASE_URL,
    DEFAULT_CUES,
    load_cues,
    score_against_reference,
    unload_model,
    tok_per_sec,
)

log = logging.getLogger(__name__)

MODELS = [
    "phi4-mini",
    "qwen3:4b",
    "translategemma",
]

# Column widths for the summary table
_COL_MODEL = 18
_COL_TIME = 8
_COL_TOKS = 8
_COL_ISSUES = 10
_COL_SIM = 12
_COL_EXACT = 8


def _run_model(
    base_url: str,
    model: str,
    cues: List[Cue],
    reference: Optional[List[str]],
    timeout: int = 600,
) -> Tuple[Optional[List[str]], float, int, float, int]:
    """Translate cues with the given model.

    Returns (translated_texts_or_None, elapsed_seconds, hunspell_issue_count,
             similarity_score, exact_match_count).
    """
    source_texts = [c.text for c in cues]
    start = time.monotonic()
    try:
        translated = _translate_batch(
            base_url=base_url,
            cues=cues,
            source_lang="english",
            target_lang="spanish",
            model=model,
            timeout=timeout,
        )
    except (OllamaUnavailable, Exception) as exc:
        elapsed = time.monotonic() - start
        click.echo(f"\n[ERROR] {model} failed after {elapsed:.1f}s: {exc}", err=True)
        return None, elapsed, 0, 0.0, 0

    elapsed = time.monotonic() - start
    issues = validate_translated_cues(translated, source_texts)

    sim, exact = (0.0, 0)
    if reference is not None:
        sim, exact = score_against_reference(translated, reference)

    return translated, elapsed, len(issues), sim, exact


def _print_model_block(
    model: str,
    elapsed: float,
    toks: float,
    issue_count: int,
    cues: List[Cue],
    translated: List[str],
    reference: Optional[List[str]],
    sim: float,
    exact: int,
) -> None:
    if reference is not None:
        header = (
            f"--- {model} ({elapsed:.1f}s, {toks:.1f} tok/s, "
            f"{issue_count} hunspell, {sim * 100:.1f}% sim, {exact} exact) ---"
        )
    else:
        header = f"--- {model} ({elapsed:.1f}s, {toks:.1f} tok/s, {issue_count} hunspell issues) ---"
    click.echo(f"\n{header}")

    if reference is not None:
        for i, (cue, tx) in enumerate(zip(cues, translated), 1):
            src = cue.text.replace("\n", " ")
            tgt = tx.replace("\n", " ")
            ref_text = reference[i - 1].replace("\n", " ") if (i - 1) < len(reference) else ""
            match_marker = "  ✓" if tx == reference[i - 1] else ""
            click.echo(f"{i:2}. EN: {src}")
            click.echo(f"    ES: {tgt}")
            click.echo(f"    RF: {ref_text}{match_marker}")
    else:
        src_width = max(len(c.text.replace("\n", " ")) for c in cues)
        src_width = min(src_width, 45)
        for i, (cue, tx) in enumerate(zip(cues, translated), 1):
            src = cue.text.replace("\n", " ")
            tgt = tx.replace("\n", " ")
            click.echo(f"{i:2}. {src:<{src_width}} -> {tgt}")


def _print_summary(
    results: List[Tuple[str, float, float, int, float, int]],
    has_reference: bool,
    num_cues: int,
) -> None:
    click.echo("\n=== Summary ===")
    if has_reference:
        header = (
            f"{'Model':<{_COL_MODEL}}"
            f"{'Time':<{_COL_TIME}}"
            f"{'Tok/s':<{_COL_TOKS}}"
            f"{'Hunspell':<{_COL_ISSUES}}"
            f"{'Similarity':<{_COL_SIM}}"
            f"{'Exact':<{_COL_EXACT}}"
        )
        total_width = _COL_MODEL + _COL_TIME + _COL_TOKS + _COL_ISSUES + _COL_SIM + _COL_EXACT
    else:
        header = (
            f"{'Model':<{_COL_MODEL}}"
            f"{'Time':<{_COL_TIME}}"
            f"{'Tok/s':<{_COL_TOKS}}"
            f"{'Hunspell Issues':<{_COL_ISSUES + _COL_SIM + _COL_EXACT}}"
        )
        total_width = _COL_MODEL + _COL_TIME + _COL_TOKS + _COL_ISSUES + _COL_SIM + _COL_EXACT
    click.echo(header)
    click.echo("-" * total_width)

    for model, elapsed, toks, issue_count, sim, exact in results:
        time_str = f"{elapsed:.1f}s"
        toks_str = f"{toks:.1f}"
        issues_str = str(issue_count) if issue_count >= 0 else "FAILED"
        row = (
            f"{model:<{_COL_MODEL}}"
            f"{time_str:<{_COL_TIME}}"
            f"{toks_str:<{_COL_TOKS}}"
            f"{issues_str:<{_COL_ISSUES}}"
        )
        if has_reference:
            sim_str = f"{sim * 100:.1f}%" if issue_count >= 0 else "-"
            exact_str = f"{exact}/{num_cues}" if issue_count >= 0 else "-"
            row += f"{sim_str:<{_COL_SIM}}{exact_str:<{_COL_EXACT}}"
        click.echo(row)


@click.command()
@click.option(
    "--file", "srt_file",
    required=True,
    help="Path to the source .en.srt file.",
)
@click.option(
    "--cues", "num_cues",
    default=DEFAULT_CUES,
    show_default=True,
    help="Number of cues to translate per model.",
)
@click.option(
    "--start",
    default=0,
    show_default=True,
    help="Start index for cue selection.",
)
@click.option(
    "--reference",
    default=None,
    help="Path to reference .es.srt file for quality comparison.",
)
@click.option(
    "--base-url",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="Ollama base URL (or set OLLAMA_BASE_URL env var).",
)
@click.option(
    "--timeout",
    default=600,
    show_default=True,
    help="Per-model request timeout in seconds.",
)
def main(srt_file: str, num_cues: int, start: int, reference: Optional[str], base_url: str, timeout: int) -> None:
    """A/B test Ollama models for subtitle translation quality."""
    logging.basicConfig(level=logging.WARNING)

    path = Path(srt_file)
    if not path.exists():
        click.echo(f"ERROR: file not found: {srt_file}", err=True)
        sys.exit(1)

    cues = load_cues(srt_file, start, num_cues)
    if not cues:
        click.echo("ERROR: no cues parsed from file.", err=True)
        sys.exit(1)

    ref_texts: Optional[List[str]] = None
    if reference is not None:
        ref_path = Path(reference)
        if not ref_path.exists():
            click.echo(f"ERROR: reference file not found: {reference}", err=True)
            sys.exit(1)
        ref_content = ref_path.read_text(encoding="utf-8", errors="replace")
        ref_cues = parse_srt(ref_content)[start:start + num_cues]
        ref_texts = [c.text for c in ref_cues]
        if len(ref_texts) < len(cues):
            click.echo(
                f"WARNING: reference has {len(ref_texts)} cues, source has {len(cues)}. "
                "Scoring will cover only overlapping cues.",
                err=True,
            )

    actual_cues = len(cues)
    click.echo(f"\n=== A/B Model Comparison ===")
    click.echo(f"Source: {path.name}")
    click.echo(f"Cues: {actual_cues} (start={start})")
    if reference is not None:
        click.echo(f"Reference: {Path(reference).name}")
    click.echo(f"Base URL: {base_url}")

    summary: List[Tuple[str, float, float, int, float, int]] = []

    for model in MODELS:
        click.echo(f"\n[Running {model}...]", nl=False)
        translated, elapsed, issue_count, sim, exact = _run_model(
            base_url, model, cues, ref_texts, timeout
        )

        if translated is None:
            summary.append((model, elapsed, 0.0, -1, 0.0, 0))
            continue

        toks = tok_per_sec(translated, elapsed)
        summary.append((model, elapsed, toks, issue_count, sim, exact))
        _print_model_block(model, elapsed, toks, issue_count, cues, translated, ref_texts, sim, exact)
        click.echo(f"[Unloading {model}...]")
        unload_model(base_url, model)

    _print_summary(summary, has_reference=ref_texts is not None, num_cues=actual_cues)


if __name__ == "__main__":
    main()
