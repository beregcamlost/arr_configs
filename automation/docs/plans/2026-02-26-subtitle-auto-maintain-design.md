# Subtitle Auto-Maintain Design

**Date**: 2026-02-26
**Status**: Approved

## Problem

External SRT files don't play reliably in Emby (returns 0 bytes). Embedded SubRip tracks work fine. Currently, muxing external SRTs into MKVs and stripping bad embedded tracks requires manual runs of `subtitle_quality_manager.sh`. We need automated maintenance that handles both new imports and the existing backlog of ~2,153 MKVs with external SRTs.

## Design: Hybrid Auto-Maintain

New `auto-maintain` subcommand in `subtitle_quality_manager.sh` with two operating modes.

### Quick Mode (`--since N`)

- Runs frequently via cron (`*/10`)
- Scans only files modified in last N minutes
- Audits external SRTs only (fast: grep-based, ~1s/file)
- Auto-muxes GOOD-rated external SRTs into MKV
- Auto-strips BAD-rated embedded tracks **only if** a GOOD replacement in the same language was just muxed
- Completes in seconds when nothing changed

### Full Mode (no `--since`)

- Runs daily at 1 AM
- Scans all files with incremental state DB tracking
- Full audit including embedded tracks (ffmpeg extraction for scoring)
- State DB: `subtitle_quality_state.db`, table `file_audits`:
  - `file_path TEXT PRIMARY KEY`
  - `mtime INTEGER` — file modification time at last audit
  - `last_audit_ts INTEGER` — when we last audited
  - `embedded_json TEXT` — cached embedded track audit results
  - `external_json TEXT` — cached external SRT audit results
  - `action_taken TEXT` — last action (muxed/stripped/skipped/none)
- Only re-audits files where current `mtime > stored mtime` (incremental)
- First run: ~30h (full library). Subsequent: minutes (only new/changed files)

### Safety Mechanisms

1. **Converter conflict**: `is_file_being_converted()` per-file check against codec manager state DB. Skip if converter is active.
2. **Active playback**: Query Emby `GET /Sessions` — skip files currently being played.
3. **Flock**: Shared lock with subtitle dedupe (`/tmp/library_subtitle_dedupe.lock`) to prevent dedupe from deleting an SRT while mux is reading it.
4. **Library scan check**: If Emby "Scan media library" task is running, defer modifications.

### Emby Integration

After modifying an MKV (mux or strip):
1. Look up Emby item ID via `GET /Items?Path={file_path}` or `GET /Items?SearchTerm={name}`
2. Call `POST /Items/{Id}/Refresh` to rescan just that item's metadata
3. Emby immediately reflects new subtitle tracks — no stale metadata

### Auto-Strip Rules

Only auto-strip embedded tracks when ALL conditions are met:
- Track is rated BAD (not WARN)
- A GOOD-rated track in the **same language** exists (either just muxed or already embedded)
- File is not being converted and not being played

WARN embedded tracks are **never** auto-stripped — reported in Discord for manual review.

### Discord Notifications

Per-run summary (only when actions taken):
- X files muxed (Y subtitle tracks)
- X files stripped (Y tracks removed)
- X files skipped (converter busy / active playback)
- X files with WARN tracks (needs manual review)

### CLI Interface

```
subtitle_quality_manager.sh auto-maintain [options]

Options:
  --path-prefix DIR     Root media directory (default: /APPBOX_DATA/storage/media)
  --state-dir DIR       State DB directory (default: /APPBOX_DATA/storage/.subtitle-quality-state)
  --since N             Only scan files modified in last N minutes (quick mode)
  --dry-run             Preview changes without modifying files
  --bazarr-url URL      Bazarr base URL
  --bazarr-db PATH      Bazarr DB path
  --emby-url URL        Emby server URL (from EMBY_URL env)
  --emby-api-key KEY    Emby API key (from EMBY_API_KEY env)
  --log-level LEVEL     info or debug
```

### Cron Entries

```cron
# Subtitle quality: quick scan every 10 min (recently changed files)
*/10 * * * * /usr/bin/flock -n /tmp/library_subtitle_dedupe.lock /bin/bash -c 'source /config/berenstuff/.env && /bin/bash /config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --since 15 --path-prefix /APPBOX_DATA/storage/media' >> /config/berenstuff/automation/logs/subtitle_quality_manager.log 2>&1

# Subtitle quality: full incremental scan daily 1 AM
0 1 * * * /usr/bin/flock -n /tmp/library_subtitle_dedupe.lock /bin/bash -c 'source /config/berenstuff/.env && /bin/bash /config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media --state-dir /APPBOX_DATA/storage/.subtitle-quality-state' >> /config/berenstuff/automation/logs/subtitle_quality_manager.log 2>&1
```

### Interaction with Existing Jobs

| Job | Frequency | Conflict? | Mitigation |
|-----|-----------|-----------|------------|
| Subtitle dedupe (*/5) | Same SRT files | Yes | Shared flock |
| Subtitle recovery (*/15) | Creates SRTs | No | Auto-maintain picks them up next run |
| Codec converter (*/15) | Same MKV files | Yes | `is_file_being_converted()` per-file |
| Codec audit (3 AM) | Reads MKV metadata | Minor | Time separation (1 AM vs 3 AM) |
| Import hook | Creates external SRTs | No | Auto-maintain picks them up next run |
| Emby auto-detect | Reads new files | Minor | Per-item refresh after modification |

### Faster New Import Handling

The import hook (`arr_profile_extract_on_import.sh`) already fires on every Sonarr/Radarr import. After extracting embedded subs, it can trigger auto-maintain directly on the imported file's directory instead of waiting for the */10 cron. This reduces the delay from ~10 minutes to seconds.

```bash
# At end of import hook, after extraction completes:
# Fire-and-forget auto-maintain on just this file's directory
nohup flock -n /tmp/library_subtitle_dedupe.lock \
  bash subtitle_quality_manager.sh auto-maintain --since 5 --path "$MEDIA_DIR" \
  >> .../subtitle_quality_manager.log 2>&1 &
```

Bazarr still takes minutes to download SRTs, so the hook-triggered run handles embedded-only cleanup. The */10 cron catches Bazarr downloads shortly after.

### Shared Emby Refresh Helper

New function `emby_refresh_item()` added to `lib_subtitle_common.sh` so ALL scripts that modify media files can trigger Emby metadata refresh:

```bash
emby_refresh_item() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 0
  local item_id
  item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&Path=$(urlencode "$file_path")&Recursive=true&Limit=1" | jq -r '.Items[0].Id // empty')"
  [[ -n "$item_id" ]] && curl -fsS -X POST "${emby_url}/Items/${item_id}/Refresh?api_key=${emby_key}&Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default" >/dev/null 2>&1
}
```

Scripts that will call `emby_refresh_item()` after modifying files:
- `subtitle_quality_manager.sh` (mux/strip/auto-maintain)
- `library_codec_manager.sh` (after convert completes — swap with new file)
- `arr_profile_extract_on_import.sh` (after extracting embedded subs)
- `library_subtitle_dedupe.sh` (after removing duplicate SRTs)

### Emby Active Playback Check Helper

New function `is_file_being_played()` in `lib_subtitle_common.sh`:

```bash
is_file_being_played() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 1
  local playing
  playing="$(curl -fsS "${emby_url}/Sessions?api_key=${emby_key}" | jq --arg path "$file_path" '[.[] | select(.NowPlayingItem.Path == $path)] | length')"
  [[ "$playing" -gt 0 ]]
}
```

### End-to-End Flow

```
1. Sonarr/Radarr imports file → lands in media/
2. Emby auto-detects → indexes video, audio, existing embedded subs
3. Import hook fires:
   a. Extracts embedded subs to external SRTs
   b. Triggers Emby refresh for the file
   c. Fire-and-forget auto-maintain on file's directory
4. Bazarr detects → downloads best external SRTs (en, es, etc.)
5. Dedupe cleans duplicates (*/5 min) → Emby refresh per affected file
6. Auto-maintain quick scan (*/10 min):
   a. Finds new/changed SRT files
   b. Audits quality (cues, timing, watermarks, encoding)
   c. Checks no active playback and no converter running
   d. Muxes GOOD SRTs into MKV
   e. Strips BAD embedded if GOOD replacement exists
   f. Triggers Emby per-item refresh
   g. Triggers Bazarr scan-disk
   h. Discord summary
7. Auto-maintain full scan (daily 1 AM):
   a. Catches anything quick scan missed
   b. Full embedded track analysis with incremental state
   c. Same mux/strip/refresh logic
8. Codec converter (*/15 min):
   a. After converting a file → Emby refresh for that item
```
