#!/usr/bin/env python3
# automation/scripts/translation/translator.py
"""DeepL subtitle translator CLI."""

import logging
import os
import sys

import click

from translation.config import (
    Config, load_config, DEEPL_LANG_MAP, DEEPL_SOURCE_LANG_MAP,
)
from translation.db import (
    init_db, record_translation, is_on_cooldown, get_monthly_chars,
    get_recent_translations,
)
from translation.deepl_client import create_translator, translate_srt_cues
from translation.discord import notify_translations, notify_quota_warning
from translation.srt_parser import parse_srt, write_srt
from translation.subtitle_scanner import (
    find_best_source_srt, find_missing_langs_on_disk, get_profile_langs,
    parse_missing_subtitles, scan_recent_missing,
)

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _db_path(state_dir: str) -> str:
    return os.path.join(state_dir, "translation_state.db")


def translate_file(cfg: Config, translator, media_path: str, chars_remaining=None):
    """Translate missing subtitle languages for a single media file.

    Args:
        chars_remaining: If set, stop after consuming this many chars.
            Returns updated remaining count via the chars field in results.

    Returns (list of {file, target, chars}, list of {file, target, error}).
    """
    db_path = _db_path(cfg.state_dir)
    basename = os.path.basename(media_path)
    stem = os.path.splitext(basename)[0]
    directory = os.path.dirname(media_path)

    # Resolve profile for this file
    profile_id = _resolve_profile_for_path(cfg.bazarr_db, media_path)
    if profile_id is None:
        log.info("No profile found for %s, skipping", basename)
        return [], []

    profile_langs = get_profile_langs(cfg.bazarr_db, profile_id)
    if not profile_langs:
        log.info("Empty profile for %s, skipping", basename)
        return [], []

    missing = find_missing_langs_on_disk(directory, stem, profile_langs)
    if not missing:
        log.info("All profile langs present for %s", basename)
        return [], []

    translated = []
    failed = []

    for target_lang in missing:
        # Skip forced/hi variants for translation
        base_lang = target_lang.split(":")[0]

        if base_lang not in DEEPL_LANG_MAP:
            log.info("No DeepL mapping for '%s', skipping", base_lang)
            continue

        if is_on_cooldown(db_path, media_path, base_lang):
            log.info("Cooldown active for %s -> %s, skipping", basename, base_lang)
            continue

        if chars_remaining is not None and chars_remaining <= 0:
            log.info("Max chars reached, stopping")
            break

        source_srt = find_best_source_srt(directory, stem, base_lang)
        if not source_srt:
            log.info("No source SRT for %s -> %s", basename, base_lang)
            record_translation(db_path, media_path, "?", base_lang, 0, "no_source")
            continue

        # Detect source language from filename
        source_basename = os.path.basename(source_srt)
        source_lang_code = source_basename[len(stem) + 1:].split(".")[0].lower()
        deepl_source = DEEPL_SOURCE_LANG_MAP.get(source_lang_code)
        deepl_target = DEEPL_LANG_MAP[base_lang]

        if not deepl_source:
            log.info("No DeepL source mapping for '%s'", source_lang_code)
            continue

        try:
            # Read and parse source SRT
            with open(source_srt, "r", encoding="utf-8", errors="replace") as f:
                source_content = f.read()
            cues = parse_srt(source_content)
            if not cues:
                log.warning("Empty SRT: %s", source_srt)
                continue

            # Translate
            log.info("Translating %s: %s -> %s (%d cues)",
                     basename, source_lang_code, base_lang, len(cues))
            translated_cues, chars_used = translate_srt_cues(
                translator, cues, deepl_source, deepl_target
            )

            # Write translated SRT + marker for deferred muxing
            output_path = os.path.join(directory, f"{stem}.{base_lang}.srt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(write_srt(translated_cues))
            # Create .deepl marker so auto-maintain defers muxing
            open(output_path + ".deepl", "w").close()

            record_translation(db_path, media_path, source_lang_code,
                               base_lang, chars_used, "success")
            translated.append({
                "file": basename, "target": base_lang, "chars": chars_used
            })
            if chars_remaining is not None:
                chars_remaining -= chars_used
            log.info("Wrote %s (%d chars)", output_path, chars_used)

        except Exception as e:
            error_msg = str(e)
            # Check for quota exceeded
            if "quota" in error_msg.lower() or "456" in error_msg:
                record_translation(db_path, media_path, source_lang_code,
                                   base_lang, 0, "quota_exceeded")
                failed.append({"file": basename, "target": base_lang,
                               "error": "quota exceeded"})
                raise  # Re-raise to stop all processing
            record_translation(db_path, media_path, source_lang_code,
                               base_lang, 0, f"error: {error_msg[:100]}")
            failed.append({"file": basename, "target": base_lang,
                           "error": error_msg[:100]})
            log.error("Translation failed %s -> %s: %s", basename, base_lang, e)

    return translated, failed


def _resolve_profile_for_path(bazarr_db: str, media_path: str):
    """Resolve Bazarr profileId for a media file path."""
    import sqlite3
    if not os.path.isfile(bazarr_db):
        return None
    conn = sqlite3.connect(bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")

    # Try episodes first
    cursor = conn.execute(
        """SELECT s.profileId FROM table_episodes e
           JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
           WHERE e.path = ? LIMIT 1""",
        (media_path,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

    # Try movies
    cursor = conn.execute(
        "SELECT profileId FROM table_movies WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

    # Fallback: directory name match for series
    parent_dir = os.path.basename(os.path.dirname(os.path.dirname(media_path)))
    if parent_dir:
        cursor = conn.execute(
            "SELECT profileId FROM table_shows WHERE path LIKE ? LIMIT 1",
            (f"%{parent_dir}%",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]

    # Fallback: directory name match for movies
    movie_dir = os.path.basename(os.path.dirname(media_path))
    if movie_dir:
        cursor = conn.execute(
            "SELECT profileId FROM table_movies WHERE path LIKE ? LIMIT 1",
            (f"%{movie_dir}%",),
        )
        row = cursor.fetchone()
        if row and row[0]:
            conn.close()
            return row[0]

    conn.close()
    return None


@click.group()
def cli():
    """DeepL subtitle translator."""
    pass


@cli.command()
@click.option("--since", type=int, default=None,
              help="Only process files modified in the last N minutes")
@click.option("--file", "file_path", type=str, default=None,
              help="Translate a single file")
@click.option("--max-chars", type=int, default=None,
              help="Maximum characters to translate in this run")
@click.option("--state-dir", type=str, default=None)
@click.option("--bazarr-db", type=str, default=None)
def translate(since, file_path, max_chars, state_dir, bazarr_db):
    """Translate missing subtitles via DeepL."""
    cfg = load_config(bazarr_db=bazarr_db, state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)
    translator = create_translator(cfg.deepl_api_key)

    chars_remaining = max_chars
    all_translated = []
    all_failed = []

    if file_path:
        # Single-file mode (import hook)
        if not os.path.isfile(file_path):
            log.error("File not found: %s", file_path)
            return
        t, f = translate_file(cfg, translator, file_path, chars_remaining)
        all_translated.extend(t)
        all_failed.extend(f)

        # Trigger Bazarr rescan
        if all_translated and cfg.bazarr_api_key:
            _trigger_bazarr_rescan(cfg, file_path)
    elif since:
        # Cron mode — scan for recent missing
        results = scan_recent_missing(cfg.bazarr_db, since)
        if not results:
            click.echo(f"No files with missing subtitles in last {since} minutes")
            return
        click.echo(f"Found {len(results)} file(s) with missing subtitles")
        for item in results:
            if chars_remaining is not None and chars_remaining <= 0:
                log.info("Max chars reached, stopping batch")
                break
            try:
                t, f = translate_file(cfg, translator, item["path"], chars_remaining)
                all_translated.extend(t)
                all_failed.extend(f)
                if chars_remaining is not None:
                    chars_remaining -= sum(x["chars"] for x in t)
                if t and cfg.bazarr_api_key:
                    _trigger_bazarr_rescan(cfg, item["path"])
            except Exception as e:
                if "quota" in str(e).lower():
                    log.error("Quota exceeded, stopping")
                    monthly = get_monthly_chars(db)
                    notify_quota_warning(cfg.discord_webhook_url, monthly, 500000)
                    break
                log.error("Error processing %s: %s", item["path"], e)
    else:
        click.echo("Must specify --since N or --file PATH")
        return

    # Summary
    total_chars = sum(t["chars"] for t in all_translated)
    monthly_chars = get_monthly_chars(db)
    click.echo(
        f"Done: {len(all_translated)} translated, {len(all_failed)} failed, "
        f"{total_chars:,} chars used ({monthly_chars:,} this month)"
    )

    # Discord notification
    if all_translated or all_failed:
        notify_translations(
            cfg.discord_webhook_url,
            all_translated, all_failed,
            total_chars, monthly_chars,
        )


@cli.command()
@click.option("--state-dir", type=str, default=None)
def status(state_dir):
    """Show translation status and recent activity."""
    cfg = load_config(state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)

    monthly = get_monthly_chars(db)
    click.echo(f"Monthly usage: {monthly:,} / 500,000 chars ({monthly/5000:.1f}%)")

    recent = get_recent_translations(db, limit=10)
    if recent:
        click.echo(f"\nRecent translations ({len(recent)}):")
        for r in recent:
            click.echo(
                f"  {r['created_at']} | {r['status']:15s} | "
                f"{r['source_lang']}->{r['target_lang']} | "
                f"{r['chars_used']:>6,} chars | {os.path.basename(r['media_path'])}"
            )
    else:
        click.echo("\nNo translations recorded yet.")


@cli.command()
def usage():
    """Query DeepL API for remaining quota."""
    cfg = load_config()
    translator = create_translator(cfg.deepl_api_key)
    usage_data = translator.get_usage()
    if usage_data.character:
        count = usage_data.character.count
        limit = usage_data.character.limit
        click.echo(f"DeepL API usage: {count:,} / {limit:,} chars ({count/limit*100:.1f}%)")
    else:
        click.echo("Could not retrieve usage data")


def _trigger_bazarr_rescan(cfg: Config, media_path: str):
    """Trigger Bazarr scan-disk for the file's series/movie."""
    import requests as req
    import sqlite3

    headers = {"X-API-KEY": cfg.bazarr_api_key}
    conn = sqlite3.connect(cfg.bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")

    # Check if it's a series episode
    cursor = conn.execute(
        "SELECT sonarrSeriesId FROM table_episodes WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    if row:
        series_id = row[0]
        conn.close()
        try:
            req.post(
                f"{cfg.bazarr_url}/api/series/action",
                headers=headers,
                json={"seriesid": series_id, "action": "scan-disk"},
                timeout=30,
            )
        except Exception as e:
            log.warning("Bazarr series rescan failed: %s", e)
        return

    # Check if it's a movie
    cursor = conn.execute(
        "SELECT radarrId FROM table_movies WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        movie_id = row[0]
        try:
            req.post(
                f"{cfg.bazarr_url}/api/movies/action",
                headers=headers,
                json={"radarrid": movie_id, "action": "scan-disk"},
                timeout=30,
            )
        except Exception as e:
            log.warning("Bazarr movie rescan failed: %s", e)


if __name__ == "__main__":
    cli()
