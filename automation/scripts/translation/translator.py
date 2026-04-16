#!/usr/bin/env python3
# automation/scripts/translation/translator.py
"""Subtitle translator CLI with DeepL -> Gemini -> Google fallback chain."""

import logging
import os
import sys

import click

from datetime import date

from translation.config import (
    Config, load_config,
    ALL_SUPPORTED_LANGS, ALL_SUPPORTED_SOURCE_LANGS,
    DEEPL_LANG_MAP, DEEPL_SOURCE_LANG_MAP, DEEPL_SKIP_UNTIL,
    GEMINI_LANG_MAP,
    GOOGLE_LANG_MAP,
    PROVIDER_DEEPL, PROVIDER_GEMINI, PROVIDER_GOOGLE,
)
from translation.db import (
    init_db, record_translation, is_on_cooldown, is_permanently_failed,
    get_monthly_chars, get_monthly_chars_by_provider, get_recent_translations,
    get_daily_requests, get_monthly_chars_by_key, get_daily_requests_by_key,
)

_DEPRECATION_WARNED: set = set()


def _get_per_key_budget(new_name: str, old_name: str, default: int) -> int:
    """Read a per-key budget env var with backward-compat fallback.

    If only the old (aggregate) name is set, honors it as the per-key value and
    emits a one-time deprecation warning so operators know to rename it.
    """
    if new_name in os.environ:
        return int(os.environ[new_name])
    if old_name in os.environ:
        if old_name not in _DEPRECATION_WARNED:
            log.warning(
                "DEPRECATED: %s should be renamed to %s (per-key semantics unchanged)",
                old_name, new_name,
            )
            _DEPRECATION_WARNED.add(old_name)
        return int(os.environ[old_name])
    return default
from translation.deepl_client import (
    translate_srt_cues as deepl_translate_srt_cues,
    get_usage as deepl_get_usage,
    DeeplKeysExhausted,
    reset_exhausted_keys as deepl_reset_exhausted_keys,
)
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

# Session-level flags: once a provider's budget or API quota is exceeded,
# skip it for the rest of the run and fall through to the next provider.
_deepl_quota_exceeded = False
_gemini_quota_exceeded = False
_google_quota_exceeded = False

MARKER_EXTENSIONS = {
    PROVIDER_DEEPL: ".deepl",
    PROVIDER_GEMINI: ".gemini",
    PROVIDER_GOOGLE: ".gtranslate",
}


def _db_path(state_dir: str) -> str:
    return os.path.join(state_dir, "translation_state.db")


def _has_deepl(cfg: Config) -> bool:
    """Check if DeepL is available (has API keys, quota not exceeded, not date-skipped)."""
    if DEEPL_SKIP_UNTIL and date.today() <= DEEPL_SKIP_UNTIL:
        return False
    return bool(cfg.deepl_api_keys) and not _deepl_quota_exceeded


def _has_gemini(cfg: Config) -> bool:
    """Check if Gemini is available (has keys and quota not exceeded)."""
    return bool(cfg.gemini_api_keys) and not _gemini_quota_exceeded


def _has_google(cfg: Config) -> bool:
    """Check if Google Translate is available (enabled and budget not exceeded)."""
    return cfg.google_translate_enabled and not _google_quota_exceeded


def _build_available_keys(db_path, provider, all_keys,
                           monthly_per_key, daily_per_key=None):
    """Return [(original_index, key), ...] for keys within budget.

    A key is available when it is under both its monthly char budget AND (if
    daily_per_key is set) its daily request budget.
    Uses two grouped queries instead of 2N per-key queries.
    Returns [] when all keys are at budget — caller skips the provider naturally;
    the session quota flag is NOT set here (reserved for genuine API exhaustion).
    """
    monthly_by_key = get_monthly_chars_by_key(db_path, provider)
    daily_by_key = get_daily_requests_by_key(db_path, provider) if daily_per_key is not None else {}
    available = []
    for i, key in enumerate(all_keys):
        if monthly_by_key.get(i, 0) >= monthly_per_key:
            continue
        if daily_per_key is not None and daily_by_key.get(i, 0) >= daily_per_key:
            continue
        available.append((i, key))
    return available


def _try_provider(provider_name, client_fn, exc_class, avail, cues,
                  source_lang, target_lang, fallback_label):
    """Attempt translation with a filtered key list.

    avail: [(original_index, key), ...] — pre-filtered by _build_available_keys.
    Returns (translated_cues, chars_used, original_key_index) on success.
    Returns None when avail is empty (per-key budget exhausted — caller skips).
    Raises exc_class back to caller on genuine API quota exhaustion so the
    caller can set the session flag and fall through to the next provider.
    Raises other exceptions unchanged (non-quota errors).
    """
    if not avail:
        log.warning("All %s keys at per-key budget, trying %s", provider_name, fallback_label)
        return None
    filtered_keys = [k for _, k in avail]
    translated_cues, chars_used, pos_in_filtered = client_fn(
        filtered_keys, cues, source_lang, target_lang
    )
    key_index = avail[pos_in_filtered][0]
    return translated_cues, chars_used, key_index


def _translate_cues_with_fallback(cfg, cues, source_lang_code, base_lang,
                                   google_translator, db_path=None,
                                   deepl_monthly_per_key=500_000,
                                   gemini_monthly_per_key=500_000,
                                   gemini_daily_per_key=None):
    """Translate cues trying Gemini -> DeepL -> Google.

    Returns (translated_cues, chars_used, provider, key_index).
    key_index is the original (unfiltered) position of the key used, or None
    for Google (no per-key tracking).
    Raises on non-quota errors.
    """
    global _deepl_quota_exceeded, _gemini_quota_exceeded, _google_quota_exceeded

    # Try Gemini first
    if _has_gemini(cfg) and base_lang in GEMINI_LANG_MAP and source_lang_code in GEMINI_LANG_MAP:
        from translation.gemini_client import (
            translate_srt_cues as gemini_translate_srt_cues,
            GeminiQuotaExhausted,
        )
        gemini_source = GEMINI_LANG_MAP[source_lang_code]
        gemini_target = GEMINI_LANG_MAP[base_lang]

        if db_path:
            avail = _build_available_keys(
                db_path, PROVIDER_GEMINI, cfg.gemini_api_keys,
                gemini_monthly_per_key, gemini_daily_per_key,
            )
            try:
                result = _try_provider(
                    PROVIDER_GEMINI, gemini_translate_srt_cues, GeminiQuotaExhausted,
                    avail, cues, gemini_source, gemini_target, "DeepL",
                )
                if result is not None:
                    translated_cues, chars_used, key_index = result
                    return translated_cues, chars_used, PROVIDER_GEMINI, key_index
            except GeminiQuotaExhausted:
                # Genuine API quota exhaustion — set session flag to skip Gemini this run
                log.warning("All Gemini keys exhausted, trying DeepL")
                _gemini_quota_exceeded = True
            except Exception as e:
                log.warning("Gemini failed (%s), trying DeepL", e)
        else:
            try:
                translated_cues, chars_used, pos = gemini_translate_srt_cues(
                    cfg.gemini_api_keys, cues, gemini_source, gemini_target
                )
                return translated_cues, chars_used, PROVIDER_GEMINI, pos
            except GeminiQuotaExhausted:
                log.warning("All Gemini keys exhausted, trying DeepL")
                _gemini_quota_exceeded = True
            except Exception as e:
                log.warning("Gemini failed (%s), trying DeepL", e)

    # Try DeepL
    if _has_deepl(cfg) and base_lang in DEEPL_LANG_MAP and source_lang_code in DEEPL_SOURCE_LANG_MAP:
        deepl_source = DEEPL_SOURCE_LANG_MAP[source_lang_code]
        deepl_target = DEEPL_LANG_MAP[base_lang]

        if db_path:
            avail = _build_available_keys(
                db_path, PROVIDER_DEEPL, cfg.deepl_api_keys, deepl_monthly_per_key
            )
            try:
                result = _try_provider(
                    PROVIDER_DEEPL, deepl_translate_srt_cues, DeeplKeysExhausted,
                    avail, cues, deepl_source, deepl_target, "Google",
                )
                if result is not None:
                    translated_cues, chars_used, key_index = result
                    return translated_cues, chars_used, PROVIDER_DEEPL, key_index
            except DeeplKeysExhausted:
                # Genuine API quota exhaustion — set session flag to skip DeepL this run
                log.warning("All DeepL keys exhausted, trying Google")
                _deepl_quota_exceeded = True
        else:
            try:
                translated_cues, chars_used, pos = deepl_translate_srt_cues(
                    cfg.deepl_api_keys, cues, deepl_source, deepl_target
                )
                return translated_cues, chars_used, PROVIDER_DEEPL, pos
            except DeeplKeysExhausted:
                log.warning("All DeepL keys exhausted, trying Google")
                _deepl_quota_exceeded = True

    # Fall back to Google (no per-key tracking)
    if _has_google(cfg) and base_lang in GOOGLE_LANG_MAP and source_lang_code in GOOGLE_LANG_MAP:
        from translation.google_client import translate_srt_cues as google_translate_srt_cues
        google_source = GOOGLE_LANG_MAP[source_lang_code]
        google_target = GOOGLE_LANG_MAP[base_lang]
        if google_translator is None:
            from translation.google_client import create_translator as create_google
            google_translator = create_google()
        translated_cues, chars_used = google_translate_srt_cues(
            google_translator, cues, google_source, google_target
        )
        return translated_cues, chars_used, PROVIDER_GOOGLE, None

    raise ValueError(f"No translation provider available for {source_lang_code} -> {base_lang}")


def translate_file(cfg: Config, media_path: str,
                   chars_remaining=None, google_translator=None,
                   deepl_monthly_per_key=500_000,
                   gemini_monthly_per_key=500_000,
                   gemini_daily_per_key=None):
    """Translate missing subtitle languages for a single media file.

    Args:
        chars_remaining: If set, stop after consuming this many chars.
        google_translator: Optional pre-created Google translator instance.
        deepl_monthly_per_key: Per-key monthly char budget for DeepL.
        gemini_monthly_per_key: Per-key monthly char budget for Gemini.
        gemini_daily_per_key: Per-key daily request cap for Gemini (None = uncapped).

    Returns (list of {file, target, chars, provider}, list of {file, target, error}).
    """
    global _gemini_quota_exceeded
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

        # Check if any provider supports this language
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
            record_translation(db_path, media_path, "?", base_lang, 0, "no_source")
            continue

        # Detect source language from filename
        source_basename = os.path.basename(source_srt)
        source_lang_code = source_basename[len(stem) + 1:].split(".")[0].lower()

        if source_lang_code not in ALL_SUPPORTED_SOURCE_LANGS:
            log.info("No source mapping for '%s'", source_lang_code)
            continue

        try:
            # Read and parse source SRT
            with open(source_srt, "r", encoding="utf-8", errors="replace") as f:
                source_content = f.read()
            cues = parse_srt(source_content)
            if not cues:
                log.warning("Empty SRT: %s", source_srt)
                continue

            # Translate with fallback
            log.info("Translating %s: %s -> %s (%d cues)",
                     basename, source_lang_code, base_lang, len(cues))
            translated_cues, chars_used, provider, key_index = _translate_cues_with_fallback(
                cfg, cues, source_lang_code, base_lang,
                google_translator,
                db_path=db_path,
                deepl_monthly_per_key=deepl_monthly_per_key,
                gemini_monthly_per_key=gemini_monthly_per_key,
                gemini_daily_per_key=gemini_daily_per_key,
            )

            # Write translated SRT + marker for deferred muxing
            output_path = os.path.join(directory, f"{stem}.{base_lang}.srt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(write_srt(translated_cues))
            marker_ext = MARKER_EXTENSIONS[provider]
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

        except Exception as e:
            error_msg = str(e)
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
    """Subtitle translator with DeepL + Google Translate fallback."""
    pass


@cli.command()
@click.option("--since", type=int, default=None,
              help="Only process files modified in the last N minutes")
@click.option("--file", "file_path", type=str, default=None,
              help="Translate a single file")
@click.option("--max-chars", type=int, default=None,
              help="Maximum characters to translate in this run")
@click.option("--monthly-budget", type=int, default=400_000,
              help="Monthly character budget (default: 400000)")
@click.option("--state-dir", type=str, default=None)
@click.option("--bazarr-db", type=str, default=None)
def translate(since, file_path, max_chars, monthly_budget, state_dir, bazarr_db):
    """Translate missing subtitles via DeepL with Google fallback."""
    global _deepl_quota_exceeded, _gemini_quota_exceeded, _google_quota_exceeded
    _deepl_quota_exceeded = False  # Reset per invocation
    _gemini_quota_exceeded = False
    _google_quota_exceeded = False

    # Reset Gemini and DeepL key rotation state
    from translation.gemini_client import reset_exhausted_keys
    reset_exhausted_keys()
    deepl_reset_exhausted_keys()

    cfg = load_config(bazarr_db=bazarr_db, state_dir=state_dir)
    db = _db_path(cfg.state_dir)
    init_db(db)

    # Per-provider monthly budget enforcement.
    # Each provider is checked independently so that one provider exhausting its
    # budget does not uncap (chars_remaining=None) the remaining providers.
    gemini_budget = int(os.environ.get("GEMINI_MONTHLY_BUDGET", "500000"))
    google_budget = int(os.environ.get("GOOGLE_MONTHLY_BUDGET", "500000"))

    # Per-key budgets. GEMINI_DAILY_REQUESTS_BUDGET (without _PER_KEY) is the
    # only legacy name that genuinely existed; honored with a deprecation warning.
    deepl_monthly_per_key = int(os.environ.get("DEEPL_MONTHLY_BUDGET_PER_KEY", "500000"))
    gemini_monthly_per_key = int(os.environ.get("GEMINI_MONTHLY_BUDGET_PER_KEY", "500000"))
    gemini_daily_per_key = _get_per_key_budget(
        "GEMINI_DAILY_REQUESTS_BUDGET_PER_KEY", "GEMINI_DAILY_REQUESTS_BUDGET", 9
    )

    deepl_used = get_monthly_chars(db, provider=PROVIDER_DEEPL)
    if monthly_budget and deepl_used >= monthly_budget:
        log.warning(
            "Monthly DeepL budget reached: %s / %s chars — DeepL disabled for this run",
            f"{deepl_used:,}", f"{monthly_budget:,}",
        )
        _deepl_quota_exceeded = True
    elif not _deepl_quota_exceeded and monthly_budget:
        # Cap max_chars to DeepL's remaining budget slice only when DeepL is active
        budget_remaining = monthly_budget - deepl_used
        if max_chars is None or max_chars > budget_remaining:
            max_chars = budget_remaining
            log.info("Capped to %d chars (DeepL monthly budget remaining)", max_chars)

    gemini_used = get_monthly_chars(db, provider=PROVIDER_GEMINI)
    if gemini_budget and gemini_used >= gemini_budget:
        log.warning(
            "Monthly Gemini budget reached: %s / %s chars — Gemini disabled for this run",
            f"{gemini_used:,}", f"{gemini_budget:,}",
        )
        _gemini_quota_exceeded = True

    google_used = get_monthly_chars(db, provider=PROVIDER_GOOGLE)
    if google_budget and google_used >= google_budget:
        log.warning(
            "Monthly Google budget reached: %s / %s chars — Google disabled for this run",
            f"{google_used:,}", f"{google_budget:,}",
        )
        _google_quota_exceeded = True

    google_translator = None

    # Check DeepL availability (date-skip and API-level quota)
    deepl_skipped_by_date = DEEPL_SKIP_UNTIL and date.today() <= DEEPL_SKIP_UNTIL
    if deepl_skipped_by_date:
        log.info("DeepL skipped until %s — using Gemini/Google", DEEPL_SKIP_UNTIL)
        _deepl_quota_exceeded = True
    elif not _deepl_quota_exceeded and cfg.deepl_api_keys:
        # Pre-flight usage check on first key only — non-fatal
        try:
            usage = deepl_get_usage(cfg.deepl_api_keys[0])
            if usage["character_count"] >= usage["character_limit"] > 0:
                log.warning("DeepL quota already exhausted, using Gemini/Google")
                _deepl_quota_exceeded = True
        except Exception as e:
            log.warning("Could not check DeepL usage: %s", e)
    elif not _deepl_quota_exceeded:
        log.info("No DeepL API keys configured, using Gemini/Google")
        _deepl_quota_exceeded = True

    if not cfg.deepl_api_keys and not cfg.gemini_api_keys and not cfg.google_translate_enabled:
        click.echo("Error: No translation provider available")
        return

    chars_remaining = max_chars
    all_translated = []
    all_failed = []

    if file_path:
        # Single-file mode (import hook)
        if not os.path.isfile(file_path):
            log.error("File not found: %s", file_path)
            return
        t, f = translate_file(cfg, file_path,
                              chars_remaining, google_translator,
                              deepl_monthly_per_key=deepl_monthly_per_key,
                              gemini_monthly_per_key=gemini_monthly_per_key,
                              gemini_daily_per_key=gemini_daily_per_key)
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
                t, f = translate_file(cfg, item["path"],
                                      chars_remaining, google_translator,
                                      deepl_monthly_per_key=deepl_monthly_per_key,
                                      gemini_monthly_per_key=gemini_monthly_per_key,
                                      gemini_daily_per_key=gemini_daily_per_key)
                all_translated.extend(t)
                all_failed.extend(f)
                if chars_remaining is not None:
                    chars_remaining -= sum(x["chars"] for x in t)
                if t and cfg.bazarr_api_key:
                    _trigger_bazarr_rescan(cfg, item["path"])
            except ValueError as e:
                if "No translation provider" in str(e):
                    log.error("All providers failed, stopping")
                    monthly = get_monthly_chars(db)
                    notify_quota_warning(cfg.discord_webhook_url, monthly, monthly_budget or 400_000)
                    break
                log.error("Error processing %s: %s", item["path"], e)
            except Exception as e:
                log.error("Error processing %s: %s", item["path"], e)
    else:
        click.echo("Must specify --since N or --file PATH")
        return

    # Summary
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

    provider_breakdown = get_monthly_chars_by_provider(db)
    monthly = sum(provider_breakdown.values())
    budget = 400_000
    click.echo(f"Monthly usage: {monthly:,} / {budget:,} chars ({monthly/budget*100:.1f}%)")
    if provider_breakdown:
        for provider, chars in sorted(provider_breakdown.items()):
            click.echo(f"  {provider}: {chars:,} chars")

    recent = get_recent_translations(db, limit=10)
    if recent:
        click.echo(f"\nRecent translations ({len(recent)}):")
        for r in recent:
            provider = r.get("provider", PROVIDER_DEEPL)
            click.echo(
                f"  {r['created_at']} | {r['status']:15s} | "
                f"{r['source_lang']}->{r['target_lang']} | "
                f"{r['chars_used']:>6,} chars | {provider:6s} | "
                f"{os.path.basename(r['media_path'])}"
            )
    else:
        click.echo("\nNo translations recorded yet.")


@cli.command()
def usage():
    """Query DeepL API for remaining quota (all configured keys)."""
    cfg = load_config()
    if not cfg.deepl_api_keys:
        click.echo("No DeepL API key configured")
        return
    for i, key in enumerate(cfg.deepl_api_keys, 1):
        key_label = f"{key[:6]}...{key[-4:]}"
        try:
            usage_data = deepl_get_usage(key)
            count = usage_data["character_count"]
            limit = usage_data["character_limit"]
            if limit > 0:
                click.echo(
                    f"DeepL key {i} ({key_label}): {count:,} / {limit:,} chars "
                    f"({count / limit * 100:.1f}%)"
                )
            else:
                click.echo(f"DeepL key {i} ({key_label}): could not retrieve usage data")
        except Exception as e:
            click.echo(f"DeepL key {i} ({key_label}) failed: {e}")


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
