"""Benchmark post-processing pipeline for Ollama subtitle translation."""

import logging
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click

from translation.srt_parser import Cue
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

MODELS = ["phi4-mini-subs"]

NAME_CORRECTIONS: Dict[str, str] = {
    "Araña": "Spider",
    "Arácnido": "Spider",
    "el Alto Campamento": "High Camp",
    "el Campamento Alto": "High Camp",
    "Alto Campamento": "High Camp",
    "Campamento Alto": "High Camp",
    "Los Comerciantes del Viento": "Los Windtraders",
    "El Comercio del Viento": "Los Windtraders",
    "Comerciantes del Viento": "Windtraders",
}

# Sort by length descending so longer phrases match before their substrings
_SORTED_NAME_CORRECTIONS = sorted(NAME_CORRECTIONS.items(), key=lambda kv: len(kv[0]), reverse=True)

# Summary table column widths
_COL_MODEL = 16
_COL_RAW = 7
_COL_FIXED = 7
_COL_GAIN = 7
_COL_TRANS = 10
_COL_POST = 9
_COL_TOTAL = 9


# ---------------------------------------------------------------------------
# Post-processing steps
# ---------------------------------------------------------------------------

def _apply_name_corrections(text: str) -> Tuple[str, int]:
    """Replace translated proper names with originals. Returns (corrected, change_count)."""
    corrected = text
    changes = 0
    for wrong, right in _SORTED_NAME_CORRECTIONS:
        if wrong in corrected:
            corrected = corrected.replace(wrong, right)
            changes += 1
    return corrected, changes


def _apply_hunspell(texts: List[str], source_texts: List[str]) -> Tuple[List[str], List[int]]:
    """Apply hunspell suggestions to texts. Returns (corrected_list, per_cue_change_counts)."""
    from translation.postprocess import _ENGLISH_PASSLIST

    issues = validate_translated_cues(texts, source_texts)
    result = list(texts)
    changes = [0] * len(texts)

    for issue in issues:
        idx = issue["index"]
        corrected = result[idx]
        for bad in issue["bad_words"]:
            word = bad["word"]
            if word.lower() in _ENGLISH_PASSLIST:
                log.debug("Skipping English word '%s' in line %d", word, idx)
                continue
            if not bad["suggestions"]:
                continue
            suggestion = bad["suggestions"][0]
            if ' ' in suggestion:
                log.debug("Rejecting '%s' -> '%s' (contains space) in line %d", word, suggestion, idx)
                continue
            ratio = SequenceMatcher(None, word.lower(), suggestion.lower()).ratio()
            if ratio < 0.80:
                log.debug("Rejecting '%s' -> '%s' (similarity %.2f) in line %d", word, suggestion, ratio, idx)
                continue
            corrected = re.sub(r'\b' + re.escape(word) + r'\b', suggestion, corrected)
            changes[idx] += 1
        result[idx] = corrected

    return result, changes


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def _run_postprocess(
    raw_texts: List[str],
    source_texts: List[str],
) -> Tuple[List[str], List[Dict], float]:
    """Run the full post-processing pipeline.

    Returns (fixed_texts, per_cue_stats_list, elapsed_seconds).
    Each stat dict: {"names": int, "hunspell": int}
    """
    start = time.monotonic()
    n = len(raw_texts)

    # Step 1: name corrections (per cue)
    after_names = []
    name_changes = []
    for text in raw_texts:
        fixed, cnt = _apply_name_corrections(text)
        after_names.append(fixed)
        name_changes.append(cnt)

    # Step 2: pattern fixes
    from translation.postprocess import apply_pattern_fixes
    after_patterns = [apply_pattern_fixes(t, "spanish") for t in after_names]

    # Step 3: hunspell
    after_hunspell, hunspell_changes = _apply_hunspell(after_patterns, source_texts)

    elapsed = time.monotonic() - start

    stats = [
        {"names": name_changes[i], "hunspell": hunspell_changes[i]}
        for i in range(n)
    ]
    return after_hunspell, stats, elapsed


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def _run_translation(
    base_url: str,
    model: str,
    cues: List[Cue],
    timeout: int,
) -> Tuple[Optional[List[str]], float]:
    """Translate cues. Returns (translated_texts or None, elapsed_seconds)."""
    start = time.monotonic()
    try:
        translated = _translate_batch(
            base_url=base_url,
            cues=cues,
            source_lang="english",
            target_lang="spanish",
            model=model,
            timeout=timeout,
            system_prompt_baked=model.endswith("-subs"),
        )
    except (OllamaUnavailable, Exception) as exc:
        elapsed = time.monotonic() - start
        click.echo(f"\n[ERROR] {model} failed after {elapsed:.1f}s: {exc}", err=True)
        return None, elapsed
    elapsed = time.monotonic() - start
    return translated, elapsed


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _print_model_block(
    model: str,
    trans_elapsed: float,
    toks: float,
    post_elapsed: float,
    cues: List[Cue],
    raw_texts: List[str],
    fixed_texts: List[str],
    reference: List[str],
    per_cue_stats: List[Dict],
    raw_sim: float,
    raw_exact: int,
    fixed_sim: float,
    fixed_exact: int,
) -> None:
    total = trans_elapsed + post_elapsed
    click.echo(f"\n--- {model} ---")
    click.echo(f"Translation: {trans_elapsed:.1f}s ({toks:.1f} tok/s)")
    click.echo(f"Post-processing: {post_elapsed:.1f}s")
    click.echo(f"Total: {total:.1f}s")
    click.echo("")

    for i, (cue, raw, fixed) in enumerate(zip(cues, raw_texts, fixed_texts)):
        en = cue.text.replace("\n", " ")
        raw_line = raw.replace("\n", " ")
        fixed_line = fixed.replace("\n", " ")
        ref_line = reference[i].replace("\n", " ") if i < len(reference) else ""
        stats = per_cue_stats[i]
        stat_str = f"[names: {stats['names']} | hunspell: {stats['hunspell']} changes]"

        if raw_line != fixed_line:
            click.echo(f"{i + 1:2}. EN: {en}")
            click.echo(f"    RAW: {raw_line}")
            click.echo(f"    FIX: {fixed_line}")
            click.echo(f"    REF: {ref_line}")
            click.echo(f"    {stat_str}")
        else:
            click.echo(f"{i + 1:2}. EN: {en}")
            click.echo(f"    ES: {raw_line}")
            click.echo(f"    REF: {ref_line}")

    click.echo("")
    click.echo(f"Raw similarity:            {raw_sim * 100:.1f}% ({raw_exact} exact)")
    click.echo(f"Post-processed similarity: {fixed_sim * 100:.1f}% ({fixed_exact} exact)")
    gain = (fixed_sim - raw_sim) * 100
    sign = "+" if gain >= 0 else ""
    click.echo(f"Quality gain: {sign}{gain:.1f}%")


def _print_final_comparison(
    results: List[Tuple[str, float, float, float, float, float, float, float]],
    num_cues: int,
) -> None:
    """Print the final comparison table.

    results: list of (model, raw_sim, fixed_sim, trans_elapsed, post_elapsed)
    """
    click.echo("\n=== Final Comparison ===")
    header = (
        f"{'Model':<{_COL_MODEL}}"
        f"{'Raw%':<{_COL_RAW}}"
        f"{'Fixed%':<{_COL_FIXED}}"
        f"{'Gain':<{_COL_GAIN}}"
        f"{'Trans(s)':<{_COL_TRANS}}"
        f"{'Post(s)':<{_COL_POST}}"
        f"{'Total(s)':<{_COL_TOTAL}}"
    )
    click.echo(header)
    total_width = _COL_MODEL + _COL_RAW + _COL_FIXED + _COL_GAIN + _COL_TRANS + _COL_POST + _COL_TOTAL
    click.echo("-" * total_width)

    for model, raw_sim, fixed_sim, trans_elapsed, post_elapsed in results:
        gain = (fixed_sim - raw_sim) * 100
        sign = "+" if gain >= 0 else ""
        total = trans_elapsed + post_elapsed
        raw_str = f"{raw_sim * 100:.1f}%"
        fixed_str = f"{fixed_sim * 100:.1f}%"
        gain_str = f"{sign}{gain:.1f}%"
        trans_str = f"{trans_elapsed:.1f}"
        post_str = f"{post_elapsed:.1f}"
        total_str = f"{total:.1f}"
        click.echo(
            f"{model:<{_COL_MODEL}}"
            f"{raw_str:<{_COL_RAW}}"
            f"{fixed_str:<{_COL_FIXED}}"
            f"{gain_str:<{_COL_GAIN}}"
            f"{trans_str:<{_COL_TRANS}}"
            f"{post_str:<{_COL_POST}}"
            f"{total_str:<{_COL_TOTAL}}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--file", "srt_file", required=True, help="Path to source .en.srt file.")
@click.option("--reference", required=True, help="Path to reference .es.srt file.")
@click.option("--start", default=0, show_default=True, help="Start index for cue selection.")
@click.option("--cues", "num_cues", default=DEFAULT_CUES, show_default=True, help="Number of cues to translate.")
@click.option("--base-url", default=DEFAULT_BASE_URL, show_default=True, help="Ollama base URL.")
@click.option("--timeout", default=900, show_default=True, help="Per-model translation timeout in seconds.")
def main(
    srt_file: str,
    reference: str,
    start: int,
    num_cues: int,
    base_url: str,
    timeout: int,
) -> None:
    """Benchmark post-processing pipeline across Ollama models."""
    logging.basicConfig(level=logging.WARNING)

    src_path = Path(srt_file)
    if not src_path.exists():
        click.echo(f"ERROR: source file not found: {srt_file}", err=True)
        sys.exit(1)

    ref_path = Path(reference)
    if not ref_path.exists():
        click.echo(f"ERROR: reference file not found: {reference}", err=True)
        sys.exit(1)

    cues = load_cues(srt_file, start, num_cues)
    if not cues:
        click.echo("ERROR: no cues parsed from source file.", err=True)
        sys.exit(1)

    ref_cues = load_cues(reference, start, num_cues)
    ref_texts = [c.text for c in ref_cues]
    if len(ref_texts) < len(cues):
        click.echo(
            f"WARNING: reference has {len(ref_texts)} cues, source has {len(cues)}. "
            "Scoring will cover only overlapping cues.",
            err=True,
        )

    source_texts = [c.text for c in cues]
    actual_cues = len(cues)

    click.echo("\n=== Post-Processing Benchmark ===")
    click.echo(f"Source: {src_path.name}")
    click.echo(f"Cues: {actual_cues} (start={start})")

    summary: List[Tuple[str, float, float, float, float]] = []

    for model in MODELS:
        click.echo(f"\n[Translating with {model}...]")
        raw_texts, trans_elapsed = _run_translation(base_url, model, cues, timeout)

        if raw_texts is None:
            summary.append((model, 0.0, 0.0, trans_elapsed, 0.0))
            click.echo(f"[Unloading {model}...]")
            unload_model(base_url, model)
            continue

        toks = tok_per_sec(raw_texts, trans_elapsed)
        raw_sim, raw_exact = score_against_reference(raw_texts, ref_texts)

        click.echo(f"[Post-processing {model}...]")
        fixed_texts, per_cue_stats, post_elapsed = _run_postprocess(raw_texts, source_texts)
        fixed_sim, fixed_exact = score_against_reference(fixed_texts, ref_texts)

        _print_model_block(
            model=model,
            trans_elapsed=trans_elapsed,
            toks=toks,
            post_elapsed=post_elapsed,
            cues=cues,
            raw_texts=raw_texts,
            fixed_texts=fixed_texts,
            reference=ref_texts,
            per_cue_stats=per_cue_stats,
            raw_sim=raw_sim,
            raw_exact=raw_exact,
            fixed_sim=fixed_sim,
            fixed_exact=fixed_exact,
        )

        summary.append((model, raw_sim, fixed_sim, trans_elapsed, post_elapsed))

        click.echo(f"\n[Unloading {model}...]")
        unload_model(base_url, model)

    _print_final_comparison(summary, actual_cues)


if __name__ == "__main__":
    main()
