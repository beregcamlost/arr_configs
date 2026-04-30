#!/usr/bin/env python3
"""phase5_gen_list.py — Generate the Phase 5 backfill worklist.

Reads audit_results_v3.json, finds all files with verdict BAD_EN_TAGGED_ES
or NO_ES_SUB that still lack a real .es.srt on disk, and writes them
one-per-line to phase5_remaining.txt.

Run before phase5_backfill.sh if the state file doesn't exist yet.

Usage:
    python3 phase5_gen_list.py [--out PATH] [--audit PATH]
"""
import argparse
import json
import os
import sys

DEFAULT_AUDIT = "/config/berenstuff/automation/logs/audit_results_v3.json"
DEFAULT_OUT = "/APPBOX_DATA/storage/.translation-state/phase5_remaining.txt"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", default=DEFAULT_AUDIT,
                        help=f"audit_results_v3.json path (default: {DEFAULT_AUDIT})")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"Output worklist path (default: {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print count but do not write the file")
    args = parser.parse_args()

    if not os.path.isfile(args.audit):
        print(f"ERROR: Audit file not found: {args.audit}", file=sys.stderr)
        sys.exit(1)

    with open(args.audit) as f:
        data = json.load(f)

    target_verdicts = {"BAD_EN_TAGGED_ES", "NO_ES_SUB"}
    remaining = []
    for item in data:
        if item.get("verdict") not in target_verdicts:
            continue
        video = item.get("video", "")
        if not video or not os.path.isfile(video):
            continue
        stem, _ = os.path.splitext(video)
        if os.path.isfile(stem + ".es.srt"):
            continue  # already fixed
        remaining.append(video)

    print(f"Files needing ES translation: {len(remaining)}")

    if args.dry_run:
        print("(dry-run — not writing)")
        return

    if os.path.exists(args.out):
        print(f"WARNING: {args.out} already exists. Overwrite? [y/N] ", end="", flush=True)
        if sys.stdin.readline().strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(remaining) + "\n")
    print(f"Written: {args.out} ({len(remaining)} entries)")


if __name__ == "__main__":
    main()
