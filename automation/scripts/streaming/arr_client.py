"""Radarr/Sonarr V3 API client for fetching library, managing tags, and deleting items."""

import logging

import requests

log = logging.getLogger(__name__)


def _headers(api_key):
    return {"X-Api-Key": api_key}


def _detect_library(path, media_type):
    """Detect library name from file path."""
    if not path:
        return media_type
    if media_type == "movie":
        if "/moviesanimated/" in path:
            return "moviesanimated"
        return "movies"
    else:
        if "/tvanimated/" in path:
            return "tvanimated"
        return "tv"


def fetch_movies(radarr_url, api_key):
    """Fetch all movies from Radarr.

    Returns:
        list of dicts with: tmdb_id, arr_id, title, year, path, size_bytes, tags, has_file, library
    """
    url = f"{radarr_url}/api/v3/movie"
    resp = requests.get(url, headers=_headers(api_key), timeout=120)
    resp.raise_for_status()
    movies = []
    for m in resp.json():
        path = m.get("path", "")
        movies.append({
            "tmdb_id": m.get("tmdbId", 0),
            "arr_id": m["id"],
            "title": m.get("title", ""),
            "year": m.get("year", 0),
            "path": path,
            "size_bytes": m.get("sizeOnDisk", 0),
            "tags": m.get("tags", []),
            "has_file": m.get("hasFile", False),
            "library": _detect_library(path, "movie"),
            "media_type": "movie",
        })
    return movies


def fetch_series(sonarr_url, api_key):
    """Fetch all series from Sonarr.

    Returns:
        list of dicts with: tmdb_id, arr_id, title, year, path, size_bytes, tags, library
    """
    url = f"{sonarr_url}/api/v3/series"
    resp = requests.get(url, headers=_headers(api_key), timeout=120)
    resp.raise_for_status()
    series = []
    for s in resp.json():
        path = s.get("path", "")
        # Sonarr uses tvdbId primarily; tmdbId may not always be present
        tmdb_id = s.get("tmdbId", 0) or 0
        stats = s.get("statistics", {})
        # Extract owned season numbers (seasons with at least one episode file)
        seasons_data = s.get("seasons", [])
        owned_seasons = [
            sn["seasonNumber"]
            for sn in seasons_data
            if sn.get("seasonNumber", 0) > 0
            and sn.get("statistics", {}).get("episodeFileCount", 0) > 0
        ]
        series.append({
            "tmdb_id": tmdb_id,
            "arr_id": s["id"],
            "title": s.get("title", ""),
            "year": s.get("year", 0),
            "path": path,
            "size_bytes": stats.get("sizeOnDisk", 0),
            "tags": s.get("tags", []),
            "library": _detect_library(path, "tv"),
            "media_type": "tv",
            "season_count": len(owned_seasons),
            "season_numbers": owned_seasons,
        })
    return series


def get_tag_id(base_url, api_key, tag_label):
    """Get tag ID by label. Returns int or None."""
    url = f"{base_url}/api/v3/tag"
    resp = requests.get(url, headers=_headers(api_key), timeout=10)
    resp.raise_for_status()
    for tag in resp.json():
        if tag["label"].lower() == tag_label.lower():
            return tag["id"]
    return None


def create_tag(base_url, api_key, tag_label):
    """Create a new tag and return its ID."""
    url = f"{base_url}/api/v3/tag"
    resp = requests.post(url, headers=_headers(api_key), json={"label": tag_label}, timeout=10)
    resp.raise_for_status()
    return resp.json()["id"]


def ensure_tag(base_url, api_key, tag_label):
    """Get or create a tag by label. Returns tag ID."""
    tag_id = get_tag_id(base_url, api_key, tag_label)
    if tag_id is None:
        tag_id = create_tag(base_url, api_key, tag_label)
    return tag_id


def get_item(base_url, api_key, app, arr_id):
    """Get a single item from Radarr/Sonarr.

    Args:
        app: 'movie' or 'series'
    """
    url = f"{base_url}/api/v3/{app}/{arr_id}"
    resp = requests.get(url, headers=_headers(api_key), timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def add_tag_to_item(base_url, api_key, app, arr_id, tag_id):
    """Add a tag to a Radarr/Sonarr item if not already present."""
    item = get_item(base_url, api_key, app, arr_id)
    if item is None:
        log.warning("Item %s/%s not found", app, arr_id)
        return
    tags = item.get("tags", [])
    if tag_id in tags:
        return
    tags.append(tag_id)
    item["tags"] = tags
    url = f"{base_url}/api/v3/{app}/{arr_id}"
    resp = requests.put(url, headers=_headers(api_key), json=item, timeout=10)
    resp.raise_for_status()


def remove_tag_from_item(base_url, api_key, app, arr_id, tag_id):
    """Remove a tag from a Radarr/Sonarr item."""
    item = get_item(base_url, api_key, app, arr_id)
    if item is None:
        log.warning("Item %s/%s not found", app, arr_id)
        return
    tags = item.get("tags", [])
    if tag_id not in tags:
        return
    tags.remove(tag_id)
    item["tags"] = tags
    url = f"{base_url}/api/v3/{app}/{arr_id}"
    resp = requests.put(url, headers=_headers(api_key), json=item, timeout=10)
    resp.raise_for_status()


def delete_item(base_url, api_key, app, arr_id, delete_files=True):
    """Delete an item from Radarr/Sonarr.

    Args:
        app: 'movie' or 'series'
        delete_files: if True, also delete files on disk
    """
    url = f"{base_url}/api/v3/{app}/{arr_id}"
    params = {"deleteFiles": str(delete_files).lower()}
    if app == "series":
        params["addImportListExclusion"] = "false"
    resp = requests.delete(url, headers=_headers(api_key), params=params, timeout=30)
    resp.raise_for_status()
