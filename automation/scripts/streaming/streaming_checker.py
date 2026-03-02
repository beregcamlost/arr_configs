#!/usr/bin/env python3
"""Streaming Availability Checker — CLI entry point.

Cross-references Radarr/Sonarr library against streaming providers (Netflix,
Disney+, etc.) via TMDB Watch Providers API. Tracks state across scans to detect
when content leaves streaming. Supports tagging, reporting, and confirm-delete flow.
"""

import json
import logging
import sys
import time

import click

from streaming.arr_client import (
    add_tag_to_item,
    delete_item,
    ensure_tag,
    fetch_movies,
    fetch_series,
    get_item,
    get_tag_id,
    remove_tag_from_item,
)
from streaming.config import PROVIDER_MAP, load_config
from streaming.db import (
    get_active_matches,
    get_active_matches_filtered,
    get_left_streaming,
    get_scan_history,
    get_summary_stats,
    init_db,
    mark_deleted,
    mark_left_streaming,
    record_scan,
    touch_keep_local_items,
    update_streaming_seasons,
    upsert_streaming_item,
)
from streaming.discord import (
    format_size,
    notify_deletion,
    notify_scan_results,
    notify_stale_cleanup,
)
from streaming.emby_client import get_last_played_map, is_playing, refresh_library
from streaming.streaming_api_client import get_season_availability
from streaming.tmdb_client import batch_check

log = logging.getLogger("streaming_checker")

TAG_LABEL = "streaming-available"


def _get_keep_local_set(cfg):
    """Build set of (arr_id, media_type) for keep-local tagged items."""
    keep_local_set = set()
    try:
        kl_radarr = get_tag_id(cfg.radarr_url, cfg.radarr_key, "keep-local")
        if kl_radarr:
            for m in fetch_movies(cfg.radarr_url, cfg.radarr_key):
                if kl_radarr in m.get("tags", []):
                    keep_local_set.add((m["arr_id"], "movie"))
    except Exception:
        log.debug("Could not check Radarr keep-local tags")
    try:
        kl_sonarr = get_tag_id(cfg.sonarr_url, cfg.sonarr_key, "keep-local")
        if kl_sonarr:
            for s in fetch_series(cfg.sonarr_url, cfg.sonarr_key):
                if kl_sonarr in s.get("tags", []):
                    keep_local_set.add((s["arr_id"], "tv"))
    except Exception:
        log.debug("Could not check Sonarr keep-local tags")
    return keep_local_set


def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
def cli():
    """Streaming Availability Checker — cross-reference your library against streaming providers."""
    pass


@cli.command()
@click.option("--country", default=None, help="ISO 3166-1 country code (default: CL)")
@click.option("--providers", default=None, help="Comma-separated provider names (default: netflix,disney)")
@click.option("--dry-run", is_flag=True, help="Check availability without modifying tags")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--db-path", default=None, help="Path to state database")
def scan(country, providers, dry_run, verbose, db_path):
    """Scan library against streaming providers and update state."""
    _setup_logging(verbose)
    cfg = load_config(country, providers, dry_run, verbose, db_path)

    if cfg.dry_run:
        log.info("DRY RUN — no tags will be modified")

    start_time = time.time()
    scan_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1. Init state DB
    init_db(cfg.db_path)

    # 2. Fetch library
    log.info("Fetching movies from Radarr...")
    movies = fetch_movies(cfg.radarr_url, cfg.radarr_key)
    log.info("Fetched %d movies", len(movies))

    log.info("Fetching series from Sonarr...")
    series = fetch_series(cfg.sonarr_url, cfg.sonarr_key)
    log.info("Fetched %d series", len(series))

    # 3. Filter out keep-local tagged items
    keep_local_radarr = ensure_tag(cfg.radarr_url, cfg.radarr_key, "keep-local") if not cfg.dry_run else None
    keep_local_sonarr = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, "keep-local") if not cfg.dry_run else None

    all_items = []
    keep_local_ids = []
    for m in movies:
        if keep_local_radarr and keep_local_radarr in m.get("tags", []):
            log.debug("Skipping keep-local: %s", m["title"])
            keep_local_ids.append((m["arr_id"], m["media_type"]))
            continue
        all_items.append(m)
    for s in series:
        if keep_local_sonarr and keep_local_sonarr in s.get("tags", []):
            log.debug("Skipping keep-local: %s", s["title"])
            keep_local_ids.append((s["arr_id"], s["media_type"]))
            continue
        all_items.append(s)

    # Touch keep-local items in DB so they aren't flagged as left-streaming
    if keep_local_ids:
        touched = touch_keep_local_items(cfg.db_path, keep_local_ids, scan_time)
        if touched:
            log.info("Touched %d keep-local DB records", touched)

    log.info("Checking %d items against TMDB (providers: %s, country: %s)",
             len(all_items), cfg.providers, cfg.country)

    # 4. Batch-check TMDB
    tmdb_results = batch_check(
        cfg.tmdb_api_key, all_items, cfg.provider_ids,
        country=cfg.country, max_workers=10,
    )

    # 5. Process matches
    streaming_tag_radarr = ensure_tag(cfg.radarr_url, cfg.radarr_key, TAG_LABEL) if not cfg.dry_run else None
    streaming_tag_sonarr = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, TAG_LABEL) if not cfg.dry_run else None

    new_items = []
    total_matches = 0

    # Build lookup for items by key
    items_by_key = {}
    for item in all_items:
        key = (item["tmdb_id"], item["media_type"])
        items_by_key[key] = item

    for key, matched_providers in tmdb_results.items():
        item = items_by_key.get(key)
        if not item:
            continue
        for provider in matched_providers:
            total_matches += 1
            is_new = upsert_streaming_item(
                cfg.db_path,
                tmdb_id=item["tmdb_id"],
                media_type=item["media_type"],
                provider_id=provider["provider_id"],
                provider_name=provider["provider_name"],
                title=item["title"],
                year=item.get("year"),
                arr_id=item["arr_id"],
                library=item.get("library"),
                size_bytes=item.get("size_bytes"),
                path=item.get("path"),
            )
            if is_new:
                new_items.append({
                    **item,
                    "provider_name": provider["provider_name"],
                    "provider_id": provider["provider_id"],
                })

            # Tag in arr
            if not cfg.dry_run:
                if item["media_type"] == "movie":
                    add_tag_to_item(cfg.radarr_url, cfg.radarr_key, "movie",
                                    item["arr_id"], streaming_tag_radarr)
                else:
                    add_tag_to_item(cfg.sonarr_url, cfg.sonarr_key, "series",
                                    item["arr_id"], streaming_tag_sonarr)

    # 6. Mark left-streaming items
    left_items = mark_left_streaming(cfg.db_path, scan_time)
    if left_items:
        log.info("%d items left streaming", len(left_items))
        if not cfg.dry_run:
            for item in left_items:
                # Remove streaming-available tag
                if item["media_type"] == "movie":
                    remove_tag_from_item(cfg.radarr_url, cfg.radarr_key, "movie",
                                         item["arr_id"], streaming_tag_radarr)
                else:
                    remove_tag_from_item(cfg.sonarr_url, cfg.sonarr_key, "series",
                                         item["arr_id"], streaming_tag_sonarr)

    # 7. Record scan
    duration = time.time() - start_time
    record_scan(
        cfg.db_path, cfg.country,
        movies_checked=len(movies),
        series_checked=len(series),
        matches_found=total_matches,
        newly_streaming=len(new_items),
        left_streaming=len(left_items),
        duration_seconds=duration,
    )

    # 8. Discord notification
    if cfg.discord_webhook_url and (new_items or left_items):
        stats = {
            "movies_checked": len(movies),
            "series_checked": len(series),
            "matches_found": total_matches,
            "duration_seconds": duration,
        }
        notify_scan_results(cfg.discord_webhook_url, new_items, left_items, stats)

    # 9. Summary
    click.echo(f"\nScan complete in {duration:.1f}s")
    click.echo(f"  Movies checked:  {len(movies)}")
    click.echo(f"  Series checked:  {len(series)}")
    click.echo(f"  Total matches:   {total_matches}")
    click.echo(f"  Newly streaming: {len(new_items)}")
    click.echo(f"  Left streaming:  {len(left_items)}")
    if cfg.dry_run:
        click.echo("  (dry-run — no tags modified)")


@cli.command("check-seasons")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--dry-run", is_flag=True, help="Check seasons without tagging keep-local")
@click.option("--db-path", default=None, help="Path to state database")
def check_seasons(verbose, dry_run, db_path):
    """Check per-season streaming availability and auto-tag keep-local."""
    _setup_logging(verbose)
    cfg = load_config(dry_run=dry_run, verbose=verbose, db_path=db_path)

    if not cfg.rapidapi_key:
        click.echo("Error: RAPIDAPI_KEY environment variable is required for check-seasons")
        sys.exit(1)

    init_db(cfg.db_path)

    # Get all active TV matches
    active = get_active_matches_filtered(cfg.db_path)
    tv_matches = [a for a in active if a["media_type"] == "tv" and a.get("tmdb_id")]

    if not tv_matches:
        click.echo("No active TV matches to check.")
        return

    # Deduplicate by tmdb_id — we only need one API call per series
    seen_tmdb = {}
    for item in tv_matches:
        tid = item["tmdb_id"]
        if tid not in seen_tmdb:
            seen_tmdb[tid] = []
        seen_tmdb[tid].append(item)

    log.info("Checking %d unique TV series for per-season availability", len(seen_tmdb))

    # Get owned season info from Sonarr
    series_list = fetch_series(cfg.sonarr_url, cfg.sonarr_key)
    sonarr_by_tmdb = {s["tmdb_id"]: s for s in series_list if s["tmdb_id"]}

    tagged_count = 0
    checked_count = 0

    for tmdb_id, db_items in seen_tmdb.items():
        # Get per-season streaming data from API
        season_avail = get_season_availability(
            cfg.rapidapi_key, tmdb_id, country=cfg.country,
        )
        checked_count += 1

        if not season_avail:
            log.debug("No season data for TMDB %s (%s)", tmdb_id, db_items[0]["title"])
            continue

        # Get owned seasons from Sonarr
        sonarr_item = sonarr_by_tmdb.get(tmdb_id)
        owned_seasons = set(sonarr_item["season_numbers"]) if sonarr_item else set()
        season_count = len(owned_seasons) if owned_seasons else None

        if not owned_seasons:
            log.debug("No owned seasons for TMDB %s (%s)", tmdb_id, db_items[0]["title"])
            continue

        title = db_items[0]["title"]

        # Map TMDB provider IDs to Streaming Availability API service IDs
        # TMDB names like "Netflix", "Disney Plus" → API IDs like "netflix", "disney"
        _provider_to_service = {
            8: "netflix",       # Netflix
            337: "disney",      # Disney Plus
            384: "hbo",         # HBO Max
            119: "prime",       # Amazon Prime
            350: "apple",       # Apple TV+
            531: "paramount",   # Paramount+
        }

        # For each provider match, determine which owned seasons are streaming
        for db_item in db_items:
            service_id = _provider_to_service.get(db_item["provider_id"], "")

            # Find which owned seasons are on this provider
            streaming_nums = []
            for snum, providers in season_avail.items():
                if service_id and service_id in providers:
                    streaming_nums.append(snum)

            streaming_seasons_json = json.dumps(sorted(streaming_nums))

            # Update DB
            update_streaming_seasons(
                cfg.db_path, tmdb_id, db_item["provider_id"],
                streaming_seasons_json, season_count=season_count,
            )

            # Check if we own seasons NOT available on this provider
            streaming_set = set(streaming_nums)
            missing_on_provider = owned_seasons - streaming_set

            if missing_on_provider and sonarr_item:
                sorted_owned = sorted(owned_seasons)
                sorted_streaming = sorted(streaming_set & owned_seasons)
                log.info(
                    "%s: own S%s, %s has S%s — missing S%s",
                    title,
                    ",".join(str(s) for s in sorted_owned),
                    db_item["provider_name"],
                    ",".join(str(s) for s in sorted_streaming) if sorted_streaming else "none",
                    ",".join(str(s) for s in sorted(missing_on_provider)),
                )

                if not dry_run:
                    keep_tag = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, "keep-local")
                    add_tag_to_item(
                        cfg.sonarr_url, cfg.sonarr_key, "series",
                        sonarr_item["arr_id"], keep_tag,
                    )
                    log.info("Tagged %s as keep-local", title)
                    tagged_count += 1
                else:
                    click.echo(f"  [dry-run] Would tag keep-local: {title}")
                    tagged_count += 1
                # Only tag once per series (any provider mismatch is enough)
                break

    click.echo(f"\ncheck-seasons complete")
    click.echo(f"  Series checked:  {checked_count}")
    click.echo(f"  Keep-local tags: {tagged_count}")
    if dry_run:
        click.echo("  (dry-run — no tags modified)")


@cli.command()
@click.option("--json-output", "--json", "json_out", is_flag=True, help="Output as JSON")
@click.option("--provider", default=None, help="Filter by provider name (case-insensitive)")
@click.option("--library", default=None, help="Filter by library name (case-insensitive)")
@click.option("--min-size", default=None, type=float, help="Minimum size in GB")
@click.option("--sort-by", default="title", type=click.Choice(["title", "size", "date", "provider"]),
              help="Sort order")
@click.option("--since-days", default=None, type=int, help="Only items first seen within N days")
@click.option("--no-play-days", default=None, type=int, help="Only show items with no Emby plays in last N days")
@click.option("--db-path", default=None, help="Path to state database")
def report(json_out, provider, library, min_size, sort_by, since_days, no_play_days, db_path):
    """Show current streaming availability status from state DB."""
    _setup_logging(False)
    cfg = load_config(db_path=db_path)

    init_db(cfg.db_path)

    min_size_bytes = int(min_size * 1_000_000_000) if min_size is not None else None
    active = get_active_matches_filtered(
        cfg.db_path, provider=provider, library=library,
        min_size=min_size_bytes, since_days=since_days, sort_by=sort_by,
    )
    left = get_left_streaming(cfg.db_path)

    # Filter out keep-local tagged items
    keep_local_set = set()
    if active or left:
        try:
            keep_local_set = _get_keep_local_set(cfg)
            if keep_local_set:
                active = [a for a in active
                          if (a.get("arr_id"), a.get("media_type")) not in keep_local_set]
                left = [l for l in left
                        if (l.get("arr_id"), l.get("media_type")) not in keep_local_set]
        except Exception as e:
            log.warning("Could not filter keep-local items: %s", e)

    # Enrich with Emby last-played data
    play_map = {}
    if no_play_days is not None and cfg.emby_api_key:
        try:
            play_map = get_last_played_map(cfg.emby_url, cfg.emby_api_key)
        except Exception as e:
            log.warning("Could not fetch Emby play history: %s", e)

    if play_map and no_play_days is not None:
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc)
        filtered = []
        for item in active:
            path = item.get("path", "")
            last_played = play_map.get(path)
            item["_last_played"] = last_played
            if last_played:
                try:
                    played_dt = datetime.fromisoformat(last_played.replace("Z", "+00:00"))
                    days_ago = (cutoff - played_dt).days
                    item["_days_ago"] = days_ago
                    if days_ago >= no_play_days:
                        filtered.append(item)
                except (ValueError, TypeError):
                    item["_days_ago"] = None
                    filtered.append(item)
            else:
                item["_days_ago"] = None
                filtered.append(item)  # never played → include
        active = filtered
    elif play_map:
        # Enrich without filtering
        for item in active:
            path = item.get("path", "")
            last_played = play_map.get(path)
            item["_last_played"] = last_played

    if json_out:
        click.echo(json.dumps({"active": active, "left_streaming": left}, indent=2))
        return

    if not active and not left:
        click.echo("No streaming matches in database.")
        return

    if active:
        # Group by provider
        by_provider = {}
        for item in active:
            pname = item.get("provider_name", "Unknown")
            by_provider.setdefault(pname, []).append(item)

        total_size = sum(item.get("size_bytes", 0) or 0 for item in active)

        click.echo(f"\n=== Active Streaming Matches ({len(active)}) — {format_size(total_size)} ===")
        for prov_name, items in sorted(by_provider.items()):
            psize = sum(it.get("size_bytes", 0) or 0 for it in items)
            click.echo(f"\n  {prov_name} ({len(items)} items, {format_size(psize)}):")
            for it in items:  # already sorted by DB query
                size = format_size(it.get("size_bytes", 0))
                extras = []

                # Season info for TV items
                sc = it.get("season_count")
                ss = it.get("streaming_seasons")
                if sc and ss:
                    try:
                        streaming_list = json.loads(ss)
                        extras.append(f"{len(streaming_list)}/{sc} seasons")
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Play history
                days_ago = it.get("_days_ago")
                if "_last_played" in it:
                    if it["_last_played"] is None:
                        extras.append("never played")
                    elif days_ago is not None:
                        extras.append(f"played {days_ago}d ago")

                # Keep-local indicator
                if (it.get("arr_id"), it.get("media_type")) in keep_local_set:
                    extras.append("keep-local")

                extra_str = " ".join(f"[{e}]" for e in extras)
                if extra_str:
                    extra_str = " " + extra_str
                click.echo(f"    - {it['title']} ({it.get('year', '?')}) [{it.get('library', '?')}] {size}{extra_str}")

    if left:
        click.echo(f"\n=== Left Streaming ({len(left)}) ===")
        for it in sorted(left, key=lambda x: x.get("title", "")):
            click.echo(f"  - {it['title']} ({it.get('year', '?')}) — was on {it.get('provider_name', '?')}")


@cli.command("confirm-delete")
@click.option("--yes", is_flag=True, required=True, help="Required safety gate")
@click.option("--provider", default=None, help="Only delete items from this provider")
@click.option("--library", default=None, help="Only delete items from this library")
@click.option("--min-size", default=None, type=float, help="Minimum size in GB")
@click.option("--tmdb-ids", default=None, help="Comma-separated TMDB IDs to delete")
@click.option("--no-play-days", default=None, type=int,
              help="Only delete items with no Emby plays in last N days")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without acting")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--db-path", default=None, help="Path to state database")
def confirm_delete(yes, provider, library, min_size, tmdb_ids, no_play_days, dry_run, verbose, db_path):
    """Delete items that are available on streaming. Requires --yes flag."""
    _setup_logging(verbose)
    cfg = load_config(dry_run=dry_run, verbose=verbose, db_path=db_path)

    init_db(cfg.db_path)

    min_size_bytes = int(min_size * 1_000_000_000) if min_size is not None else None
    active = get_active_matches_filtered(
        cfg.db_path, provider=provider, library=library, min_size=min_size_bytes,
    )

    if tmdb_ids:
        id_set = {int(x.strip()) for x in tmdb_ids.split(",")}
        active = [a for a in active if a["tmdb_id"] in id_set]

    # Filter by Emby play history
    if no_play_days is not None and cfg.emby_api_key:
        try:
            play_map = get_last_played_map(cfg.emby_url, cfg.emby_api_key)
        except Exception as e:
            log.warning("Could not fetch Emby play history: %s", e)
            play_map = {}

        if play_map:
            from datetime import datetime, timezone
            cutoff = datetime.now(timezone.utc)
            filtered = []
            for item in active:
                path = item.get("path", "")
                last_played = play_map.get(path)
                if last_played:
                    try:
                        played_dt = datetime.fromisoformat(last_played.replace("Z", "+00:00"))
                        days_ago = (cutoff - played_dt).days
                        if days_ago >= no_play_days:
                            filtered.append(item)
                    except (ValueError, TypeError):
                        filtered.append(item)
                else:
                    filtered.append(item)  # never played → include
            active = filtered

    # Filter out keep-local tagged items
    try:
        keep_local_set = _get_keep_local_set(cfg)
        if keep_local_set:
            active = [a for a in active
                      if (a.get("arr_id"), a.get("media_type")) not in keep_local_set]
    except Exception as e:
        log.warning("Could not filter keep-local items: %s", e)

    if not active:
        click.echo("No items to delete.")
        return

    # Deduplicate by arr_id + media_type (item may match multiple providers)
    seen = set()
    to_delete = []
    for item in active:
        key = (item["arr_id"], item["media_type"])
        if key in seen:
            continue
        seen.add(key)
        to_delete.append(item)

    click.echo(f"Will delete {len(to_delete)} items:")
    total_size = 0
    for it in to_delete:
        size = it.get("size_bytes", 0) or 0
        total_size += size
        click.echo(f"  - {it['title']} ({it.get('year', '?')}) [{it.get('library', '?')}] {format_size(size)}")
    click.echo(f"Total space to free: {format_size(total_size)}")

    if dry_run:
        click.echo("\n(dry-run — no deletions performed)")
        return

    # Double-check keep-local tags before each delete
    keep_local_radarr = ensure_tag(cfg.radarr_url, cfg.radarr_key, "keep-local")
    keep_local_sonarr = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, "keep-local")

    deleted_items = []
    freed_bytes = 0

    for item in to_delete:
        if item["media_type"] == "movie":
            base_url = cfg.radarr_url
            api_key = cfg.radarr_key
            app = "movie"
            keep_tag = keep_local_radarr
        else:
            base_url = cfg.sonarr_url
            api_key = cfg.sonarr_key
            app = "series"
            keep_tag = keep_local_sonarr

        # Re-fetch to check keep-local
        current = get_item(base_url, api_key, app, item["arr_id"])
        if current is None:
            log.warning("Item %s not found in arr, skipping", item["title"])
            continue
        if keep_tag in current.get("tags", []):
            log.info("Skipping %s — has keep-local tag", item["title"])
            continue

        # Check if playing in Emby
        if cfg.emby_api_key and item.get("path"):
            if is_playing(cfg.emby_url, cfg.emby_api_key, item["path"]):
                log.info("Skipping %s — currently being played", item["title"])
                continue

        log.info("Deleting %s (%s/%d)", item["title"], app, item["arr_id"])
        delete_item(base_url, api_key, app, item["arr_id"])

        # Mark all provider entries as deleted
        for match in active:
            if match["arr_id"] == item["arr_id"] and match["media_type"] == item["media_type"]:
                mark_deleted(cfg.db_path, match["tmdb_id"], match["media_type"], match["provider_id"])

        deleted_items.append(item)
        freed_bytes += item.get("size_bytes", 0) or 0

    # Refresh Emby
    if deleted_items and cfg.emby_api_key:
        try:
            refresh_library(cfg.emby_url, cfg.emby_api_key)
        except Exception as e:
            log.warning("Emby refresh failed: %s", e)

    # Discord notification
    if deleted_items and cfg.discord_webhook_url:
        notify_deletion(cfg.discord_webhook_url, deleted_items, freed_bytes)

    click.echo(f"\nDeleted {len(deleted_items)} items, freed {format_size(freed_bytes)}")


@cli.command()
@click.option("--json-output", "--json", "json_out", is_flag=True, help="Output as JSON")
@click.option("--db-path", default=None, help="Path to state database")
def summary(json_out, db_path):
    """Show summary statistics for streaming matches."""
    _setup_logging(False)
    cfg = load_config(db_path=db_path)

    init_db(cfg.db_path)
    stats = get_summary_stats(cfg.db_path)
    history = get_scan_history(cfg.db_path, limit=5)

    if json_out:
        click.echo(json.dumps({"stats": stats, "recent_scans": history}, indent=2))
        return

    if stats["total_active"] == 0:
        click.echo("No active streaming matches.")
        return

    click.echo(f"\n=== Streaming Summary ===")
    click.echo(f"  Active matches: {stats['total_active']}")
    click.echo(f"  Reclaimable:    {format_size(stats['total_size_bytes'])}")

    if stats["by_provider"]:
        click.echo(f"\n  By Provider:")
        for p in stats["by_provider"]:
            click.echo(f"    {p['provider_name']}: {p['count']} items ({format_size(p['size_bytes'])})")

    if stats["by_library"]:
        click.echo(f"\n  By Library (deduplicated):")
        for lib in stats["by_library"]:
            click.echo(f"    {lib['library']}: {lib['count']} unique items")

    if stats["last_scan"]:
        ls = stats["last_scan"]
        click.echo(f"\n  Last scan: {ls['timestamp']} ({ls['matches_found']} matches in {ls['duration_seconds']:.1f}s)")

    if history:
        click.echo(f"\n  Recent scans:")
        for h in history:
            click.echo(f"    {h['timestamp']} — {h['matches_found']} matches, "
                       f"+{h['newly_streaming']} new, -{h['left_streaming']} left")


@cli.command("stale-cleanup")
@click.option("--no-play-days", default=365, type=int,
              help="Delete items not played in this many days (default: 365)")
@click.option("--min-size-gb", default=3.0, type=float,
              help="Auto-delete items larger than this (default: 3.0 GB)")
@click.option("--yes", is_flag=True, required=True, help="Required safety gate")
@click.option("--dry-run", is_flag=True, help="Show what would happen without deleting")
@click.option("--verbose", is_flag=True, help="Enable debug logging")
@click.option("--db-path", default=None, help="Path to streaming state database")
def stale_cleanup(no_play_days, min_size_gb, yes, dry_run, verbose, db_path):
    """Delete stale library items not played in N days.

    Scans ALL library items (not just streaming matches). Auto-deletes items
    above the size threshold and sends a Discord report for everything.
    Excludes keep-local tagged items and active streaming matches.
    """
    _setup_logging(verbose)
    cfg = load_config(dry_run=dry_run, verbose=verbose, db_path=db_path)

    if not cfg.emby_api_key:
        click.echo("Error: EMBY_API_KEY is required for stale-cleanup")
        sys.exit(1)

    init_db(cfg.db_path)

    # 1. Fetch all library items
    log.info("Fetching movies from Radarr...")
    movies = fetch_movies(cfg.radarr_url, cfg.radarr_key)
    log.info("Fetching series from Sonarr...")
    series = fetch_series(cfg.sonarr_url, cfg.sonarr_key)

    all_items = [m for m in movies if m.get("has_file")] + list(series)
    log.info("Library: %d movies + %d series = %d items",
             len(movies), len(series), len(all_items))

    # 2. Get Emby play history
    log.info("Fetching Emby play history...")
    try:
        play_map = get_last_played_map(cfg.emby_url, cfg.emby_api_key)
    except Exception as e:
        log.error("Failed to fetch Emby play history: %s", e)
        sys.exit(1)
    log.info("Play history: %d paths with playback data", len(play_map))

    # 3. Build exclusion sets
    # a) Keep-local tags
    keep_local_set = _get_keep_local_set(cfg)
    log.info("Keep-local items: %d", len(keep_local_set))

    # b) Active streaming matches (handled by tier 1 weekly cleanup)
    streaming_paths = set()
    try:
        active_streaming = get_active_matches_filtered(cfg.db_path)
        for item in active_streaming:
            if item.get("path"):
                streaming_paths.add(item["path"])
    except Exception:
        log.debug("Could not load streaming matches for exclusion")
    log.info("Active streaming matches: %d (excluded)", len(streaming_paths))

    # 4. Filter to stale items
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc)
    min_size_bytes = int(min_size_gb * 1_000_000_000)

    stale_items = []
    for item in all_items:
        # Exclude keep-local
        if (item["arr_id"], item["media_type"]) in keep_local_set:
            continue
        # Exclude active streaming matches
        if item.get("path") in streaming_paths:
            continue
        # Check play history
        path = item.get("path", "")
        last_played = play_map.get(path)
        if last_played:
            try:
                played_dt = datetime.fromisoformat(last_played.replace("Z", "+00:00"))
                days_ago = (cutoff - played_dt).days
                if days_ago < no_play_days:
                    continue  # played recently enough
                item["_days_ago"] = days_ago
            except (ValueError, TypeError):
                item["_days_ago"] = None
        else:
            item["_days_ago"] = None  # never played

        stale_items.append(item)

    log.info("Stale items (not played in %dd): %d", no_play_days, len(stale_items))

    if not stale_items:
        click.echo(f"No items found that haven't been played in {no_play_days} days.")
        return

    # 5. Split into auto-delete (> threshold) and report-only (< threshold)
    to_delete = []
    to_report = []
    for item in stale_items:
        size = item.get("size_bytes", 0) or 0
        if size > min_size_bytes:
            to_delete.append(item)
        else:
            to_report.append(item)

    click.echo(f"\nStale items (not played in {no_play_days}d): {len(stale_items)}")
    click.echo(f"  Auto-delete (>{min_size_gb} GB): {len(to_delete)}")
    click.echo(f"  Report only (<={min_size_gb} GB): {len(to_report)}")

    if to_delete:
        total_delete_size = sum(it.get("size_bytes", 0) or 0 for it in to_delete)
        click.echo(f"\nWill delete {len(to_delete)} items ({format_size(total_delete_size)}):")
        for it in sorted(to_delete, key=lambda x: x.get("size_bytes", 0) or 0, reverse=True):
            size = format_size(it.get("size_bytes", 0) or 0)
            days = it.get("_days_ago")
            play_label = f"played {days}d ago" if days else "never played"
            click.echo(f"  - {it['title']} ({it.get('year', '?')}) [{it.get('library', '?')}] {size} [{play_label}]")

    if to_report:
        click.echo(f"\nReport only ({len(to_report)} items):")
        for it in sorted(to_report, key=lambda x: x.get("size_bytes", 0) or 0, reverse=True):
            size = format_size(it.get("size_bytes", 0) or 0)
            days = it.get("_days_ago")
            play_label = f"played {days}d ago" if days else "never played"
            click.echo(f"  - {it['title']} ({it.get('year', '?')}) [{it.get('library', '?')}] {size} [{play_label}]")

    if dry_run:
        click.echo("\n(dry-run — no deletions performed)")
        return

    # 6. Delete items above threshold
    keep_local_radarr = ensure_tag(cfg.radarr_url, cfg.radarr_key, "keep-local")
    keep_local_sonarr = ensure_tag(cfg.sonarr_url, cfg.sonarr_key, "keep-local")

    deleted_items = []
    freed_bytes = 0

    for item in to_delete:
        if item["media_type"] == "movie":
            base_url, api_key, app = cfg.radarr_url, cfg.radarr_key, "movie"
            keep_tag = keep_local_radarr
        else:
            base_url, api_key, app = cfg.sonarr_url, cfg.sonarr_key, "series"
            keep_tag = keep_local_sonarr

        # Re-fetch to verify keep-local
        current = get_item(base_url, api_key, app, item["arr_id"])
        if current is None:
            log.warning("Item %s not found in arr, skipping", item["title"])
            continue
        if keep_tag in current.get("tags", []):
            log.info("Skipping %s — has keep-local tag", item["title"])
            continue

        # Check if playing in Emby
        if item.get("path"):
            if is_playing(cfg.emby_url, cfg.emby_api_key, item["path"]):
                log.info("Skipping %s — currently being played", item["title"])
                continue

        log.info("Deleting %s (%s/%d)", item["title"], app, item["arr_id"])
        delete_item(base_url, api_key, app, item["arr_id"])
        deleted_items.append(item)
        freed_bytes += item.get("size_bytes", 0) or 0

    # 7. Refresh Emby
    if deleted_items:
        try:
            refresh_library(cfg.emby_url, cfg.emby_api_key)
        except Exception as e:
            log.warning("Emby refresh failed: %s", e)

    # 8. Discord notification (always send if there are stale items)
    if cfg.discord_webhook_url and (deleted_items or to_report):
        notify_stale_cleanup(
            cfg.discord_webhook_url, deleted_items, to_report,
            freed_bytes, no_play_days, min_size_gb,
        )

    click.echo(f"\nDeleted {len(deleted_items)} items, freed {format_size(freed_bytes)}")
    if to_report:
        click.echo(f"Reported {len(to_report)} items below {min_size_gb} GB threshold")


@cli.command()
@click.option("--country", default="CL", help="Country code for provider list")
def providers(country):
    """List available streaming providers for a country from TMDB."""
    _setup_logging(False)
    cfg = load_config()

    import requests
    url = f"https://api.themoviedb.org/3/watch/providers/movie"
    resp = requests.get(url, params={"api_key": cfg.tmdb_api_key, "watch_region": country}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    providers_list = data.get("results", [])
    providers_list.sort(key=lambda p: p.get("display_priority", 999))

    click.echo(f"\nStreaming providers available in {country}:")
    click.echo(f"{'ID':>6}  {'Name':<40}  {'Priority':>8}")
    click.echo("-" * 60)
    for p in providers_list:
        pid = p["provider_id"]
        name = p["provider_name"]
        priority = p.get("display_priority", "?")
        marker = " <--" if pid in PROVIDER_MAP.values() else ""
        click.echo(f"{pid:>6}  {name:<40}  {priority:>8}{marker}")


if __name__ == "__main__":
    cli()
