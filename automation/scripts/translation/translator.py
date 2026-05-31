#!/usr/bin/env python3
# automation/scripts/translation/translator.py
"""Subtitle translator CLI — Ollama-only (DeepL/Gemini/Google removed 2026-04-29)."""

import concurrent.futures
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading

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

# Worker count for parallel batch translation (--since / --all modes).
# Override via TRANSLATOR_WORKERS env var without code edits.
_TRANSLATOR_WORKERS = int(os.environ.get("TRANSLATOR_WORKERS", "8"))
_results_lock = threading.Lock()

# Metrics helper (fail-soft: if lib_metrics is missing or DB unavailable,
# record_run_start returns -1 and record_run_end is a no-op)
try:
    from translation.lib_metrics import record_run_start as _metrics_start
    from translation.lib_metrics import record_run_end as _metrics_end
except ImportError:
    def _metrics_start(subsystem: str) -> int:  # type: ignore[misc]
        return -1
    def _metrics_end(run_id: int, exit_code: int, **kwargs) -> None:  # type: ignore[misc]
        pass

try:
    from translation.ollama_client import OllamaUnavailable as _OllamaUnavailable
except ImportError:  # pragma: no cover — only fails if ollama_client missing
    class _OllamaUnavailable(Exception):  # type: ignore[no-redef]
        pass

MARKER_EXTENSIONS = {
    PROVIDER_OLLAMA: ".ollama",
    "ollama+gemini": ".ollama",  # backward-compat: old marker name still .ollama
}

# ── Embedded Spanish extraction ───────────────────────────────────────────────

_PROVIDER_EMBEDDED = "embedded-extract"

# Language tags that indicate a Spanish subtitle stream.
_SPA_LANG_TAGS = {"spa", "es", "es-es", "es-mx", "es-419", "es-419", "esl", "esm"}

# Text-based codec names ffprobe reports for extractable subtitle streams.
_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Spanish / English marker words for content-based fingerprinting.
_ES_MARKERS = re.compile(
    r"\b(que|para|porque|también|nunca|señor|hola|gracias|hijo|estás|"
    r"qué|sí|cómo|está|esto|aquí|ahora|pero|tiene|cuando|donde|como)\b",
    re.IGNORECASE,
)
_EN_MARKERS = re.compile(
    r"\b(the|and|you|that|with|never|hello|thanks|sir|are|what|yes|"
    r"how|this|here|now|but|have|when|where|like)\b",
    re.IGNORECASE,
)

# langdetect seed for deterministic results.
try:
    from langdetect import detect as _langdetect_detect
    from langdetect import DetectorFactory as _DetectorFactory
    _DetectorFactory.seed = 42
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False


def _ffprobe_spa_streams(video_path: str) -> list:
    """Return list of (track_id_or_index, codec_name, lang_tag) for spa-tagged text sub streams.

    For MKV files, uses mkvmerge -J to get track IDs (needed by mkvextract).
    For other containers, falls back to ffprobe stream indices.
    The returned 'track_id' field is interpreted by _extract_spa_sub accordingly.
    """
    import json as _json

    ext = os.path.splitext(video_path)[1].lower()
    if ext == ".mkv":
        # mkvmerge path: faster probe, returns real track IDs for mkvextract
        try:
            result = subprocess.run(
                ["mkvmerge", "-J", video_path],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                return []
            data = _json.loads(result.stdout or "{}")
            found = []
            # mkvmerge -J returns human-readable codec names like "SubRip/SRT", "ASS", etc.
            # Accept any text-based subtitle codec; exclude image-based (PGS, DVDSUB, VOBSUB).
            _MKV_IMAGE_CODECS = {"VOBSUB", "PGS", "HDMV PGS", "BMP", "DVDVOBSUB"}
            for t in data.get("tracks", []):
                if t.get("type") != "subtitles":
                    continue
                codec = t.get("codec", "")
                if codec in _MKV_IMAGE_CODECS:
                    continue
                props = t.get("properties", {}) or {}
                lang = (props.get("language") or "und").lower()
                # Normalize 3-letter ISO 639-2 to 2-letter
                if lang == "spa":
                    lang = "es"
                if lang in _SPA_LANG_TAGS:
                    if props.get("forced_track") or props.get("flag_hearing_impaired"):
                        continue  # not a primary content track; skip
                    found.append((t["id"], codec, lang))
            return found
        except Exception as e:
            log.debug("mkvmerge probe failed for %s: %s", video_path, e)
            return []
    else:
        # Non-MKV: use ffprobe, return stream indices for ffmpeg extraction
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-select_streams", "s",
                    "-show_entries", "stream=index,codec_name:stream_tags=language:stream_disposition=forced,hearing_impaired",
                    "-of", "json",
                    video_path,
                ],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode != 0:
                return []
            data = _json.loads(result.stdout or "{}")
            found = []
            for s in data.get("streams", []):
                codec = (s.get("codec_name") or "").lower()
                if codec not in _TEXT_CODECS:
                    continue
                lang = (s.get("tags", {}).get("language") or "").lower().strip()
                if lang in _SPA_LANG_TAGS:
                    disp = s.get("disposition", {}) or {}
                    if disp.get("forced", 0) or disp.get("hearing_impaired", 0):
                        continue  # not a primary content track; skip
                    found.append((s["index"], codec, lang))
            return found
        except Exception as e:
            log.debug("ffprobe failed for %s: %s", video_path, e)
            return []


def _extract_spa_sub(video_path: str, track_id: int, out_path: str) -> bool:
    """Extract a subtitle track to out_path. Uses mkvextract for MKV, ffmpeg for others.

    Returns True on success.
    """
    ext = os.path.splitext(video_path)[1].lower()
    try:
        if ext == ".mkv":
            result = subprocess.run(
                ["mkvextract", "tracks", video_path, f"{track_id}:{out_path}"],
                capture_output=True, timeout=300,
            )
        else:
            # ffmpeg with absolute stream index; -c:s copy avoids re-encoding
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", video_path,
                    "-map", f"0:{track_id}",
                    "-c:s", "copy",
                    out_path,
                ],
                capture_output=True, timeout=300,
            )
        return result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        log.debug("extract failed track %d from %s: %s", track_id, video_path, e)
        return False


def _looks_like_spanish(srt_path: str) -> bool:
    """Return True if the SRT file content is genuinely Spanish.

    Uses langdetect on the middle 30% of the file if available;
    falls back to ES/EN marker-word ratio (>=30 ES hits, ratio >=3:1).
    """
    try:
        text = open(srt_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False

    # Strip SRT timing/index lines to get dialogue only.
    dialogue = re.sub(r"^\d+\s*$|^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\s*$",
                      "", text, flags=re.MULTILINE)

    if _HAS_LANGDETECT:
        # Sample the middle 30% of dialogue text for stability.
        start = len(dialogue) // 3
        sample = dialogue[start: start + max(500, len(dialogue) // 3)]
        if len(sample.strip()) < 50:
            sample = dialogue[:2000]
        try:
            return _langdetect_detect(sample) == "es"
        except Exception:
            pass  # fall through to marker heuristic

    es_count = len(_ES_MARKERS.findall(dialogue))
    en_count = len(_EN_MARKERS.findall(dialogue))
    return es_count >= 30 and es_count >= en_count * 3


def _try_embedded_spanish(video_path: str, target_srt: str) -> bool:
    """Extract a genuine Spanish embedded subtitle track to target_srt.

    Returns True if a real Spanish track was found, extracted, and moved into place.
    Falls through silently if nothing usable is found.
    """
    streams = _ffprobe_spa_streams(video_path)
    if not streams:
        return False

    import shutil
    tmp_dir = tempfile.mkdtemp(prefix="emb_es_")
    try:
        for track_id, codec, lang_tag in streams:
            tmp_path = os.path.join(tmp_dir, f"track_{track_id}.srt")
            if not _extract_spa_sub(video_path, track_id, tmp_path):
                log.debug("embedded-extract: extraction failed track %d in %s",
                          track_id, os.path.basename(video_path))
                continue
            if _looks_like_spanish(tmp_path):
                shutil.move(tmp_path, target_srt)
                log.info(
                    "embedded-extract: used track %d (tag=%s codec=%s) from %s -> %s",
                    track_id, lang_tag, codec,
                    os.path.basename(video_path), os.path.basename(target_srt),
                )
                return True
            log.debug(
                "embedded-extract: track %d lang=%s fingerprint FAILED (English content), skipping",
                track_id, lang_tag,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return False


def _db_path(state_dir: str) -> str:
    # Phase 6 I-A: use centralized pipeline.db if PIPELINE_DB env var is set
    pipeline_db = os.environ.get("PIPELINE_DB", "").strip()
    if pipeline_db:
        return pipeline_db
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


def translate_file(cfg: Config, media_path: str, chars_remaining=None,
                   no_embedded_fallback: bool = False):
    """Translate missing subtitle languages for a single media file.

    Returns (list of {file, target, chars, provider}, list of {file, target, error}).
    If no_embedded_fallback is False (default), checks the source MKV for embedded
    genuine Spanish tracks before invoking the LoRA translator.
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

        # Guard: this pipeline is EN→ES only. Any non-ES target would cause
        # the Ollama EN→ES model to produce Spanish content saved under the wrong
        # language tag (e.g. .en.srt with Spanish content). Skip silently.
        if base_lang != "es":
            log.info("Skipping non-ES target '%s' — translator is EN→ES only", base_lang)
            continue

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

        # ── Embedded Spanish fast-path ────────────────────────────────────────
        # Before invoking the LoRA translator, check if the MKV already has a
        # genuine Spanish subtitle stream. If so, extract it directly and skip
        # GPU translation entirely.  Bypass with --no-embedded-fallback.
        if base_lang == "es" and not no_embedded_fallback and os.path.isfile(media_path):
            target_srt = os.path.join(directory, f"{stem}.{base_lang}.srt")
            if _try_embedded_spanish(media_path, target_srt):
                # Write a distinct marker so we can audit embedded-vs-translated.
                # [markers-off 2026-05-31] open(target_srt + ".embedded", "w").close()
                record_translation(
                    db_path, media_path, "spa-embedded", base_lang,
                    os.path.getsize(target_srt), "success", _PROVIDER_EMBEDDED,
                )
                translated.append({
                    "file": basename, "target": base_lang,
                    "chars": os.path.getsize(target_srt),
                    "provider": _PROVIDER_EMBEDDED,
                })
                log.info("lora-translate: SKIPPED — embedded-extract succeeded for %s", basename)
                continue
        # ── End embedded fast-path ────────────────────────────────────────────

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
            # [markers-off 2026-05-31] open(output_path + marker_ext, "w").close()

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
@click.option("--no-embedded-fallback", "no_embedded_fallback", is_flag=True, default=False,
              help="Skip embedded Spanish track check; always use LoRA translator")
def translate(since, scan_all, file_path, max_chars, state_dir, bazarr_db, max_files,
              no_embedded_fallback):
    """Translate missing subtitles via Ollama."""
    global _ollama_unavailable
    _ollama_unavailable = False

    _metrics_run_id = _metrics_start("translator")

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
        t, f = translate_file(cfg, file_path, chars_remaining,
                               no_embedded_fallback=no_embedded_fallback)
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
        # budget tracking disabled in parallel path: local Ollama, no per-call cost
        def _worker(item, _nef=no_embedded_fallback):
            return item, translate_file(cfg, item["path"], None, no_embedded_fallback=_nef)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_TRANSLATOR_WORKERS) as pool:
            futures = {pool.submit(_worker, item): item for item in results}
            for fut in concurrent.futures.as_completed(futures):
                item = futures[fut]
                try:
                    _item, (t, f) = fut.result()
                except Exception as e:
                    log.exception("translate_file failed for %s: %s", item.get("path"), e)
                    with _results_lock:
                        all_failed.append({"path": item.get("path"), "error": str(e)})
                    continue
                with _results_lock:
                    all_translated.extend(t)
                    all_failed.extend(f)
                if t and cfg.bazarr_api_key:
                    _trigger_bazarr_rescan(cfg, item["path"])
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

    _metrics_end(
        _metrics_run_id,
        exit_code=0,
        files_processed=len(all_translated),
        files_failed=len(all_failed),
        metadata={"provider": "ollama", "total_chars": total_chars},
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
