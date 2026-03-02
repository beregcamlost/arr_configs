"""Emby client for library refresh and playback detection."""

import logging
import os

import requests

log = logging.getLogger(__name__)


def refresh_library(emby_url, api_key):
    """Trigger a full Emby library refresh."""
    url = f"{emby_url}/Library/Refresh"
    resp = requests.post(url, params={"api_key": api_key}, timeout=10)
    resp.raise_for_status()
    log.info("Emby library refresh triggered")


def is_playing(emby_url, api_key, file_path):
    """Check if a file is currently being played in Emby.

    Args:
        file_path: Full path to the media file on disk

    Returns:
        True if the file is currently being played
    """
    url = f"{emby_url}/Sessions"
    resp = requests.get(url, params={"api_key": api_key}, timeout=10)
    resp.raise_for_status()
    for session in resp.json():
        now_playing = session.get("NowPlayingItem", {})
        if not now_playing:
            continue
        playing_path = now_playing.get("Path", "")
        if playing_path and os.path.normpath(playing_path) == os.path.normpath(file_path):
            return True
    return False


def _normalize_to_dir(file_path):
    """Normalize a file path to its series/movie directory.

    Movies: parent directory (e.g. /media/movies/Fight Club (1999)/)
    TV: series root before /Season (e.g. /media/tv/Breaking Bad/)
    """
    season_idx = file_path.find("/Season ")
    if season_idx >= 0:
        return file_path[:season_idx]
    return os.path.dirname(file_path)


def get_last_played_map(emby_url, api_key):
    """Build a map of directory paths to their last-played dates.

    Uses the Emby Activity Log (playback.stop events) to find actual play
    timestamps, then resolves ItemIds to file paths via the Items API.

    Returns:
        dict mapping dir_path (str) → last_played ISO string.
        Keeps the maximum (most recent) play date per directory path.
    """
    # 1. Collect all playback.stop events from activity log → {item_id: latest_date}
    item_dates = {}
    start_index = 0
    page_size = 500

    while True:
        try:
            resp = requests.get(
                f"{emby_url}/System/ActivityLog/Entries",
                params={
                    "api_key": api_key,
                    "StartIndex": start_index,
                    "Limit": page_size,
                    "hasUserId": "true",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Failed to fetch activity log at offset %d: %s", start_index, e)
            break

        entries = data.get("Items", [])
        if not entries:
            break

        for entry in entries:
            if entry.get("Type") != "playback.stop":
                continue
            item_id = entry.get("ItemId")
            date = entry.get("Date", "")
            if not item_id or not date:
                continue
            # Normalize date: strip sub-second precision for consistent comparison
            # "2026-03-01T23:52:59.8330000Z" → "2026-03-01T23:52:59Z"
            if "." in date:
                date = date[:date.index(".")] + "Z"
            existing = item_dates.get(item_id)
            if existing is None or date > existing:
                item_dates[item_id] = date

        total = data.get("TotalRecordCount", 0)
        start_index += len(entries)
        if start_index >= total:
            break

    if not item_dates:
        log.debug("No playback events found in activity log")
        return {}

    log.debug("Found %d unique items with playback events", len(item_dates))

    # 2. Resolve ItemIds to file paths in batches
    play_map = {}
    item_id_list = list(item_dates.keys())
    batch_size = 50

    for i in range(0, len(item_id_list), batch_size):
        batch = item_id_list[i:i + batch_size]
        ids_param = ",".join(str(x) for x in batch)
        try:
            resp = requests.get(
                f"{emby_url}/Items",
                params={
                    "api_key": api_key,
                    "Ids": ids_param,
                    "Fields": "Path",
                },
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json().get("Items", [])
        except Exception as e:
            log.warning("Failed to resolve item batch at offset %d: %s", i, e)
            continue

        for item in items:
            item_id = str(item.get("Id", ""))
            file_path = item.get("Path", "")
            if not file_path or item_id not in item_dates:
                continue

            dir_path = _normalize_to_dir(file_path)
            date = item_dates[item_id]

            existing = play_map.get(dir_path)
            if existing is None or date > existing:
                play_map[dir_path] = date

    log.debug("Built play map with %d directory paths", len(play_map))
    return play_map


def refresh_item(emby_url, api_key, file_path):
    """Refresh a specific Emby item by matching its file path.

    Searches Emby for the item by title extracted from the path,
    then matches by path to find the correct item and triggers a refresh.
    """
    # Extract a search term from the path (parent directory name)
    dirname = os.path.basename(os.path.dirname(file_path))
    # Strip year suffix like "Movie Name (2020)"
    search_term = dirname.split("(")[0].strip() if "(" in dirname else dirname

    if not search_term:
        log.warning("Could not extract search term from path: %s", file_path)
        return False

    # Search Emby
    url = f"{emby_url}/Items"
    params = {
        "api_key": api_key,
        "SearchTerm": search_term,
        "Recursive": "true",
        "Fields": "Path",
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()

    items = resp.json().get("Items", [])
    for item in items:
        item_path = item.get("Path", "")
        if item_path and os.path.normpath(item_path) == os.path.normpath(file_path):
            refresh_url = f"{emby_url}/Items/{item['Id']}/Refresh"
            resp = requests.post(refresh_url, params={"api_key": api_key}, timeout=10)
            resp.raise_for_status()
            log.info("Refreshed Emby item %s (%s)", item["Id"], search_term)
            return True

    log.warning("Emby item not found for path: %s", file_path)
    return False
