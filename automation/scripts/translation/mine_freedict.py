"""Mine English-Spanish word/phrase pairs from FreeDict dictionaries.

Sources used (installed via apt):
  1. dict-freedict-eng-spa  →  /usr/share/dictd/freedict-eng-spa.dict.dz
     Format: plaintext dictd (gzip), ~5907 headwords
  2. GitHub TEI for spa-eng  →  downloaded to /tmp/spa-eng.tei (if present)
     Format: TEI XML (Spanish headwords, English translations — reversed for EN→ES)

Both sources are combined and deduped.  Output is JSONL in the training format:
  {"instruction": "...", "input": "<english>", "output": "<spanish>", "source": "freedict"}

Note: The Debian `freedict-eng-spa` package only ships ~5907 entries (dictd format),
not the 50-70k TEI-based corpus described in the original spec.  Combined with
spa-eng (reversed) the expected yield is 10-20k unique pairs.

Usage:
    python3 -m translation.mine_freedict --output /tmp/corpus-v22-layerB.jsonl
"""

import gzip
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Tuple
import xml.etree.ElementTree as ET

import click

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DICTD_ENG_SPA = Path("/usr/share/dictd/freedict-eng-spa.dict.dz")
DICTD_SPA_ENG = Path("/usr/share/dictd/freedict-spa-eng.dict.dz")
TEI_SPA_ENG_CACHE = Path("/tmp/spa-eng.tei")
TEI_SPA_ENG_URL = (
    "https://raw.githubusercontent.com/freedict/fd-dictionaries/master/"
    "spa-eng/spa-eng.tei"
)

TEI_NS = "http://www.tei-c.org/ns/1.0"

_RE_PHONETIC = re.compile(r"/[^/]*/")
_RE_NUM_PREFIX = re.compile(r"^\d+\.\s+")
_RE_SKIP_LINE = re.compile(
    r"^00database"
    r"|^[A-Z][a-z]+-[A-Z][a-z]+ FreeDict"  # "English-Spanish FreeDict..."
    r"|^http"
    r"|^Copyright"
    r"|^Maintainer"
    r"|^Edition"
    r"|^Size"
    r"|^Publisher"
    r"|^Availability"
    r"|^Available"
    r"|^Notes"
    r"|^Source"
    r"|^The Project"
    r"|^This "
    r"|^Changelog"
    r"|^\s*\*"
    r"|^\s+[A-Za-z]"  # indented prose (header block)
    r"|^Published"
    r"|^[A-Za-z][a-zA-Z-]+ FreeDict"
)


# ---------------------------------------------------------------------------
# dictd parser
# ---------------------------------------------------------------------------

def _parse_dictd(path: Path) -> Iterator[Tuple[str, str]]:
    """Yield (english, spanish) pairs from a freedict-eng-spa .dict.dz file.

    Format (after the preamble header):
      headword /phonetics/          ← ALWAYS has /phonetics/ delimiters
      translation(s)                ← 1-N lines; may be numbered "1. foo"
      next headword /phonetics/
      ...  (NO blank lines between entries)

    Headwords are detected by the presence of /phonetics/ delimiters.
    Translations are all lines between one headword and the next.
    Comma-separated alternatives on a single translation line are expanded.
    """
    try:
        with gzip.open(str(path), "rt", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        click.echo(f"WARNING: cannot read {path}: {exc}", err=True)
        return

    lines = raw.splitlines()

    # Find the start of entries: the line AFTER "FreeDict Dictionary ver. X.Y.Z"
    # followed by the URL line, then entries begin.
    entry_start = None
    for i, line in enumerate(lines):
        if re.search(r"FreeDict Dictionary ver\.", line):
            # Skip the version line and the URL line right after it
            j = i + 1
            if j < len(lines) and lines[j].startswith("http"):
                j += 1
            entry_start = j
            break

    if entry_start is None:
        # Fallback: no header found, treat whole file as entries
        entry_start = 0

    # Parse entries: group lines by headword
    # A headword line contains /phonetics/ (forward-slash delimited).
    headword: str | None = None
    pending_trans: list[str] = []

    def _flush(hw: str, trans_lines: list[str]) -> Iterator[Tuple[str, str]]:
        for trans_line in trans_lines:
            text = _RE_NUM_PREFIX.sub("", trans_line.strip())
            for part in text.split(","):
                part = _RE_NUM_PREFIX.sub("", part).strip()
                if part:
                    yield hw, part

    for line in lines[entry_start:]:
        stripped = line.strip()
        if not stripped:
            continue

        if _RE_PHONETIC.search(stripped):
            # New headword — flush pending translations first
            if headword and pending_trans:
                yield from _flush(headword, pending_trans)
            # Extract headword: strip phonetics
            headword = _RE_PHONETIC.sub("", stripped).strip()
            pending_trans = []
        elif headword:
            pending_trans.append(stripped)

    # Flush last entry
    if headword and pending_trans:
        yield from _flush(headword, pending_trans)


# ---------------------------------------------------------------------------
# TEI parser (spa-eng — reversed: headword is Spanish, trans is English)
# ---------------------------------------------------------------------------

def _parse_tei_spa_eng(path: Path) -> Iterator[Tuple[str, str]]:
    """Yield (english, spanish) pairs from the spa-eng TEI file.

    The file has Spanish headwords with English translations.
    We reverse: (english_translation, spanish_headword).
    """
    try:
        tree = ET.parse(str(path))
    except (OSError, ET.ParseError) as exc:
        click.echo(f"WARNING: cannot parse TEI {path}: {exc}", err=True)
        return

    root = tree.getroot()
    ns = {"t": TEI_NS}

    for entry in root.findall(".//t:entry", ns):
        # Spanish headword
        orth = entry.find(".//t:form/t:orth", ns)
        if orth is None or not orth.text:
            continue
        spanish = orth.text.strip()

        # English translations (one or more <cit type="trans"><quote>)
        for cit in entry.findall(".//t:cit[@type='trans']", ns):
            quote = cit.find("t:quote", ns)
            if quote is not None and quote.text:
                english = quote.text.strip()
                if english and spanish:
                    yield english, spanish


# ---------------------------------------------------------------------------
# Ensure TEI file is available
# ---------------------------------------------------------------------------

def _ensure_tei_spa_eng() -> Path | None:
    """Return path to spa-eng TEI, downloading if needed.  None on failure."""
    if TEI_SPA_ENG_CACHE.exists():
        return TEI_SPA_ENG_CACHE
    click.echo(f"Downloading spa-eng TEI from GitHub → {TEI_SPA_ENG_CACHE} ...", err=True)
    try:
        result = subprocess.run(
            ["curl", "-sf", "-o", str(TEI_SPA_ENG_CACHE), TEI_SPA_ENG_URL],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            click.echo(
                f"WARNING: curl failed (exit {result.returncode}); spa-eng TEI unavailable",
                err=True,
            )
            return None
        return TEI_SPA_ENG_CACHE
    except (OSError, subprocess.TimeoutExpired) as exc:
        click.echo(f"WARNING: cannot download TEI: {exc}", err=True)
        return None


# ---------------------------------------------------------------------------
# JSONL formatter
# ---------------------------------------------------------------------------

def _format_record(english: str, spanish: str) -> str:
    record = {
        "instruction": "Translate English word/phrase to Spanish.",
        "input": english,
        "output": spanish,
        "source": "freedict",
    }
    return json.dumps(record, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option(
    "--output",
    default=None,
    help="Output JSONL file path.  Defaults to stdout.",
)
@click.option(
    "--min-length",
    type=int,
    default=2,
    show_default=True,
    help="Minimum character length for both sides of a pair.",
)
def cli(output: str | None, min_length: int) -> None:
    """Mine English-Spanish word/phrase pairs from FreeDict dictionaries."""

    pairs: List[Tuple[str, str]] = []

    # --- Source 1: eng-spa dictd ---
    if DICTD_ENG_SPA.exists():
        eng_spa_pairs = list(_parse_dictd(DICTD_ENG_SPA))
        click.echo(f"eng-spa dictd: {len(eng_spa_pairs)} raw pairs", err=True)
        pairs.extend(eng_spa_pairs)
    else:
        click.echo(
            f"WARNING: {DICTD_ENG_SPA} not found.  "
            "Install with: sudo apt-get install -y dict-freedict-eng-spa",
            err=True,
        )

    # --- Source 2: spa-eng TEI (reversed) ---
    tei_path = _ensure_tei_spa_eng()
    if tei_path:
        tei_pairs = list(_parse_tei_spa_eng(tei_path))
        click.echo(f"spa-eng TEI (reversed): {tei_pairs and len(tei_pairs)} raw pairs", err=True)
        pairs.extend(tei_pairs)
    else:
        click.echo("Skipping spa-eng TEI source.", err=True)

    # --- Filter: min length ---
    filtered = [
        (en, es) for en, es in pairs
        if len(en) >= min_length and len(es) >= min_length
    ]
    click.echo(f"After min-length filter ({min_length}): {len(filtered)} pairs", err=True)

    # --- Dedup on (english, spanish) ---
    seen: set = set()
    deduped: List[Tuple[str, str]] = []
    for en, es in filtered:
        key = (en.lower(), es.lower())
        if key not in seen:
            seen.add(key)
            deduped.append((en, es))
    click.echo(f"After dedup: {len(deduped)} unique pairs", err=True)

    # --- Output ---
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for en, es in deduped:
                fh.write(_format_record(en, es) + "\n")
        click.echo(f"Output written to: {output}", err=True)
    else:
        for en, es in deduped:
            sys.stdout.write(_format_record(en, es) + "\n")


if __name__ == "__main__":
    cli()
