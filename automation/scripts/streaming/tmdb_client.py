"""TMDB Watch Providers API client with batch ThreadPoolExecutor support."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

log = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"


def get_watch_providers(api_key, tmdb_id, media_type, country="CL"):
    """Get watch providers for a single title from TMDB.

    Args:
        api_key: TMDB API key
        tmdb_id: TMDB ID of the title
        media_type: 'movie' or 'tv'
        country: ISO 3166-1 country code

    Returns:
        dict with provider data for the country, or empty dict
    """
    url = f"{TMDB_BASE}/{media_type}/{tmdb_id}/watch/providers"
    resp = requests.get(url, params={"api_key": api_key}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", {}).get(country, {})


def check_streaming(api_key, tmdb_id, media_type, provider_ids, country="CL"):
    """Check if a title is available for streaming (flatrate) on given providers.

    Returns:
        list of matching providers as {provider_id, provider_name}
    """
    country_data = get_watch_providers(api_key, tmdb_id, media_type, country)
    flatrate = country_data.get("flatrate", [])
    matches = []
    for p in flatrate:
        if p["provider_id"] in provider_ids:
            matches.append({
                "provider_id": p["provider_id"],
                "provider_name": p["provider_name"],
            })
    return matches


def batch_check(api_key, items, provider_ids, country="CL", max_workers=10):
    """Batch-check multiple titles against streaming providers using ThreadPoolExecutor.

    Args:
        api_key: TMDB API key
        items: list of dicts with 'tmdb_id' and 'media_type' keys
        provider_ids: list of TMDB provider IDs to check against
        country: ISO 3166-1 country code
        max_workers: max concurrent threads

    Returns:
        dict keyed by (tmdb_id, media_type) → list of matching providers
    """
    results = {}

    def _check_one(item):
        tmdb_id = item["tmdb_id"]
        media_type = item["media_type"]
        if not tmdb_id:
            log.warning("Skipping item with tmdb_id=0: %s", item.get("title", "unknown"))
            return (tmdb_id, media_type), []
        try:
            matches = check_streaming(api_key, tmdb_id, media_type, provider_ids, country)
            return (tmdb_id, media_type), matches
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                log.warning("TMDB ID %s not found (404)", tmdb_id)
            else:
                log.error("TMDB API error for %s: %s", tmdb_id, e)
            return (tmdb_id, media_type), []
        except Exception as e:
            log.error("Unexpected error checking TMDB ID %s: %s", tmdb_id, e)
            return (tmdb_id, media_type), []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for item in items:
            futures.append(executor.submit(_check_one, item))
            time.sleep(0.05)  # Rate-limit submissions

        for future in as_completed(futures):
            key, matches = future.result()
            if matches:
                results[key] = matches

    return results
