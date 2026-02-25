# Subtitle Quality Manager Design

**Date:** 2026-02-25
**Scope:** New script `subtitle_quality_manager.sh` (~400 lines)

## Problem

Emby can't deliver external .srt subtitle files to clients (returns 0 bytes). Embedded SubRip tracks work fine on all clients. External SRTs need to be muxed into MKV files as embedded tracks, but only after verifying quality.

## Commands

### `audit --path <dir>`

Scan media files, score all subtitle tracks (embedded + external), output a per-file report.

Quality metrics:

| Metric | Check | Rating |
|--------|-------|--------|
| Timing sync | First cue within 2min of video start, last cue within 5min of end | WARN if violated |
| Cue count | 400-900 cues/hour normal. <200 = BAD, >1200 = WARN | BAD/WARN/GOOD |
| Duration coverage | % of video covered by cues. <50% = BAD, <70% = WARN | BAD/WARN/GOOD |
| Encoding | Non-UTF8 or mojibake detection | BAD if detected |
| Watermarks | Known strings (GalaxyTV, YIFY, opensubtitles, addic7ed) | WARN if found |

Output: per-file table with each subtitle track, scores, and overall GOOD/WARN/BAD rating.

### `mux --path <dir>`

Embed external .srt files into MKV as additional subtitle tracks.

- Only muxes external SRTs rated GOOD by audit (or with `--force`)
- Runs audit internally first, skips BAD-rated files
- ffmpeg remux: `-c copy`, ~5-10s per file, no re-encoding
- Deletes external .srt files after successful mux
- Calls Bazarr `scan-disk` API to sync state
- `--dry-run` to preview
- Discord notification with summary

### `strip --path <dir> --track <language|index>`

Remove specific embedded subtitle tracks from MKV.

- Target by language code (e.g., `--track eng`) or stream index (e.g., `--track 2`)
- Useful for removing watermarked embedded subs
- ffmpeg remux: `-map 0 -map -0:s:N -c copy`
- `--dry-run` to preview

## Converter Conflict Safety

Before modifying any MKV file, check the codec manager's state DB:
- Query `conversion_plan` for `status='running'` matching the file path
- If the file is being converted: skip it, log warning
- State DB: `/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db`
- Also check for converter temp files in the same directory

## Bazarr Integration

After muxing external SRTs and deleting the files:
- Call `scan-disk` API (movie or series) so Bazarr detects the change
- Bazarr will see subtitles as embedded (not missing) after library re-scan

## CLI

```
subtitle_quality_manager.sh audit --path <dir> [--recursive] [--log-level info|debug]
subtitle_quality_manager.sh mux --path <dir> [--force] [--dry-run] [--recursive]
subtitle_quality_manager.sh strip --path <dir> --track <lang|index> [--dry-run]

Common flags:
  --bazarr-url URL     Bazarr base URL (default: http://127.0.0.1:6767/bazarr)
  --bazarr-db PATH     Bazarr SQLite DB path
  --state-dir DIR      Codec manager state dir (for conflict check)
```

## Files

- Create: `automation/scripts/subtitles/subtitle_quality_manager.sh`
- Create: `scripts/subtitle_quality_manager.sh` (compat copy)
- Sources: `lib_subtitle_common.sh` (for `resolve_media_info`, `bazarr_scan_disk_*`, `sql_escape`, `notify_discord_embed`)

## Test Case

Use Evil (TV) as the primary test case:
- Path: `/APPBOX_DATA/storage/media/tv/Evil`
- Has embedded SubRip (eng, watermarked "GalaxyTV") + external .en.srt + .es.srt
- Audit should flag the embedded "GalaxyTV" watermark
- Mux should embed the external .en.srt and .es.srt
- Strip should be able to remove the watermarked embedded track
