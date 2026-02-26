# Streaming Availability Checker — Design

**Date**: 2026-02-26
**Status**: Approved
**Language**: Python 3
**Location**: `automation/scripts/streaming/`

## Purpose

Cross-reference the local Radarr/Sonarr library against Netflix and Disney+ catalogs for Chile (CL) using the TMDB Watch Providers API. Identify media that is currently streaming so it can be removed to free disk space. Track when items leave streaming platforms so they can be re-added.

## Requirements

- Check all 671 Radarr movies + 149 Sonarr series against Netflix (provider 8) and Disney+ (provider 337) in Chile
- Tag matches in Radarr/Sonarr (`streaming-netflix`, `streaming-disney`)
- Items with `keep-local` tag (Sonarr id=4, Radarr id=5) are always excluded
- Explicit `confirm-delete` step required before any deletion
- Deletion = full remove from arr + delete files + Emby refresh
- Detect when items leave streaming (diff current vs previous scan)
- Discord notifications for new matches, left-streaming alerts, deletion confirmations
- Weekly cron + manual CLI

## Architecture

### Module Layout

```
automation/scripts/streaming/
├── __init__.py
├── streaming_checker.py    # CLI entry point (click)
├── tmdb_client.py          # TMDB Watch Providers API (async)
├── arr_client.py           # Radarr/Sonarr V3 API client
├── emby_client.py          # Emby per-item refresh + playback check
├── discord.py              # Webhook notifications (embeds)
├── db.py                   # SQLite state DB
└── config.py               # Env vars + CLI config
```

### CLI Subcommands

| Command | Description |
|---------|-------------|
| `scan` | Fetch library from arrs, check TMDB, update state DB, tag items, send Discord report |
| `report` | Display current state: streaming matches, left-streaming, pending deletions |
| `confirm-delete` | Delete tagged items from arrs, remove files, refresh Emby, notify Discord |
| `providers` | List available streaming providers for a given country |

### Data Flow

```
Radarr GET /api/v3/movie ──────┐
Sonarr GET /api/v3/series ─────┤
                                ├─► scan ─► TMDB /movie/{id}/watch/providers (async, 10 concurrent)
                                │            TMDB /tv/{id}/watch/providers
                                │                    │
                                │                    ▼
                                │           SQLite state DB (diff vs previous)
                                │                    │
                                │              ┌─────┴──────┐
                                │              ▼            ▼
                                │     Tag in arr      Discord report
                                │     (streaming-*)   (new / left / pending)
                                │
                                │   confirm-delete
                                │       │
                                ├─── DELETE /api/v3/movie/{id}?deleteFiles=true
                                ├─── DELETE /api/v3/series/{id}?deleteFiles=true
                                └─── Emby: refresh item (remove from library)
```

## State Database

Path: `/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db`

### Tables

```sql
CREATE TABLE streaming_status (
    tmdb_id       INTEGER NOT NULL,
    media_type    TEXT NOT NULL,          -- 'movie' or 'series'
    provider_id   INTEGER NOT NULL,
    provider_name TEXT NOT NULL,
    title         TEXT NOT NULL,
    year          INTEGER,
    arr_id        INTEGER,               -- Radarr/Sonarr internal ID
    library       TEXT,                   -- 'movies', 'tv', 'moviesanimated', 'tvanimated'
    size_bytes    INTEGER,
    first_seen    TEXT NOT NULL,          -- ISO 8601
    last_seen     TEXT NOT NULL,          -- ISO 8601
    left_at       TEXT,                   -- set when item disappears from streaming
    deleted_at    TEXT,                   -- set when confirm-delete runs
    PRIMARY KEY (tmdb_id, media_type, provider_id)
);

CREATE TABLE scan_history (
    scan_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    country          TEXT NOT NULL DEFAULT 'CL',
    movies_checked   INTEGER,
    series_checked   INTEGER,
    matches_found    INTEGER,
    newly_streaming  INTEGER,
    left_streaming   INTEGER,
    duration_seconds REAL
);
```

### "Left Streaming" Detection

On each scan:
1. Query TMDB for all library items
2. For each `(tmdb_id, media_type, provider_id)` in current results:
   - If exists in DB with `left_at IS NULL`: update `last_seen`
   - If not in DB: INSERT (newly streaming)
   - If exists with `left_at IS NOT NULL`: clear `left_at`, update `last_seen` (returned to streaming)
3. For rows in DB where `last_seen < current_scan AND left_at IS NULL AND deleted_at IS NULL`:
   - Set `left_at = now` (just left streaming)
   - Discord alert: "These items left Netflix/Disney+ CL"

## TMDB API Details

- **Endpoint**: `GET /3/movie/{tmdb_id}/watch/providers` and `GET /3/tv/{tmdb_id}/watch/providers`
- **Auth**: `?api_key={TMDB_API_KEY}` query param
- **Rate limit**: ~40 req/s, no daily cap
- **Batching**: No batch endpoint; use `asyncio`+`aiohttp` with semaphore(10) for ~800 calls in ~15s
- **Response parsing**: `.results.CL.flatrate[]` → filter `provider_id` in {8, 337}
- **Provider IDs**: Netflix=8, Disney+=337

## Tag-Based Exclusion

- `keep-local` tag: items with this tag are never flagged, tagged, or deleted
- Tag IDs: Sonarr=4, Radarr=5
- Currently applied to: Lioness (Sonarr id=24)
- The script reads tag IDs dynamically via `GET /api/v3/tag` (tag label lookup, not hardcoded IDs)

## Confirm-Delete Flow

1. `scan` tags items with `streaming-netflix` / `streaming-disney` in their respective arr
2. Discord embed lists all matches grouped by provider, with title, year, library, file size
3. User reviews and runs `confirm-delete`
4. For each item in DB where `deleted_at IS NULL` and tagged:
   - Skip if `keep-local` tag present
   - `DELETE /api/v3/movie/{arr_id}?deleteFiles=true` (Radarr)
   - `DELETE /api/v3/series/{arr_id}?deleteFiles=true&addImportListExclusion=false` (Sonarr)
   - Emby per-item refresh (to remove from Emby library)
   - Set `deleted_at` in DB
5. Discord confirmation summary with total freed space

## Configuration

From `.env` (add `TMDB_API_KEY`):
```bash
export TMDB_API_KEY=<to be registered>
export RADARR_KEY=<existing>
export SONARR_KEY=<existing>
export EMBY_API_KEY=<existing>
export DISCORD_WEBHOOK_URL=<existing>
```

CLI flags override env vars:
```
--country CL          # ISO 3166-1 alpha-2 (default: CL)
--providers netflix,disney  # comma-separated (default: netflix,disney)
--dry-run             # scan + report but don't tag or modify anything
--verbose             # debug logging
--db-path PATH        # override state DB location
```

## Cron Schedule

```
0 5 * * 0  source /config/berenstuff/.env && python3 /config/berenstuff/automation/scripts/streaming/streaming_checker.py scan >> /config/berenstuff/automation/logs/streaming_checker.log 2>&1
```

Weekly Sunday 5 AM UTC — after subtitle dedupe full scan (4 AM), before emby report (Tuesday).

## Dependencies

- Python 3 (system)
- `aiohttp` — async HTTP for TMDB batch calls
- `click` — CLI framework
- `sqlite3` — stdlib
- No other external deps needed; `requests` only if we skip async

## Future Possibilities

- Emby plugin / web UI for visual review
- Custom *arr integration
- Additional providers (HBO Max, Prime, etc.)
- Auto re-add when items leave streaming
