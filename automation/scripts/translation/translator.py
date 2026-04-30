#!/usr/bin/env python3
# automation/scripts/translation/translator.py
"""Subtitle translator CLI — Ollama-only (DeepL/Gemini/Google removed 2026-04-29)."""

import logging
import os
import sys

import click

from translation.config import (
    Config, load_config,
    ALL_SUPPORTED_LANGS, ALL_SUPPORTED_SOURCE_LANGS,
    OLLAMA_LANG_MAP,
    PROVIDER_OLLAMA,
)
from translation.db import (
    init_db, record_translation, is_on_cooldown, is_permanently_failed,
    get_monthly_chars, get_monthly_chars_by_provider, get_recent_translations,
)
from translation.discord import notify_translations
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

# Session-level flag: once Ollama is down, skip it for the rest of the run.
_ollama_unavailable = False

try:
    from translation.ollama_client import OllamaUnavailable as _OllamaUnavailable
except ImportError:  # pragma: no cover — only fails if ollama_client missing
    class _OllamaUnavailable(Exception):  # type: ignore[no-redef]
        pass

MARKER_EXTENSIONS = {
    PROVIDER_OLLAMA: ".ollama",
    "ollama+gemini": ".ollama",  # backward-compat: old marker name still .ollama
}


def _db_path(state_dir: str) -> str:
    return os.path.join(state_dir, "translation_state.db")


def _has_ollama(cfg: Config) -> bool:
    """Check if Ollama is available (URL configured and not marked unavailable)."""
    return bool(cfg.ollama_base_url) and not _ollama_unavailable


def _translate_cues_with_ollama(cfg, cues, source_lang_code, base_lang):
    """Translate cues using Ollama only.

    Returns (translated_cues, chars_used, provider, key_index).
    Raises OllamaUnavailable on connection failure.
    Raises ValueError when Ollama URL is not configured.
    """
    global _ollama_unavailable

    if not _has_ollama(cfg):
        raise ValueError(f"Ollama not available for {source_lang_code} -> {base_lang}")

    if base_lang not in OLLAMA_LANG_MAP or source_lang_code not in OLLAMA_LANG_MAP:
        raise ValueError(f"No Ollama language mapping for {source_lang_code} -> {base_lang}")

    from translation.ollama_client import (
        translate_srt_cues as ollama_translate_srt_cues,
        OllamaUnavailable,
    )
    ollama_source = OLLAMA_LANG_MAP[source_lang_code]
    ollama_target = OLLAMA_LANG_MAP[base_lang]

    try:
        translated_cues, chars_used, _ = ollama_translate_srt_cues(
            cfg.ollama_base_url, cues, ollama_source, ollama_target,
            model=cfg.ollama_model,
        )
        return translated_cues, chars_used, PROVIDER_OLLAMA, None
    except OllamaUnavailable as e:
        log.warning("Ollama unavailable: %s", e)
        _ollama_unavailable = True
        raise


def translate_file(cfg: Config, media_path: str, chars_remaining=None):
    """Translate missing subtitle languages for a single media file.

    Returns (list of {file, target, chars, provider}, list of {file, target, error}).
    """
    db_path = _db_path(cfg.state_dir)

    basename = os.path.basename(media_path)
    stem = os.path.splitext(basename)[0]
    directory = os.path.dirname(media_path)

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
        base_lang = target_lang.split(":")[0]

        if base_lang not in ALL_SUPPORTED_LANGS:
            log.info("No translation mapping for '%s', skipping", base_lang)
            continue

        if is_permanently_failed(db_path, media_path, base_lang):
            log.info(
                "SKIP_PERMANENT: %s (%s) — prior NoneType parse failure",
                media_path, base_lang,
            )
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
            record_translation(db_path, media_path, "?", base_lang, 0, "no_source", PROVIDER_OLLAMA)
            continue

        source_basename = os.path.basename(source_srt)
        source_lang_code = source_basename[len(stem) + 1:].split(".")[0].lower()

        if source_lang_code not in ALL_SUPPORTED_SOURCE_LANGS:
            log.info("No source mapping for '%s'", source_lang_code)
            continue

        try:
            with open(source_srt, "r", encoding="utf-8", errors="replace") as f:
                source_content = f.read()
            cues = parse_srt(source_content)
            if not cues:
                log.warning("Empty SRT: %s", source_srt)
                continue

            log.info("Translating %s: %s -> %s (%d cues)",
                     basename, source_lang_code, base_lang, len(cues))
            translated_cues, chars_used, provider, key_index = _translate_cues_with_ollama(
                cfg, cues, source_lang_code, base_lang,
            )

            output_path = os.path.join(directory, f"{stem}.{base_lang}.srt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(write_srt(translated_cues))
            marker_ext = MARKER_EXTENSIONS.get(provider, ".ollama")
            open(output_path + marker_ext, "w").close()

            record_translation(db_path, media_path, source_lang_code,
                               base_lang, chars_used, "success", provider,
                               key_index=key_index)
            translated.append({
                "file": basename, "target": base_lang,
                "chars": chars_used, "provider": provider,
            })
            if chars_remaining is not None:
                chars_remaining -= chars_used
            log.info("Wrote %s (%d chars, %s)", output_path, chars_used, provider)

        except _OllamaUnavailable as e:
            global _ollama_unavailable
            _ollama_unavailable = True
            error_msg = str(e)
            record_translation(db_path, media_path, source_lang_code,
                               base_lang, 0, f"error: {error_msg[:100]}", PROVIDER_OLLAMA)
            failed.append({"file": basename, "target": base_lang,
                           "error": error_msg[:100]})
            log.error("Ollama unavailable, aborting remaining files in this run: %s", e)
            break
        except Exception as e:
            error_msg = str(e)
            record_translation(db_path, media_path, source_lang_code,
                               base_lang, 0, f"error: {error_msg[:100]}", PROVIDER_OLLAMA)
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

    cursor = conn.execute(
        "SELECT profileId FROM table_movies WHERE path = ? LIMIT 1",
        (media_path,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        conn.close()
        return row[0]

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
    """Subtitle translator — Ollama only."""
    pass


@cli.command()
@click.option("--since", type=int, default=None,
              help="Only process files modified in the last N minutes")
@click.option("--all", "scan_all", is_flag=True, default=False,
              help="Translate all files with missing subtitles")
@click.option("--file", "file_path", type=str, default=None,
              help="Translate a single file")
@click.option("--max-chars", type=int, default=None,
              help="Maximum characters to translate in this run")
@click.option("--state-dir", type=str, default=None)
@click.option("--bazarr-db", type=str, default=None)
@click.option("--max-files", type=int, default=None,
              help="Max files to process in this run (applied after scan)")
def translate(since, scan_all, file_path, max_chars, state_dir, bazarr_db, max_files):
    """Translate missing subtitles via Ollama."""
    global _ollama_unavailable
    _ollama_unavailable = False

    cfg = load_config(bazarr_db=bazarr_db, state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)

    if not cfg.ollama_base_url:
        click.echo("Error: OLLAMA_BASE_URL not set — no translation provider available")
        sys.exit(1)

    chars_remaining = max_chars
    all_translated = []
    all_failed = []

    if file_path:
        if not os.path.isfile(file_path):
            log.error("File not found: %s", file_path)
            return
        t, f = translate_file(cfg, file_path, chars_remaining)
        all_translated.extend(t)
        all_failed.extend(f)

        if all_translated and cfg.bazarr_api_key:
            _trigger_bazarr_rescan(cfg, file_path)
    elif since or scan_all:
        if since and scan_all:
            click.echo("Error: --since and --all are mutually exclusive")
            return
        results = scan_recent_missing(cfg.bazarr_db, since)
        if not results:
            msg = "No files with missing subtitles"
            if since:
                msg += f" in last {since} minutes"
            click.echo(msg)
            return
        if max_files is not None:
            results = results[:max_files]
        click.echo(f"Found {len(results)} file(s) with missing subtitles")
        for item in results:
            if chars_remaining is not None and chars_remaining <= 0:
                log.info("Max chars reached, stopping batch")
                break
            try:
                t, f = translate_file(cfg, item["path"], chars_remaining)
                all_translated.extend(t)
                all_failed.extend(f)
                if chars_remaining is not None:
                    chars_remaining -= sum(x["chars"] for x in t)
                if t and cfg.bazarr_api_key:
                    _trigger_bazarr_rescan(cfg, item["path"])
            except Exception as e:
                log.error("Error processing %s: %s", item["path"], e)
    else:
        click.echo("Must specify --since N, --file PATH, or --all")
        return

    total_chars = sum(t["chars"] for t in all_translated)
    provider_breakdown = get_monthly_chars_by_provider(db)
    monthly_chars = sum(provider_breakdown.values())
    provider_str = ", ".join(
        f"{p}: {c:,}" for p, c in sorted(provider_breakdown.items())
    )
    click.echo(
        f"Done: {len(all_translated)} translated, {len(all_failed)} failed, "
        f"{total_chars:,} chars used ({monthly_chars:,} this month"
        f"{' — ' + provider_str if provider_str else ''})"
    )

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

    provider_breakdown = get_monthly_chars_by_provider(db)
    monthly = sum(provider_breakdown.values())
    click.echo(f"Monthly usage: {monthly:,} chars")
    if provider_breakdown:
        for provider, chars in sorted(provider_breakdown.items()):
            click.echo(f"  {provider}: {chars:,} chars")

    recent = get_recent_translations(db, limit=10)
    if recent:
        click.echo(f"\nRecent translations ({len(recent)}):")
        for r in recent:
            provider = r.get("provider", PROVIDER_OLLAMA)
            click.echo(
                f"  {r['created_at']} | {r['status']:15s} | "
                f"{r['source_lang']}->{r['target_lang']} | "
                f"{r['chars_used']:>6,} chars | {provider:6s} | "
                f"{os.path.basename(r['media_path'])}"
            )
    else:
        click.echo("\nNo translations recorded yet.")


def _trigger_bazarr_rescan(cfg: Config, media_path: str):
    """Trigger Bazarr scan-disk for the file's series/movie."""
    import requests as req
    import sqlite3

    headers = {"X-API-KEY": cfg.bazarr_api_key}
    conn = sqlite3.connect(cfg.bazarr_db)
    conn.execute("PRAGMA busy_timeout=30000")

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
