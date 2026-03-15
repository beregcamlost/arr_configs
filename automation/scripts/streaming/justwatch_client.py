"""JustWatch unofficial GraphQL client for streaming availability cross-validation."""

import logging

import requests

log = logging.getLogger(__name__)

JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"

# JustWatch packageId → TMDB provider ID (same IDs for major providers)
JUSTWATCH_TO_PROVIDER = {
    8: 8,       # Netflix
    337: 337,   # Disney+
    1899: 384,  # HBO Max (JW 1899 → TMDB 384)
    119: 119,   # Amazon Prime
    350: 350,   # Apple TV+
    531: 531,   # Paramount+
    283: 283,   # Crunchyroll
}

# GraphQL query: search by title, return TMDB ID + FLATRATE offers
_SEARCH_QUERY = """
query GetTitleOffers($country: Country!, $searchQuery: String!, $first: Int!) {
  popularTitles(country: $country, first: $first, filter: {
    searchQuery: $searchQuery
  }) {
    edges {
      node {
        objectType
        content(country: $country, language: "en") {
          externalIds {
            tmdbId
          }
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          package {
            packageId
          }
        }
      }
    }
  }
}
"""


def get_justwatch_providers(tmdb_id, media_type, title, country="CL"):
    """Check streaming availability via JustWatch GraphQL.

    Searches by title, then matches the result with the correct TMDB ID
    to extract FLATRATE offers.

    Args:
        tmdb_id: TMDB ID (integer)
        media_type: 'movie' or 'tv'
        title: Item title for search query
        country: ISO 3166-1 alpha-2 country code (uppercase)

    Returns:
        (list[{"provider_id": int}], bool) — providers + API responded flag.
        Follows same (list, bool) contract as MoTN/Watchmode clients.
    """
    if not title:
        return [], True

    jw_type = "MOVIE" if media_type == "movie" else "SHOW"

    variables = {
        "country": country.upper(),
        "searchQuery": title,
        "first": 5,
    }

    try:
        resp = requests.post(
            JUSTWATCH_GRAPHQL_URL,
            json={"query": _SEARCH_QUERY, "variables": variables},
            timeout=15,
        )
        if resp.status_code in (429, 401, 403):
            log.warning("JustWatch API error (%d) for '%s'", resp.status_code, title)
            return [], False
        if resp.status_code == 404:
            return [], True
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("JustWatch API error for '%s': %s", title, e)
        return [], False

    data = resp.json()
    edges = (data.get("data") or {}).get("popularTitles", {}).get("edges", [])

    # Find the matching node by TMDB ID and object type
    target_tmdb = str(tmdb_id)
    matched_node = None
    for edge in edges:
        node = edge.get("node", {})
        if node.get("objectType") != jw_type:
            continue
        ext_ids = (node.get("content") or {}).get("externalIds") or {}
        if ext_ids.get("tmdbId") == target_tmdb:
            matched_node = node
            break

    if not matched_node:
        log.debug("JustWatch: no match for TMDB %s (%s) '%s'", tmdb_id, media_type, title)
        return [], True  # API responded, item not found

    # Extract FLATRATE offers
    seen = set()
    result = []
    for offer in matched_node.get("offers") or []:
        if offer.get("monetizationType") != "FLATRATE":
            continue
        pkg_id = (offer.get("package") or {}).get("packageId")
        provider_id = JUSTWATCH_TO_PROVIDER.get(pkg_id)
        if provider_id and provider_id not in seen:
            seen.add(provider_id)
            result.append({"provider_id": provider_id})

    return result, True
