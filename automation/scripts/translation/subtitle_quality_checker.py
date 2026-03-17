"""Gemini-based subtitle content quality checker.

3rd-line defense for wrong-language/garbage subtitles that structural checks
can't catch.  Samples cues from the middle 60% of the file, asks Gemini to
evaluate language, translation quality, and encoding integrity, and returns a
GOOD / WARN / BAD / SKIP verdict.

Usage:
    python3 -m translation.subtitle_quality_checker check \\
        --srt /tmp/file.srt --expected-lang es

    python3 -m translation.subtitle_quality_checker batch \\
        --dir /media/tv/Show --expected-lang es --max-calls 10
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from translation.srt_parser import Cue, parse_srt
from translation.config import GEMINI_LANG_MAP

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_SAMPLE_COUNT = 10
DEFAULT_STATE_DB = "/APPBOX_DATA/storage/.subtitle-quality-state/subtitle_quality_state.db"

# Session-scoped set of exhausted API keys (mirrors gemini_client pattern)
_exhausted_keys: set = set()


def _skip(reason: str) -> dict:
    """Return a SKIP result dict — used when a quality check cannot proceed."""
    return {"quality": "SKIP", "confidence": 0.0, "actual_lang": "", "reason": reason}


def _bad(reason: str) -> dict:
    """Return a BAD result dict — used when a file is structurally unacceptable."""
    return {"quality": "BAD", "confidence": 1.0, "actual_lang": "", "reason": reason}


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample_cues(cues: list, count: int = DEFAULT_SAMPLE_COUNT) -> list:
    """Sample up to *count* cues from the middle 60% of the file.

    Avoids watermark-heavy start/end sections that Gemini may fixate on.
    Returns an empty list when *cues* is empty.
    """
    if not cues:
        return []

    n = len(cues)
    start_idx = int(n * 0.2)
    end_idx = int(n * 0.8)

    if end_idx <= start_idx:
        middle = cues
    else:
        middle = cues[start_idx:end_idx]

    if len(middle) <= count:
        return list(middle)

    # Evenly-spaced samples across the middle window
    step = len(middle) / count
    return [middle[int(i * step)] for i in range(count)]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_quality_prompt(cues: list, expected_lang: str) -> str:
    """Build the Gemini prompt for quality checking."""
    lang_name = GEMINI_LANG_MAP.get(expected_lang, expected_lang)

    lines = []
    for i, cue in enumerate(cues, 1):
        lines.append(f"{i}. [{cue.start}] {cue.text}")

    cue_text = "\n".join(lines)

    return (
        f"Analyze these subtitle cues that should be in {lang_name}.\n\n"
        f"{cue_text}\n\n"
        f"Answer these 3 questions:\n"
        f"1. LANGUAGE: Is this actually {lang_name}? (yes/no, and what language it actually is)\n"
        f"2. QUALITY: Is this human-quality translation or machine garbage/random text? (human/garbage)\n"
        f"3. ENCODING: Is the text readable or does it have encoding artifacts? (readable/corrupted)\n\n"
        f"Then provide a final verdict as a single JSON object on the last line:\n"
        f'{{"quality": "GOOD|WARN|BAD", "confidence": 0.0-1.0, "actual_lang": "xx", "reason": "brief explanation"}}\n'
        f"GOOD = correct language, human quality, readable\n"
        f"WARN = minor issues (slightly different dialect, some odd phrasing)\n"
        f"BAD = wrong language, garbage, or unreadable"
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_quality_response(response_text: str) -> Optional[dict]:
    """Parse Gemini's quality assessment response.

    Finds the last JSON object in the response and validates that it contains
    a *quality* field with one of GOOD / WARN / BAD.

    Returns a normalised dict or *None* when the response cannot be parsed.
    """
    lines = response_text.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                result = json.loads(line)
                quality = result.get("quality", "").upper()
                if quality not in ("GOOD", "WARN", "BAD"):
                    continue
                return {
                    "quality": quality,
                    "confidence": float(result.get("confidence", 0.0)),
                    "actual_lang": result.get("actual_lang", ""),
                    "reason": result.get("reason", ""),
                }
            except (json.JSONDecodeError, ValueError):
                continue

    return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ensure_quality_table(conn: sqlite3.Connection):
    """Create the quality_checks table if it does not exist yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_checks (
            srt_path    TEXT NOT NULL,
            srt_mtime   INTEGER NOT NULL,
            expected_lang TEXT NOT NULL,
            actual_lang TEXT NOT NULL DEFAULT '',
            quality     TEXT NOT NULL,
            reason      TEXT NOT NULL DEFAULT '',
            confidence  REAL NOT NULL DEFAULT 0.0,
            checked_ts  INTEGER NOT NULL,
            provider    TEXT NOT NULL DEFAULT 'gemini',
            PRIMARY KEY (srt_path, expected_lang, srt_mtime)
        )
    """)
    conn.commit()


def check_cache(db_path: str, srt_path: str, srt_mtime: int,
                expected_lang: str) -> Optional[dict]:
    """Return a cached quality result or *None* on miss / error."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 30000")
        _ensure_quality_table(conn)
        row = conn.execute(
            "SELECT quality, confidence, actual_lang, reason FROM quality_checks "
            "WHERE srt_path = ? AND expected_lang = ? AND srt_mtime = ?",
            (srt_path, expected_lang, srt_mtime),
        ).fetchone()
        conn.close()
        if row:
            return {
                "quality": row[0],
                "confidence": row[1],
                "actual_lang": row[2],
                "reason": row[3],
            }
    except sqlite3.Error:
        pass
    return None


def save_cache(db_path: str, srt_path: str, srt_mtime: int,
               expected_lang: str, result: dict):
    """Persist a quality check result; silently ignores DB errors."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 30000")
        _ensure_quality_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO quality_checks "
            "(srt_path, srt_mtime, expected_lang, actual_lang, quality, reason, "
            " confidence, checked_ts, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'gemini')",
            (
                srt_path,
                srt_mtime,
                expected_lang,
                result.get("actual_lang", ""),
                result["quality"],
                result.get("reason", ""),
                result.get("confidence", 0.0),
                int(time.time()),
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        log.warning("Failed to cache quality check: %s", e)


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_subtitle_quality(
    srt_path: str,
    expected_lang: str,
    api_keys: list,
    model: str = DEFAULT_MODEL,
    state_db: Optional[str] = None,
) -> dict:
    """Check subtitle content quality using Gemini.

    Returns a dict with keys: quality (GOOD/WARN/BAD/SKIP), confidence,
    actual_lang, reason.

    Never raises — on any API/IO failure returns a SKIP result so that callers
    never penalise a file solely because of an API outage.
    """
    # Read and parse
    try:
        content = Path(srt_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError) as e:
        return _skip(f"read error: {e}")

    cues = parse_srt(content)

    # Early exits that don't require an API call
    if not cues:
        return _bad("empty file (0 cues)")
    if len(cues) < 5:
        return _bad(f"too few cues ({len(cues)})")

    # Cache lookup
    srt_mtime = 0
    if state_db:
        try:
            srt_mtime = int(os.path.getmtime(srt_path))
        except OSError:
            srt_mtime = 0
        cached = check_cache(state_db, srt_path, srt_mtime, expected_lang)
        if cached:
            log.debug("Cache hit for %s", srt_path)
            return cached

    if not api_keys:
        return _skip("no API keys available")

    available = [k for k in api_keys if k not in _exhausted_keys]
    if not available:
        return _skip("all API keys exhausted")

    sampled = sample_cues(cues)
    prompt = build_quality_prompt(sampled, expected_lang)

    for api_key in available:
        try:
            genai.configure(api_key=api_key)
            model_obj = genai.GenerativeModel(
                model_name=model,
                system_instruction=(
                    "You are a subtitle quality analyst. Analyze subtitle cues for "
                    "language correctness, translation quality, and encoding integrity. "
                    "Always end your response with a JSON verdict line."
                ),
            )
            response = model_obj.generate_content(prompt)
            result = parse_quality_response(response.text)

            if result is None:
                log.warning("Unparseable Gemini response for %s", srt_path)
                return _skip("unparseable response")

            if state_db:
                save_cache(state_db, srt_path, srt_mtime, expected_lang, result)

            return result

        except ResourceExhausted:
            log.warning(
                "Gemini key %s...%s exhausted", api_key[:8], api_key[-4:]
            )
            _exhausted_keys.add(api_key)
            continue
        except Exception as e:
            log.warning("Gemini error for %s: %s", srt_path, e)
            return _skip(f"API error: {e}")

    return _skip("all API keys exhausted")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Subtitle content quality checker using Gemini"
    )
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Check a single SRT file")
    check_p.add_argument("--srt", required=True, help="Path to SRT file")
    check_p.add_argument(
        "--expected-lang", required=True, help="Expected 2-letter language code"
    )
    check_p.add_argument(
        "--state-db", default=None, help="State DB path for caching"
    )
    check_p.add_argument(
        "--model", default=DEFAULT_MODEL, help="Gemini model name"
    )

    batch_p = sub.add_parser("batch", help="Check all SRT files in a directory")
    batch_p.add_argument("--dir", required=True, help="Directory to scan")
    batch_p.add_argument(
        "--expected-lang", required=True, help="Expected 2-letter language code"
    )
    batch_p.add_argument(
        "--state-db", default=None, help="State DB path for caching"
    )
    batch_p.add_argument(
        "--max-calls",
        type=int,
        default=0,
        help="Max API calls (0 = unlimited)",
    )
    batch_p.add_argument(
        "--model", default=DEFAULT_MODEL, help="Gemini model name"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    api_keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",") if k.strip()]

    if args.command == "check":
        result = check_subtitle_quality(
            args.srt,
            args.expected_lang,
            api_keys,
            model=args.model,
            state_db=args.state_db,
        )
        print(
            f"{args.srt}\t{result['quality']}\t"
            f"{result['confidence']:.2f}\t{result.get('reason', '')}"
        )

    elif args.command == "batch":
        srt_dir = Path(args.dir)
        calls = 0
        for srt_file in sorted(srt_dir.rglob(f"*.{args.expected_lang}*.srt")):
            if args.max_calls and calls >= args.max_calls:
                log.info("Max calls (%d) reached, stopping", args.max_calls)
                break
            result = check_subtitle_quality(
                str(srt_file),
                args.expected_lang,
                api_keys,
                model=args.model,
                state_db=args.state_db,
            )
            print(
                f"{srt_file}\t{result['quality']}\t"
                f"{result['confidence']:.2f}\t{result.get('reason', '')}"
            )
            if result["quality"] != "SKIP":
                calls += 1


if __name__ == "__main__":
    main()
