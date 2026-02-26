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
