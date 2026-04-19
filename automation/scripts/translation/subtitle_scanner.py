"""Find missing subtitles and source SRTs for translation."""

import ast
import glob
import json
import logging
import os
import re
import sqlite3
import time
from typing import List, Optional

log = logging.getLogger(__name__)


def parse_missing_subtitles(raw: Optional[str]) -> List[str]:
    """Parse Bazarr's missing_subtitles field (Python repr or JSON)."""
    if not raw or raw.strip() in ("", "[]"):
        return []
    raw = raw.strip()
    try:
        result = ast.literal_eval(raw)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (ValueError, SyntaxError):
        pass
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(x) for x in result]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _is_dialogue_srt(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False
    cues = [c for c in re.split(r"\n\n+", content.strip()) if c.strip()]
    if not cues:
        return False
    if len(cues) > 50:
        step = len(cues) / 50
        cues = [cues[int(i * step)] for i in range(50)]
    tagged = 0
    total_clean_chars = 0
    for cue in cues:
        lines = cue.splitlines()
        text_lines = lines[2:] if len(lines) > 2 else []
        text = " ".join(text_lines)
        has_tag = bool(re.search(r"<font|\{\\", text))
        if has_tag:
            tagged += 1
        clean = re.sub(r"<[^>]+>|\{[^}]+\}", "", text)
        total_clean_chars += len(clean.strip())
    tag_ratio = tagged / len(cues)
    avg_clean = total_clean_chars / len(cues)
    return not (tag_ratio > 0.5 and avg_clean < 20)


def find_best_source_srt(
    directory: str, stem: str, target_lang: str
) -> Optional[str]:
    """Find the best source SRT for translation.

    Picks the largest non-forced, non-target-language SRT.
    Prefers English when within 20% of the largest candidate.
    """
    pattern = os.path.join(glob.escape(directory), f"{glob.escape(stem)}.*.srt")
    candidates = []

    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        # Extract language code: stem.LANG.srt or stem.LANG.forced.srt
        parts = basename[len(stem) + 1:].split(".")
        if not parts:
            continue
        lang = parts[0].lower()
        if lang == target_lang.lower():
            continue
        if "forced" in [p.lower() for p in parts]:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == 0:
            continue
        candidates.append((path, lang, size))

    candidates = [(p, l, s) for p, l, s in candidates if _is_dialogue_srt(p)]

    if not candidates:
        return None

    # Sort by size descending
    candidates.sort(key=lambda x: x[2], reverse=True)
    largest_size = candidates[0][2]

    # Prefer English if within 20% of largest
    for path, lang, size in candidates:
        if lang == "en" and size >= largest_size * 0.8:
            return path

    return candidates[0][0]


def find_missing_langs_on_disk(
    directory: str, stem: str, profile_langs: List[str]
) -> List[str]:
    """Return profile languages that don't have an SRT on disk."""
    missing = []
    for lang in profile_langs:
        srt_path = os.path.join(directory, f"{stem}.{lang}.srt")
        if not os.path.isfile(srt_path):
            missing.append(lang)
    return missing


def get_profile_langs(bazarr_db: str, profile_id: int) -> List[str]:
    """Get language codes from a Bazarr language profile."""
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT items FROM table_languages_profiles WHERE profileId=? LIMIT 1",
        (profile_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row or not row[0]:
        return []
    try:
        items = json.loads(row[0])
        return [item["language"] for item in items
                if item.get("forced", "False") != "True"]
    except (json.JSONDecodeError, KeyError):
        return []


def scan_recent_missing(bazarr_db: str, since_minutes=None) -> list:
    """Scan Bazarr DB for media with missing subtitles and recent mtime.

    Returns list of dicts: {path, media_type, media_id, profile_id, missing_subtitles}
    """
    results = []
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    # Episodes
    cursor = conn.execute("""
        SELECT e.path, e.missing_subtitles, e.sonarrSeriesId, s.profileId
        FROM table_episodes e
        JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
        WHERE e.missing_subtitles != '[]'
          AND e.missing_subtitles IS NOT NULL
          AND e.path IS NOT NULL
          AND s.profileId IS NOT NULL
    """)
    for row in cursor.fetchall():
        path = row["path"]
        if not os.path.isfile(path):
            continue
        if since_minutes is not None:
            try:
                age_minutes = (time.time() - os.path.getmtime(path)) / 60
                if age_minutes > since_minutes:
                    continue
            except OSError:
                continue
        results.append({
            "path": path,
            "media_type": "series",
            "media_id": row["sonarrSeriesId"],
            "profile_id": row["profileId"],
            "missing_subtitles": row["missing_subtitles"],
        })

    # Movies
    cursor = conn.execute("""
        SELECT m.path, m.missing_subtitles, m.radarrId, m.profileId
        FROM table_movies m
        WHERE m.missing_subtitles != '[]'
          AND m.missing_subtitles IS NOT NULL
          AND m.path IS NOT NULL
          AND m.profileId IS NOT NULL
    """)
    for row in cursor.fetchall():
        path = row["path"]
        if not os.path.isfile(path):
            continue
        if since_minutes is not None:
            try:
                age_minutes = (time.time() - os.path.getmtime(path)) / 60
                if age_minutes > since_minutes:
                    continue
            except OSError:
                continue
        results.append({
            "path": path,
            "media_type": "movie",
            "media_id": row["radarrId"],
            "profile_id": row["profileId"],
            "missing_subtitles": row["missing_subtitles"],
        })

    conn.close()
    return results
