"""Self-distillation: use v2-17b-r1 on WSL GPU to translate unpaired English cues.

This script runs ON WSL (Windows Subsystem for Linux) with CUDA + transformers.
Do NOT run this on the orchestrator (no GPU there).

Workflow:
1. On orchestrator: run collect_unpaired_english.py → /tmp/distill-input.jsonl
2. scp /tmp/distill-input.jsonl beren@WSL:/tmp/distill-input.jsonl
3. On WSL: python3 -m translation.distill_from_winner \\
       --source-model-path /home/beren/training/out/v2-17b-r1/merged \\
       --input /tmp/distill-input.jsonl \\
       --output /tmp/layer-distilled.jsonl \\
       --target-pairs 50000
4. scp beren@WSL:/tmp/layer-distilled.jsonl back to orchestrator

Requirements (WSL only):
    pip install torch transformers accelerate

Usage:
    python3 -m translation.distill_from_winner \\
        --source-model-path /home/beren/training/out/v2-17b-r1/merged \\
        --input /tmp/distill-input.jsonl \\
        --output /tmp/layer-distilled.jsonl \\
        --target-pairs 50000 \\
        --batch-size 16
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Iterator, List

import click

INSTRUCTION = "Translate English subtitle to natural Spanish."
DEFAULT_PROMPT_TEMPLATE = (
    "### Instruction:\n{instruction}\n\n"
    "### Input:\n{input}\n\n"
    "### Response:\n"
)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _read_input_jsonl(path: Path) -> List[str]:
    """Read {\"english\": \"...\"} JSONL and return list of English cues."""
    cues = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                click.echo(f"WARNING: {path}:{lineno}: invalid JSON — {exc}", err=True)
                continue
            english = obj.get("english", "").strip()
            if english:
                cues.append(english)
    return cues


def _build_prompt(english: str) -> str:
    return DEFAULT_PROMPT_TEMPLATE.format(
        instruction=INSTRUCTION,
        input=english,
    )


def _extract_response(generated: str, prompt: str) -> str:
    """Strip prompt prefix from generated text and return clean response."""
    if generated.startswith(prompt):
        response = generated[len(prompt):]
    else:
        # Fallback: try to find "### Response:\n" marker
        marker = "### Response:\n"
        idx = generated.rfind(marker)
        if idx != -1:
            response = generated[idx + len(marker):]
        else:
            response = generated
    # Stop at next ### section if present
    if "\n###" in response:
        response = response[:response.index("\n###")]
    return response.strip()


# ---------------------------------------------------------------------------
# Batched inference
# ---------------------------------------------------------------------------

def _batched(items: list, batch_size: int) -> Iterator[list]:
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def _translate_batch(
    model,
    tokenizer,
    english_cues: List[str],
    max_new_tokens: int,
    temperature: float,
    device: str,
) -> List[str]:
    """Translate a batch of English cues, return list of Spanish translations."""
    import torch

    prompts = [_build_prompt(en) for en in english_cues]
    inputs = tokenizer(
        prompts,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=256,
    ).to(device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=temperature > 0,
            pad_token_id=tokenizer.eos_token_id,
        )

    # Decode only the new tokens (after the input)
    input_lengths = inputs["input_ids"].shape[1]
    translations = []
    for i, out in enumerate(outputs):
        new_tokens = out[input_lengths:]
        decoded = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        translations.append(decoded)

    return translations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--source-model-path",
    required=True,
    help="Path to merged v2-17b-r1 model directory (HF format).",
)
@click.option(
    "--input",
    "input_path",
    required=True,
    type=click.Path(exists=True),
    help='Input JSONL with {"english": "..."} lines.',
)
@click.option(
    "--output",
    required=True,
    help="Output JSONL path for distilled pairs.",
)
@click.option(
    "--target-pairs",
    type=int,
    default=50_000,
    show_default=True,
    help="Maximum output pairs to produce.",
)
@click.option(
    "--batch-size",
    type=int,
    default=16,
    show_default=True,
    help="Inference batch size.",
)
@click.option(
    "--max-new-tokens",
    type=int,
    default=128,
    show_default=True,
    help="Maximum tokens to generate per translation.",
)
@click.option(
    "--temperature",
    type=float,
    default=0.1,
    show_default=True,
    help="Sampling temperature (0.1 for near-deterministic output).",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for sampling input if over target.",
)
def cli(
    source_model_path: str,
    input_path: str,
    output: str,
    target_pairs: int,
    batch_size: int,
    max_new_tokens: int,
    temperature: float,
    seed: int,
) -> None:
    """Translate unpaired English cues using v2-17b-r1 (WSL GPU only)."""

    # --- Check transformers/torch available ---
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        click.echo(
            "Install with: pip install torch transformers accelerate",
            err=True,
        )
        sys.exit(1)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    click.echo(f"Device: {device}", err=True)
    if device == "cpu":
        click.echo("WARNING: running on CPU — this will be very slow", err=True)

    # --- Load input cues ---
    cues = _read_input_jsonl(Path(input_path))
    click.echo(f"Loaded {len(cues):,} English cues from {input_path}", err=True)

    if len(cues) > target_pairs:
        rng = random.Random(seed)
        cues = rng.sample(cues, target_pairs)
        click.echo(f"Sampled {target_pairs:,} cues (seed={seed})", err=True)

    # --- Load model ---
    model_path = Path(source_model_path)
    if not model_path.exists():
        click.echo(f"ERROR: model path not found: {model_path}", err=True)
        sys.exit(1)

    click.echo(f"Loading model from {model_path} ...", err=True)
    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        str(model_path),
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    model.eval()
    click.echo("Model loaded.", err=True)

    # --- Translate in batches ---
    out_path = Path(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for batch_idx, batch in enumerate(_batched(cues, batch_size)):
            try:
                translations = _translate_batch(
                    model, tokenizer, batch, max_new_tokens, temperature, device
                )
            except Exception as exc:
                click.echo(f"WARNING: batch {batch_idx} failed: {exc}", err=True)
                skipped += len(batch)
                continue

            for en, es in zip(batch, translations):
                if not es.strip():
                    skipped += 1
                    continue
                record = {
                    "instruction": INSTRUCTION,
                    "input": en,
                    "output": es,
                    "source": "distilled",
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

            if (batch_idx + 1) % 50 == 0:
                done = (batch_idx + 1) * batch_size
                click.echo(
                    f"  {done:,}/{len(cues):,} cues processed | {written:,} written",
                    err=True,
                )

    click.echo("", err=True)
    click.echo("=== Distillation Stats ===", err=True)
    click.echo(f"  Input cues:     {len(cues):>8,}", err=True)
    click.echo(f"  Written pairs:  {written:>8,}", err=True)
    click.echo(f"  Skipped:        {skipped:>8,}", err=True)
    click.echo(f"  Written to: {output}", err=True)


if __name__ == "__main__":
    cli()
