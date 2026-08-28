#!/usr/bin/env python3
"""Trending Auto-Add — fetches trending content from streaming services and adds to Radarr/Sonarr.

Queries Movie of the Night API for top trending movies/series on configured
streaming services, caches results, and adds new items to Radarr/Sonarr.
"""

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click
import requests

from streaming.arr_client import (
    add_movie,
    add_series,
    ensure_tag,
    fetch_movies,
    fetch_series,
)
from streaming.config import load_config
from streaming.discord import GREEN, ORANGE, send_embed
from streaming.streaming_api_client import search_catalog

log = logging.getLogger("trending_add")

TAG_LABEL = "trending-add"
CACHE_DIR = Path("/APPBOX_DATA/storage/.trending-cache")
CACHE_TTL_DAYS = 7

ANIMATED_GENRES = {"animation", "anime"}
TRENDING_SERVICES = ["apple", "paramount", "hbo"]

# Root folders and quality profiles
RADARR_ROOTS = {
    "movies": "/APPBOX_DATA/storage/media/movies",
    "moviesanimated": "/APPBOX_DATA/storage/media/moviesanimated",
}
SONARR_ROOTS = {
    "tv": "/APPBOX_DATA/storage/media/tv",
    "tvanimated": "/APPBOX_DATA/storage/media/tvanimated",
}
RADARR_QUALITY_PROFILE = 1  # HD-1080p
SONARR_QUALITY_PROFILE = 4  # HD-1080p

# Per-show_type dispatch table: (add_fn, quality_profile, root_map, arr_url_attr, arr_key_attr, tag_key)
_TYPE_CONFIG = {
    "movie": {
        "add_fn": add_movie,
        "quality_profile": RADARR_QUALITY_PROFILE,
        "roots": RADARR_ROOTS,
        "root_keys": ("moviesanimated", "movies"),
        "url_attr": "radarr_url",
        "key_attr": "radarr_key",
        "tag_key": "radarr",
    },
    "series": {
        "add_fn": add_series,
        "quality_profile": SONARR_QUALITY_PROFILE,
        "roots": SONARR_ROOTS,
        "root_keys": ("tvanimated", "tv"),
        "url_attr": "sonarr_url",
        "key_attr": "sonarr_key",
        "tag_key": "sonarr",
    },
}


def _is_animated(genres):
    """Check if genres contain animation/anime."""
    return bool(ANIMATED_GENRES & {g.lower() for g in genres})


def _cache_path(service, show_type):
    return CACHE_DIR / f"{service}_{show_type}.json"


def _cache_age_hours(path):
    """Return cache age in hours, or None if no cache."""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
    except FileNotFoundError:
        return None


def _load_cache(service, show_type):
    """Load cached results if fresh (< TTL). Returns list or None."""
    path = _cache_path(service, show_type)
    try:
        st = path.stat()
        age_h = (datetime.now(timezone.utc) - datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)).total_seconds() / 3600
        if age_h > CACHE_TTL_DAYS * 24:
            return None
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _save_cache(service, show_type, data):
    """Save results to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(service, show_type)
    path.write_text(json.dumps(data, indent=2))


def _fetch_trending(cfg, service, show_type, limit):
    """Fetch trending items, using cache if fresh."""
    cached = _load_cache(service, show_type)
    if cached is not None:
        log.info("Using cached %s %s results (%d items)", service, show_type, len(cached))
        return cached[:limit]

    log.info("Fetching %s trending %s from API (limit %d)...", service, show_type, limit)
    results = search_catalog(
        cfg.rapidapi_key, service, show_type,
        country=cfg.country.lower(), limit=limit,
    )
    if results:
        _save_cache(service, show_type, results)
        log.info("Cached %d %s %s results", len(results), service, show_type)
    return results


def _get_existing_tmdb_ids(cfg):
    """Get sets of TMDB IDs already in Radarr and Sonarr (fetched in parallel)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_movies = ex.submit(fetch_movies, cfg.radarr_url, cfg.radarr_key)
        f_series = ex.submit(fetch_series, cfg.sonarr_url, cfg.sonarr_key)
    movie_ids = {m["tmdb_id"] for m in f_movies.result() if m["tmdb_id"]}
    series_ids = {s["tmdb_id"] for s in f_series.result() if s["tmdb_id"]}
    return movie_ids, series_ids


def _add_items(cfg, items, show_type, seen_ids, tag_ids, dry_run):
    """Add items not in existing library.

    Args:
        seen_ids: set of TMDB IDs already in library or added this run.
            Mutated in-place — added tmdb_ids are inserted to prevent cross-service duplicates.
        tag_ids: dict mapping "radarr"/"sonarr" to tag ID ints.

    Returns (added, skipped, failed) lists.
    """
    tc = _TYPE_CONFIG[show_type]
    add_fn = tc["add_fn"]
    quality_profile = tc["quality_profile"]
    roots = tc["roots"]
    animated_key, normal_key = tc["root_keys"]
    arr_url = getattr(cfg, tc["url_attr"])
    arr_key = getattr(cfg, tc["key_attr"])
    tag_id = tag_ids.get(tc["tag_key"], 0)

    added, skipped, failed = [], [], []

    for item in items:
        tmdb_id = item["tmdb_id"]
        title = item["title"]
        year = item.get("year", "?")

        if not tmdb_id:
            log.warning("Skipping '%s' — no TMDB ID", title)
            failed.append(item)
            continue

        if tmdb_id in seen_ids:
            log.debug("Already in library: %s (%s)", title, year)
            skipped.append(item)
            continue

        animated = _is_animated(item.get("genres", []))
        root = roots[animated_key if animated else normal_key]
        label = "animated " if animated else ""

        if dry_run:
            log.info("[DRY RUN] Would add %s%s: %s (%s) → %s", label, show_type, title, year, root)
            added.append(item)
            seen_ids.add(tmdb_id)
            continue

        try:
            result = add_fn(arr_url, arr_key, tmdb_id, quality_profile, root, tags=[tag_id])
            if result:
                log.info("Added %s%s: %s (%s)", label, show_type, title, year)
                added.append(item)
                seen_ids.add(tmdb_id)
            else:
                log.warning("Lookup failed for %s: %s (%s)", show_type, title, year)
                failed.append(item)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                log.info("Already exists or invalid: %s (%s)", title, year)
                skipped.append(item)
            else:
                log.error("Failed to add %s %s: %s", show_type, title, e)
                failed.append(item)

    return added, skipped, failed


def _notify_results(cfg, all_added, all_skipped, all_failed, services, dry_run,
                    movies_limit=30, series_limit=10):
    """Send Discord notification with results."""
    if not all_added and not all_failed:
        return

    prefix = "[DRY RUN] " if dry_run else ""
    desc_lines = []

    movies_added = [i for i in all_added if i.get("show_type") == "movie"]
    series_added = [i for i in all_added if i.get("show_type") != "movie"]

    if movies_added:
        desc_lines.append(f"🎬 {prefix}Movies added: **{len(movies_added)}**")
    if series_added:
        desc_lines.append(f"📺 {prefix}Series added: **{len(series_added)}**")
    if all_skipped:
        desc_lines.append(f"⏭️ Already in library: **{len(all_skipped)}**")
    if all_failed:
        desc_lines.append(f"❌ Failed: **{len(all_failed)}**")

    fields = []
    if all_added:
        lines = []
        for item in all_added[:20]:
            animated = "🎌 " if _is_animated(item.get("genres", [])) else ""
            typ = "🎬" if item.get("show_type") == "movie" else "📺"
            lines.append(f"{typ} {animated}`{item['title']}` ({item.get('year', '?')})")
        if len(all_added) > 20:
            lines.append(f"…and {len(all_added) - 20} more")
        fields.append({
            "name": f"{'📋 Would Add' if dry_run else '✅ Added'}",
            "value": "\n".join(lines),
            "inline": False,
        })

    color = GREEN if not all_failed else ORANGE
    svc_label = ", ".join(s.title() for s in services)
    send_embed(
        cfg.discord_webhook_url,
        f"{'📋' if dry_run else '🆕'} Trending Auto-Add — {svc_label}",
        "\n".join(desc_lines),
        color,
        fields,
        footer=f"Services: {svc_label} | Movies: {movies_limit}, Series: {series_limit}",
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True)
def cli(verbose):
    """Trending content auto-add from streaming services."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@cli.command()
@click.option("--dry-run", is_flag=True, help="Preview without adding")
@click.option("--movies", default=30, help="Number of trending movies")
@click.option("--series", default=10, help="Number of trending series")
def sync(dry_run, movies, series):
    """Fetch trending content and add to Radarr/Sonarr."""
    cfg = load_config()
    if not cfg.rapidapi_key:
        click.echo("Error: RAPIDAPI_KEY is required", err=True)
        sys.exit(1)

    movie_ids, series_ids = _get_existing_tmdb_ids(cfg)
    log.info("Library: %d movies, %d series", len(movie_ids), len(series_ids))

    # Ensure tags exist
    tag_ids = {}
    if not dry_run:
        tag_ids["radarr"] = ensure_tag(cfg.radarr_url, cfg.radarr_key, TAG_LABEL)
        tag_ids["sonarr"] = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, TAG_LABEL)

    all_added, all_skipped, all_failed = [], [], []

    for service in TRENDING_SERVICES:
        log.info("=== Processing %s ===", service.upper())

        # Fetch trending movies
        trending_movies = _fetch_trending(cfg, service, "movie", movies)
        if trending_movies:
            added, skipped, failed = _add_items(
                cfg, trending_movies, "movie", movie_ids, tag_ids, dry_run,
            )
            all_added.extend(added)
            all_skipped.extend(skipped)
            all_failed.extend(failed)

        # Fetch trending series
        trending_series = _fetch_trending(cfg, service, "series", series)
        if trending_series:
            added, skipped, failed = _add_items(
                cfg, trending_series, "series", series_ids, tag_ids, dry_run,
            )
            all_added.extend(added)
            all_skipped.extend(skipped)
            all_failed.extend(failed)

    log.info("Done: %d added, %d skipped, %d failed",
             len(all_added), len(all_skipped), len(all_failed))

    _notify_results(cfg, all_added, all_skipped, all_failed, TRENDING_SERVICES, dry_run,
                    movies_limit=movies, series_limit=series)


@cli.command("preview")
@click.option("--movies", default=30, help="Number of trending movies")
@click.option("--series", default=10, help="Number of trending series")
def preview(movies, series):
    """Show what would be added without adding (alias for sync --dry-run)."""
    ctx = click.get_current_context()
    ctx.invoke(sync, dry_run=True, movies=movies, series=series)


@cli.command("cache-status")
def cache_status():
    """Show cache status for all services."""
    for service in TRENDING_SERVICES:
        for show_type in ("movie", "series"):
            path = _cache_path(service, show_type)
            age = _cache_age_hours(path)
            if age is None:
                click.echo(f"  {service}/{show_type}: no cache")
            else:
                items = 0
                try:
                    items = len(json.loads(path.read_text()))
                except Exception:
                    pass
                fresh = "fresh" if age < CACHE_TTL_DAYS * 24 else "stale"
                click.echo(f"  {service}/{show_type}: {items} items, {age:.1f}h old ({fresh})")


if __name__ == "__main__":
    cli()
