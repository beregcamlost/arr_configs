#!/usr/bin/env python3
"""sub_lang_sniff.py — Verify SRT content language matches expected tag.

Usage:
    sub_lang_sniff.py <srt_path> <expected_lang_2letter>

Output (one line on stdout):
    OK lang=<expected> p=<prob>
    WRONG_LANG expected=<exp> detected=<actual> p=<prob>
    UNCERTAIN
    EMPTY

Exit codes:
    0  OK
    1  WRONG_LANG
    2  UNCERTAIN / EMPTY
    3  IO error
"""
import os
import re
import sys

EXPECTED_MIN_PROB = float(os.environ.get("SUB_LANG_SNIFF_EXPECTED_MIN", "0.70"))
WRONG_MIN_PROB = float(os.environ.get("SUB_LANG_SNIFF_WRONG_MIN", "0.60"))
MAX_CHARS = int(os.environ.get("SUB_LANG_SNIFF_MAX_CHARS", "12000"))
MIN_CHARS = int(os.environ.get("SUB_LANG_SNIFF_MIN_CHARS", "200"))


def clean_srt_text(raw: str) -> str:
    raw = re.sub(
        r"\d+\n\d{2}:\d{2}:\d{2}[,.]\d+\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d+.*\n",
        "", raw,
    )
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = re.sub(r"\{[^}]+\}", "", raw)
    raw = re.sub(r"\[[A-Z][A-Z\s]{1,30}\]", "", raw)
    raw = re.sub(r"♪[^\n]*", "", raw)
    raw = re.sub(r"^\d+\s*$", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n{2,}", "\n", raw)
    return raw.strip()[:MAX_CHARS]


def main():
    if len(sys.argv) != 3:
        print("usage: sub_lang_sniff.py <srt_path> <expected_lang_2letter>", file=sys.stderr)
        sys.exit(2)

    srt_path, expected = sys.argv[1], sys.argv[2].lower()
    try:
        raw = open(srt_path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"ERR io: {exc}", file=sys.stderr)
        sys.exit(3)

    text = clean_srt_text(raw)
    if len(text) < MIN_CHARS:
        print("EMPTY")
        sys.exit(2)

    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        langs = [(d.lang, d.prob) for d in detect_langs(text)]
    except Exception as exc:
        print(f"UNCERTAIN err={exc.__class__.__name__}", file=sys.stderr)
        sys.exit(2)

    expected_p = next((p for l, p in langs if l == expected), 0.0)
    if expected_p >= EXPECTED_MIN_PROB:
        print(f"OK lang={expected} p={expected_p:.3f}")
        sys.exit(0)

    others = sorted(((l, p) for l, p in langs if l != expected), key=lambda x: -x[1])
    if others and others[0][1] >= WRONG_MIN_PROB:
        l, p = others[0]
        print(f"WRONG_LANG expected={expected} detected={l} p={p:.3f}")
        sys.exit(1)

    print(f"UNCERTAIN expected_p={expected_p:.3f} top={others[:2]}")
    sys.exit(2)


if __name__ == "__main__":
    main()
