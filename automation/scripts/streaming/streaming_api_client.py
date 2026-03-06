"""Streaming Availability API client (Movie of the Night / RapidAPI)."""

import logging
import re

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://streaming-availability.p.rapidapi.com"


def _rapidapi_headers(api_key):
    return {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "streaming-availability.p.rapidapi.com",
    }


def _parse_season_number(title):
    """Extract season number from title like 'Season 1'. Returns int or None."""
    m = re.search(r"(\d+)", title or "")
    return int(m.group(1)) if m else None


def get_season_availability(api_key, tmdb_id, country="cl"):
    """Get per-season streaming availability from the Streaming Availability API.

    Args:
        api_key: RapidAPI key
        tmdb_id: TMDB series ID (integer)
        country: ISO 3166-1 alpha-2 country code (lowercase)

    Returns:
        dict mapping season_number (int) → list of provider name strings.
        e.g. {1: ["netflix"], 2: ["netflix"], 3: []}
        Returns empty dict on error (fail-open).
    """
    # API requires tv/ prefix for TMDB TV series IDs
    url = f"{BASE_URL}/shows/tv/{tmdb_id}"
    headers = _rapidapi_headers(api_key)
    params = {
        "series_granularity": "season",
        "country": country.lower(),
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            log.warning("Streaming Availability API rate limit hit (429) for TMDB %s", tmdb_id)
            return {}
        if resp.status_code in (401, 403):
            log.warning("Streaming Availability API auth error (%d) — check RAPIDAPI_KEY", resp.status_code)
            return {}
        if resp.status_code == 404:
            log.debug("TMDB %s not found in Streaming Availability API", tmdb_id)
            return {}
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Streaming Availability API error for TMDB %s: %s", tmdb_id, e)
        return {}

    data = resp.json()
    seasons = data.get("seasons", [])
    country_lower = country.lower()

    result = {}
    for idx, season in enumerate(seasons):
        # API doesn't include seasonNumber — parse from title or use 1-based index
        season_num = _parse_season_number(season.get("title"))
        if season_num is None:
            season_num = idx + 1
        streaming_options = season.get("streamingOptions", {}).get(country_lower, [])
        providers = []
        for opt in streaming_options:
            service = opt.get("service", {})
            service_id = service.get("id", "")
            if service_id and service_id not in providers:
                providers.append(service_id)
        result[season_num] = providers

    return result


# Mapping from MoTN service IDs to TMDB provider IDs (same as check-seasons)
SERVICE_TO_PROVIDER = {
    "netflix": 8, "disney": 337, "hbo": 384,
    "prime": 119, "apple": 350, "paramount": 531,
}


def get_streaming_providers(api_key, tmdb_id, media_type, country="cl"):
    """Check if a single item is available for streaming via Movie of the Night API.

    Args:
        api_key: RapidAPI key
        tmdb_id: TMDB ID (integer)
        media_type: 'movie' or 'tv'
        country: ISO 3166-1 alpha-2 country code (lowercase)

    Returns:
        list of dicts with {service_id, service_name} for subscription-type providers.
        Returns empty list on error (fail-open).
    """
    prefix = "movie" if media_type == "movie" else "tv"
    url = f"{BASE_URL}/shows/{prefix}/{tmdb_id}"
    headers = _rapidapi_headers(api_key)
    params = {"country": country.lower()}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 429:
            log.warning("RapidAPI rate limit hit (429) for TMDB %s", tmdb_id)
            return []
        if resp.status_code in (401, 403):
            log.warning("RapidAPI auth error (%d) — check RAPIDAPI_KEY", resp.status_code)
            return []
        if resp.status_code == 404:
            log.debug("TMDB %s (%s) not found in Streaming Availability API", tmdb_id, media_type)
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Streaming Availability API error for TMDB %s: %s", tmdb_id, e)
        return []

    data = resp.json()
    options = data.get("streamingOptions", {}).get(country.lower(), [])
    seen = set()
    result = []
    for opt in options:
        if opt.get("type") != "subscription":
            continue
        service = opt.get("service", {})
        sid = service.get("id", "")
        if sid and sid not in seen:
            seen.add(sid)
            result.append({"service_id": sid, "service_name": service.get("name", sid)})
    return result


def search_catalog(api_key, service, show_type, country="cl", limit=20):
    """Search streaming catalog for trending titles on a service.

    Args:
        api_key: RapidAPI key
        service: service name (e.g. "apple", "paramount", "hbo")
        show_type: "movie" or "series"
        country: ISO 3166-1 alpha-2 country code
        limit: max results to return (handles pagination)

    Returns:
        list of dicts with: tmdb_id, title, year, show_type, genres, imdb_id
        Returns empty list on error (fail-open).
    """
    headers = _rapidapi_headers(api_key)
    results = []
    cursor = None

    while len(results) < limit:
        params = {
            "country": country.lower(),
            "services": service,
            "show_type": show_type,
            "order_by": "popularity_1year",
            "desc": "true",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(f"{BASE_URL}/shows/search/filters",
                                headers=headers, params=params, timeout=15)
            if resp.status_code == 429:
                log.warning("RapidAPI rate limit hit (429) for catalog search")
                break
            if resp.status_code in (401, 403):
                log.warning("RapidAPI auth error (%d) — check RAPIDAPI_KEY", resp.status_code)
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Catalog search error: %s", e)
            break

        data = resp.json()
        for show in data.get("shows", []):
            # tmdbId format: "movie/12345" or "tv/12345"
            raw_tmdb = show.get("tmdbId", "")
            tmdb_id = int(raw_tmdb.split("/")[-1]) if "/" in raw_tmdb else 0
            results.append({
                "tmdb_id": tmdb_id,
                "title": show.get("title", ""),
                "year": show.get("releaseYear", 0),
                "show_type": show.get("showType", show_type),
                "genres": [g.get("name", g) if isinstance(g, dict) else g
                           for g in show.get("genres", [])],
                "imdb_id": show.get("imdbId", ""),
            })
            if len(results) >= limit:
                break

        if not data.get("hasMore"):
            break
        cursor = data.get("nextCursor")
        if not cursor:
            break

    return results[:limit]
