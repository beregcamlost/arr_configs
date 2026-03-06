# Streaming Check on Import

## Goal

Tag newly imported items with `streaming-available` at import time instead of waiting for the weekly scan. Upserts matches into the streaming DB for full integration.

## Architecture

New `check-import` subcommand in `streaming_checker.py`.

Called from `arr_profile_extract_on_import.sh` as a backgrounded task (same pattern as DeepL translation and codec enqueue-import).

## API Strategy

1. **Primary: Movie of the Night API** (RapidAPI) — already integrated in `streaming_api_client.py`
2. **Fallback: TMDB Watch Providers** — already used by weekly `scan` command

Skip MoTN if no `RAPIDAPI_KEY`; skip TMDB if no `TMDB_API_KEY`. Fail-open on all errors.

## Flow

1. Receive `--file`, `--media-type` (movie|series), `--arr-id` from import hook
2. Fetch item from Sonarr/Radarr via `get_item()` to get TMDB ID, title, year, size, path
3. Skip if already tagged `streaming-available`
4. Query MoTN API for streaming availability; on failure, fall back to TMDB
5. If on streaming: `ensure_tag("streaming-available")` + `add_tag_to_item()` + `upsert_streaming_item()` in DB
6. Discord notification (brief embed)

## Integration

- Reuses: `arr_client` (tag CRUD, get_item), `db` (upsert), `config`, `streaming_api_client`, `discord`
- Import hook chains it as background task with `</dev/null &` + `disown`
- Idempotent: skips if tag already present
- Keep-local items still get tagged (deletion logic handles filtering separately)

## Error Handling

- Fail-open: API errors log warning, don't block import
- Missing API keys: skip that source, try next
- Already tagged: skip entirely (no API calls)
