# Streaming Check on Import — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** On Sonarr/Radarr import, check if the imported item is on streaming services and tag it with `streaming-available` + upsert into the streaming DB.

**Architecture:** New `check-import` Click subcommand in `streaming_checker.py`. Uses Movie of the Night API (primary) with TMDB Watch Providers fallback. Called from import hook as a backgrounded subprocess. Fail-open on all errors.

**Tech Stack:** Python (Click CLI), requests, existing streaming_checker infrastructure (arr_client, db, config, discord, streaming_api_client, tmdb_client)

---

### Task 1: Add `get_streaming_providers()` to `streaming_api_client.py`

The existing API client has `get_season_availability()` (TV only) and `search_catalog()` (catalog search). We need a single-item lookup that works for both movies and TV, returning which providers stream the item.

**Files:**
- Modify: `automation/scripts/streaming/streaming_api_client.py`
- Test: `automation/scripts/streaming/tests/test_streaming_api_client.py`

**Step 1: Write the failing tests**

Add a new `TestGetStreamingProviders` class to `test_streaming_api_client.py`:

```python
class TestGetStreamingProviders:
    """Tests for get_streaming_providers() — single-item streaming lookup."""

    @patch("streaming.streaming_api_client.requests.get")
    def test_movie_found_on_netflix(self, mock_get):
        """Movie found on Netflix returns provider list."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "apple", "name": "Apple TV"}, "type": "rent"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None

        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == [{"service_id": "netflix", "service_name": "Netflix"}]
        # Verify correct URL with movie/ prefix
        call_url = mock_get.call_args[0][0]
        assert "/shows/movie/550" in call_url

    @patch("streaming.streaming_api_client.requests.get")
    def test_tv_found_on_multiple(self, mock_get):
        """TV series found on multiple providers."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "disney", "name": "Disney+"}, "type": "subscription"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None

        result = get_streaming_providers("test-key", 1396, "tv", country="cl")
        assert len(result) == 2
        # Verify correct URL with tv/ prefix
        call_url = mock_get.call_args[0][0]
        assert "/shows/tv/1396" in call_url

    @patch("streaming.streaming_api_client.requests.get")
    def test_not_found_returns_empty(self, mock_get):
        """404 returns empty list (fail-open)."""
        mock_get.return_value.status_code = 404
        result = get_streaming_providers("test-key", 99999, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_rate_limit_returns_empty(self, mock_get):
        """429 returns empty list (fail-open)."""
        mock_get.return_value.status_code = 429
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_no_streaming_in_country_returns_empty(self, mock_get):
        """Item exists but no streaming options in requested country."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {"us": [{"service": {"id": "netflix"}, "type": "subscription"}]}
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_filters_subscription_only(self, mock_get):
        """Only returns subscription (flatrate) providers, not rent/buy."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "apple", "name": "Apple TV"}, "type": "buy"},
                    {"service": {"id": "prime", "name": "Amazon Prime"}, "type": "rent"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert len(result) == 1
        assert result[0]["service_id"] == "netflix"

    @patch("streaming.streaming_api_client.requests.get")
    def test_network_error_returns_empty(self, mock_get):
        """Network error returns empty list (fail-open)."""
        mock_get.side_effect = requests.RequestException("timeout")
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_streaming_api_client.py -x -q`
Expected: FAIL — `get_streaming_providers` not defined

**Step 3: Implement `get_streaming_providers()`**

Add to `streaming_api_client.py` after `get_season_availability()`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_streaming_api_client.py -x -q`
Expected: PASS

**Step 5: Commit**

```bash
git add automation/scripts/streaming/streaming_api_client.py automation/scripts/streaming/tests/test_streaming_api_client.py
git commit -m "feat(streaming): add get_streaming_providers() for single-item MoTN lookup"
```

---

### Task 2: Add `notify_import_streaming()` to `discord.py`

Brief Discord embed for import-time streaming detection.

**Files:**
- Modify: `automation/scripts/streaming/discord.py`
- Test: `automation/scripts/streaming/tests/test_discord.py`

**Step 1: Write the failing test**

```python
class TestNotifyImportStreaming:
    @patch("streaming.discord.requests.post")
    def test_sends_embed(self, mock_post):
        """Sends a rich embed with title, providers, and media type."""
        mock_post.return_value.status_code = 204
        notify_import_streaming(
            "https://hook.example.com",
            title="Fight Club",
            year=1999,
            media_type="movie",
            providers=["Netflix", "Disney+"],
        )
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]
        assert "Fight Club" in embed["title"]
        assert embed["color"] == 3447003  # BLUE
        assert any("Netflix" in f["value"] for f in embed["fields"])

    @patch("streaming.discord.requests.post")
    def test_skips_empty_webhook(self, mock_post):
        """No-op when webhook URL is empty."""
        notify_import_streaming("", title="X", year=2020, media_type="movie", providers=["Netflix"])
        mock_post.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_discord.py::TestNotifyImportStreaming -x -q`
Expected: FAIL

**Step 3: Implement `notify_import_streaming()`**

Add to `discord.py`:

```python
def notify_import_streaming(webhook_url, title, year, media_type, providers):
    """Send a brief Discord notification when an imported item is on streaming."""
    if not webhook_url:
        return
    kind = "Movie" if media_type == "movie" else "Series"
    provider_list = ", ".join(providers)
    send_embed(
        webhook_url,
        title=f"📡 Streaming Detected — Import",
        description=f"**{title}** ({year}) is available on streaming",
        color=BLUE,
        fields=[
            {"name": "Title", "value": f"`{title} ({year})`", "inline": True},
            {"name": "Type", "value": kind, "inline": True},
            {"name": "Providers", "value": provider_list, "inline": True},
        ],
        footer=f"Event: Import · check-import",
    )
```

**Step 4: Run test to verify it passes**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_discord.py::TestNotifyImportStreaming -x -q`
Expected: PASS

**Step 5: Commit**

```bash
git add automation/scripts/streaming/discord.py automation/scripts/streaming/tests/test_discord.py
git commit -m "feat(streaming): add notify_import_streaming() discord embed"
```

---

### Task 3: Add `check-import` subcommand to `streaming_checker.py`

The main subcommand. Receives file path, media type, arr_id. Fetches item from arr, checks streaming via MoTN (fallback TMDB), tags + upserts + notifies.

**Files:**
- Modify: `automation/scripts/streaming/streaming_checker.py`
- Test: `automation/scripts/streaming/tests/test_cli.py`

**Step 1: Write the failing tests**

Add a new `TestCheckImport` class to `test_cli.py`:

```python
class TestCheckImport:
    """Tests for the check-import subcommand."""

    MOCK_RADARR_ITEM = {
        "id": 1, "tmdbId": 550, "title": "Fight Club", "year": 1999,
        "path": "/media/movies/Fight Club (1999)",
        "movieFile": {"size": 5_000_000_000},
        "tags": [],
    }

    MOCK_SONARR_ITEM = {
        "id": 10, "tvdbId": 81189, "title": "Breaking Bad", "year": 2008,
        "path": "/media/tv/Breaking Bad",
        "statistics": {"sizeOnDisk": 80_000_000_000},
        "tags": [],
    }

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_movie_on_streaming_tags_and_upserts(
        self, mock_get_item, mock_motn, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path
    ):
        """Movie found on streaming gets tagged + upserted into DB."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_motn.return_value = [
            {"service_id": "netflix", "service_name": "Netflix"},
        ]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        assert "Netflix" in result.output
        mock_add_tag.assert_called_once()
        mock_notify.assert_called_once()

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_movie_not_on_streaming_no_tag(
        self, mock_get_item, mock_tmdb, mock_motn, mock_add_tag,
        runner, env_config, tmp_path
    ):
        """Movie not on any streaming — no tag added."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = []

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_add_tag.assert_not_called()

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_tmdb_fallback_when_motn_empty(
        self, mock_get_item, mock_tmdb, mock_motn, mock_add_tag,
        runner, env_config, tmp_path
    ):
        """Falls back to TMDB when MoTN returns empty."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = [{"provider_id": 8, "provider_name": "Netflix"}]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_tmdb.assert_called_once()
        mock_add_tag.assert_called_once()

    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_already_tagged_skips_api(
        self, mock_get_item, mock_motn, runner, env_config, tmp_path
    ):
        """Item already tagged streaming-available skips all API calls."""
        db = _make_db(tmp_path)
        item = dict(self.MOCK_RADARR_ITEM)
        item["tags"] = [1]  # tag_id 1 = streaming-available
        mock_get_item.return_value = item

        with patch("streaming.streaming_checker.get_tag_id", return_value=1):
            result = runner.invoke(cli, [
                "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
                "--media-type", "movie", "--arr-id", "1", "--db-path", db,
            ])
        assert result.exit_code == 0, result.output
        mock_motn.assert_not_called()
        assert "already tagged" in result.output.lower()

    @patch("streaming.streaming_checker.get_item", return_value=None)
    def test_item_not_found_exits_gracefully(
        self, mock_get_item, runner, env_config, tmp_path
    ):
        """arr_id not found in Sonarr/Radarr — exits 0 with warning."""
        db = _make_db(tmp_path)
        result = runner.invoke(cli, [
            "check-import", "--file", "/test/file.mkv",
            "--media-type", "movie", "--arr-id", "999", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        assert "not found" in result.output.lower()

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_series_on_streaming(
        self, mock_get_item, mock_motn, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path
    ):
        """TV series found on streaming gets tagged."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_SONARR_ITEM
        mock_motn.return_value = [
            {"service_id": "disney", "service_name": "Disney+"},
        ]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/tv/Breaking Bad/Season 1/ep.mkv",
            "--media-type", "series", "--arr-id", "10", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_add_tag.assert_called_once()

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_no_rapidapi_key_skips_motn(
        self, mock_get_item, mock_tmdb, mock_motn, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path, monkeypatch
    ):
        """Without RAPIDAPI_KEY, skips MoTN and goes straight to TMDB."""
        db = _make_db(tmp_path)
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = [{"provider_id": 8, "provider_name": "Netflix"}]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_motn.assert_not_called()
        mock_tmdb.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_cli.py::TestCheckImport -x -q`
Expected: FAIL — `check-import` not a recognized command

**Step 3: Implement `check_import_cmd`**

Add to `streaming_checker.py` imports:

```python
from streaming.streaming_api_client import get_season_availability, get_streaming_providers
from streaming.tmdb_client import batch_check, check_streaming
from streaming.discord import (format_size, notify_deletion, notify_import_streaming,
    notify_scan_results, notify_stale_cleanup, notify_stale_flag)
```

Add the subcommand (after `check_audio` and before `report` is a good spot, but order doesn't matter for Click):

```python
@cli.command("check-import")
@click.option("--file", "file_path", required=True, help="Imported file path")
@click.option("--media-type", required=True, type=click.Choice(["movie", "series"]),
              help="movie or series")
@click.option("--arr-id", required=True, type=int, help="Radarr movie ID or Sonarr series ID")
@click.option("--db-path", default=None, help="Override DB path")
@click.option("--verbose", is_flag=True)
def check_import_cmd(file_path, media_type, arr_id, db_path, verbose):
    """Check if an imported item is on streaming and tag it."""
    _setup_logging(verbose)
    cfg = load_config(db_path=db_path)
    db = cfg.db_path
    init_db(db)

    # Determine arr type and fetch item
    if media_type == "movie":
        app = "movie"
        base_url = cfg.radarr_url
        api_key = cfg.radarr_key
    else:
        app = "series"
        base_url = cfg.sonarr_url
        api_key = cfg.sonarr_key

    item = get_item(base_url, api_key, app, arr_id)
    if not item:
        click.echo(f"Item not found: {app}/{arr_id}")
        return

    # Extract metadata
    tmdb_id = item.get("tmdbId", 0)
    title = item.get("title", "Unknown")
    year = item.get("year", 0)
    path = item.get("path", "")
    if media_type == "movie":
        size_bytes = item.get("movieFile", {}).get("size", 0) if item.get("movieFile") else 0
    else:
        size_bytes = item.get("statistics", {}).get("sizeOnDisk", 0)
    tmdb_media = "movie" if media_type == "movie" else "tv"

    if not tmdb_id:
        click.echo(f"No TMDB ID for {title} — skipping")
        return

    # Check if already tagged
    tag_id = get_tag_id(base_url, api_key, TAG_LABEL)
    if tag_id and tag_id in item.get("tags", []):
        click.echo(f"Already tagged streaming-available: {title}")
        return

    library = _detect_library(path, media_type)

    # Primary: Movie of the Night API
    providers = []
    if cfg.rapidapi_key:
        motn_results = get_streaming_providers(cfg.rapidapi_key, tmdb_id, tmdb_media, country=cfg.country)
        for p in motn_results:
            provider_id = SERVICE_TO_PROVIDER.get(p["service_id"])
            if provider_id and provider_id in cfg.provider_ids:
                providers.append({"provider_id": provider_id, "provider_name": p["service_name"]})

    # Fallback: TMDB Watch Providers
    if not providers and cfg.tmdb_api_key:
        providers = check_streaming(cfg.tmdb_api_key, tmdb_id, tmdb_media, cfg.provider_ids, cfg.country)

    if not providers:
        click.echo(f"Not on streaming: {title} ({year})")
        return

    # Tag the item
    sa_tag_id = ensure_tag(base_url, api_key, TAG_LABEL)
    add_tag_to_item(base_url, api_key, app, arr_id, sa_tag_id)

    # Upsert each provider match into DB
    for p in providers:
        upsert_streaming_item(
            db, tmdb_id=tmdb_id, media_type=tmdb_media,
            provider_id=p["provider_id"], provider_name=p["provider_name"],
            title=title, year=year, arr_id=arr_id,
            library=library, size_bytes=size_bytes, path=path,
        )

    provider_names = [p["provider_name"] for p in providers]
    click.echo(f"Tagged streaming-available: {title} ({year}) — {', '.join(provider_names)}")

    # Discord notification
    if cfg.discord_webhook_url:
        notify_import_streaming(
            cfg.discord_webhook_url,
            title=title, year=year, media_type=tmdb_media,
            providers=provider_names,
        )
```

Also add the import for `_detect_library` at the top:

```python
from streaming.arr_client import (add_tag_to_item, delete_item, ensure_tag,
    fetch_movies, fetch_series, get_item, get_tag_id, remove_tag_from_item,
    _detect_library)
```

Wait — `_detect_library` is private in `arr_client.py`. Instead, inline the library detection or make it public. Since it's already defined there and used by `fetch_movies`/`fetch_series`, just import it. The underscore prefix is convention, not enforcement. Add it to the import line.

Also need to add `SERVICE_TO_PROVIDER` — import it from `streaming_api_client` where we defined it in Task 1:

```python
from streaming.streaming_api_client import get_season_availability, get_streaming_providers, SERVICE_TO_PROVIDER
```

And import `check_streaming` from `tmdb_client`:

```python
from streaming.tmdb_client import batch_check, check_streaming
```

**Step 4: Run tests to verify they pass**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/test_cli.py::TestCheckImport -x -q`
Expected: PASS

**Step 5: Run ALL tests to verify no regressions**

Run: `cd /config/berenstuff && PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/ -x -q`
Expected: All tests pass

**Step 6: Commit**

```bash
git add automation/scripts/streaming/streaming_checker.py automation/scripts/streaming/tests/test_cli.py
git commit -m "feat(streaming): add check-import subcommand for import-time streaming detection"
```

---

### Task 4: Wire into the import hook

Add the backgrounded call to `arr_profile_extract_on_import.sh`.

**Files:**
- Modify: `scripts/arr_profile_extract_on_import.sh` (compat copy that Sonarr/Radarr actually calls)
- Sync: `automation/scripts/subtitles/arr_profile_extract_on_import.sh` (canonical, if it exists)

**Step 1: Add the background call**

After the codec enqueue-import block (line ~343) and before the Discord notification (line ~345), add:

```bash
  # Check streaming availability and tag (background, non-blocking)
  local streaming_media_type
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    streaming_media_type="series"
  else
    streaming_media_type="movie"
  fi
  (
    source /config/berenstuff/.env
    PYTHONPATH=/config/berenstuff/automation/scripts \
      python3 /config/berenstuff/automation/scripts/streaming/streaming_checker.py \
      check-import --file "$MEDIA_PATH" --media-type "$streaming_media_type" --arr-id "$MEDIA_ID"
  ) >> /config/berenstuff/automation/logs/streaming_check_import.log 2>&1 </dev/null &
  disown
```

**Step 2: Syntax-check the modified script**

Run: `bash -n scripts/arr_profile_extract_on_import.sh`
Expected: No errors

**Step 3: Sync compat copy (if canonical exists separately)**

Check if `automation/scripts/subtitles/arr_profile_extract_on_import.sh` is a separate file or the same. If separate, sync it. Then:

Run: `bash -n automation/scripts/subtitles/arr_profile_extract_on_import.sh` (if it exists)

**Step 4: Create the log file**

```bash
touch /config/berenstuff/automation/logs/streaming_check_import.log
```

**Step 5: Commit**

```bash
git add scripts/arr_profile_extract_on_import.sh
# Also add canonical if it's a separate file
git commit -m "feat(streaming): wire check-import into import hook as background task"
```

---

### Task 5: Manual end-to-end test

**Step 1: Test with a known streaming movie**

```bash
cd /config/berenstuff
source .env
PYTHONPATH=automation/scripts python3 automation/scripts/streaming/streaming_checker.py \
  check-import --file "/APPBOX_DATA/storage/media/movies/Fight Club (1999)/Fight Club.mkv" \
  --media-type movie --arr-id 1 --verbose
```

Verify:
- Output shows provider name(s) or "Not on streaming"
- Discord notification received (if on streaming)
- Check Radarr UI for `streaming-available` tag

**Step 2: Test with a known non-streaming item**

Pick an item not on any provider and run the same command. Verify no tag added, clean exit.

**Step 3: Test idempotency**

Run the same command twice for a streaming item. Second run should say "Already tagged".
