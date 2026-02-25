# Subtitle System Improvements Design

**Date:** 2026-02-25
**Scope:** 7 improvements across all subtitle scripts (~3,000 lines)

## 1. Unified Webhook Hook

Replace `sonarr_profile_extract_on_import.sh` and `radarr_profile_extract_on_import.sh` with a single `arr_profile_extract_on_import.sh`.

- Auto-detects Sonarr vs Radarr by checking which env vars are set (`SONARR_EVENTTYPE` vs `RADARR_EVENTTYPE`)
- No `--type` flag needed — arr apps set distinctive env vars
- Delete old scripts from both `automation/scripts/subtitles/` and `scripts/`
- Create unified script in both locations
- ~90 lines instead of 310
- Discord notification adapts title/fields based on detected type
- User updates Sonarr/Radarr Connect paths after deployment

## 2. DRY Recovery State DB Functions

Replace 7 repetitive state functions with 2 generic ones:

- `state_get_col(media_type, media_id, lang, forced, hi, column_name)` — replaces `state_get_ts`, `state_get_bazarr_attempts`, `state_get_arr_attempts`, `state_get_regrab_attempts`
- `state_inc_col(media_type, media_id, lang, forced, hi, column_name)` — replaces `state_inc_bazarr_attempts`, `state_inc_arr_attempts`, `state_inc_regrab_attempts`
- Keep `state_set()` and `state_reset_for_regrab()` as-is (multi-column updates)
- Cuts ~80 lines of repetitive code

## 3. Hardened SQL Escaping

Move `sql_escape()` into `lib_subtitle_common.sh` (shared by dedupe and recovery):

- Strip null bytes (sqlite3 CLI chokes on these)
- Keep existing single-quote escaping (`'` -> `''`)
- Replace inline escaping in recovery script with calls to shared function

## 4. API Error Handling with Retries

New `curl_with_retry()` helper in `lib_subtitle_common.sh`:

- 3 attempts max, 5s/15s sleep between retries
- Retries on: HTTP 500/502/503/504, curl transport errors (exit 7=connection refused, 28=timeout)
- Does NOT retry on 4xx (client errors)
- Returns HTTP code from last attempt
- Logs each retry: `RETRY attempt=2/3 url=... http=502`
- Recovery script only increments attempt counters after all retries exhausted
- Used by: recovery, dedupe (Bazarr rescan), batch extractor, unified hook

## 5. Batch jsonish_to_json

Replace per-item Python invocations with bulk conversion:

1. Bulk-export all missing items from Bazarr DB into temp file
2. Single Python script converts all Python-repr strings to JSON, writes JSON-lines output
3. Main loop reads from JSON-lines file instead of per-item DB queries + Python calls
4. Reduces ~400 Python invocations to 2 (one for episodes, one for movies)
5. Mid-loop re-reads after API actions stay as-is (can't be pre-fetched)

## 6. Discord Notifications for Dedupe

Add Discord summary to `library_subtitle_dedupe.sh`:

- `notify_discord()` function matching other scripts' embed style
- Sends summary when `changed > 0`: scanned, processed, converted, stripped, removed, renamed, rescans
- Skips when no changes or `DISCORD_WEBHOOK_URL` not set

## 7. `--since` for Recovery

New `--since MINUTES` flag for `bazarr_subtitle_recovery.sh`:

- Only processes items where state DB `updated_at` is older than N minutes OR no state entry exists (new items)
- Default: 0 (process all, current behavior)
- Enables quick cron runs focused on new/stale items
- Example: `*/15` cron with `--since 30` skips items touched in last 30 minutes

## Files Modified

- `lib_subtitle_common.sh` — add `sql_escape()`, `curl_with_retry()`, `notify_discord_embed()`
- `bazarr_subtitle_recovery.sh` — improvements 2, 4, 5, 7
- `library_subtitle_dedupe.sh` — improvements 3, 4, 6
- `arr_profile_extract_on_import.sh` — new unified hook (improvement 1)
- `batch_extract_embedded.sh` — improvement 4
- Delete: `sonarr_profile_extract_on_import.sh`, `radarr_profile_extract_on_import.sh` (both locations)
