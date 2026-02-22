# ENESFR Profile Extraction Runbook

Date: 2026-02-13

## Goal
Use profile `enesfr` (profileId `5`) to work with French subtitles (forced + full), then allow EN/ES workflow, without making `enesfr` the global default profile.

## What was changed

### 1) Bazarr config (kept global unknown-language fallback empty)
File: `/opt/bazarr/data/config/config.yaml`

- `embeddedsubtitles.fallback_lang: ''`
- `general.default_und_embedded_subtitles_lang: ''`
- `general.use_embedded_subs: true`
- `general.ignore_pgs_subs: false`

### 2) Language profile `enesfr` (profileId 5)
DB: `/opt/bazarr/data/db/bazarr.db`

Profile items are:
- French forced
- French full
- English full
- Spanish full

### 3) Per-profile extraction script
Script: `/config/berenstuff/scripts/extract_fr_embedded_for_profile5.sh`

What it does:
- Reads Bazarr DB and selects monitored episodes from monitored series with `profileId=5`
- Scans embedded subtitle streams with `ffprobe`
- Uses title/language heuristics to detect French streams
- Extracts to external files beside each video:
  - `*.fr.forced.srt`
  - `*.fr.srt`

## Run it again anytime

```bash
/config/berenstuff/scripts/extract_fr_embedded_for_profile5.sh
```

If filesystem permissions block media writes, run with elevated permissions.

## Verify generated files (example)

```bash
find "/APPBOX_DATA/storage/media/tvanimated/Cross Ange - Rondo of Angel and Dragon/Season 1" -maxdepth 1 -type f -name '*.fr.srt' | wc -l
find "/APPBOX_DATA/storage/media/tvanimated/Cross Ange - Rondo of Angel and Dragon/Season 1" -maxdepth 1 -type f -name '*.fr.forced.srt' | wc -l
```

## Bazarr UI steps after extraction
1. Open the series in Bazarr.
2. Run `Scan Disk` / `Update subtitles index`.
3. Run `Search missing subtitles`.

## Notes
- Bazarr does not natively apply embedded-language fallback by profile.
- This script is the workaround to get per-profile behavior.

---

## Chinese profile workflow (same logic)

Script:
- `/config/berenstuff/scripts/extract_zh_embedded_for_chinese_profiles.sh`

What it targets:
- Any monitored series assigned to profiles containing Chinese languages (`zh` or `zt`), currently profile IDs `3,4`.

What it writes:
- Traditional Chinese:
  - `*.zt.forced.srt`
  - `*.zt.srt`
- Simplified/Generic Chinese:
  - `*.zh.forced.srt`
  - `*.zh.srt`

Run:

```bash
/config/berenstuff/scripts/extract_zh_embedded_for_chinese_profiles.sh
```

Current status at creation time:
- No monitored episodes were assigned to Chinese profiles, so it reported nothing to process.

---

## Sonarr Auto-Trigger (Option 2)

Configured in Sonarr Connect:
- Name: `Profile Subtitle Extractor`
- Type: `Custom Script`
- Script path: `/config/berenstuff/scripts/sonarr_profile_extract_on_import.sh`
- Enabled events: Download, Upgrade, Import Complete, Rename, Series Add

How it decides language extraction:
- Reads series `profileId` from Bazarr DB using Sonarr series ID.
- If profile contains `fr`, extracts FR (`.fr.forced.srt` and `.fr.srt`).
- If profile contains `zh` or `zt`, extracts Chinese (`.zh/.zt` forced/full files).

Important behavior:
- Runs automatically on Sonarr events with an episode file path (imports/renames/upgrades).
- `Series Add` event has no episode file yet, so script safely skips until files are imported.
- Bazarr `Scan Disk` does not execute this script.

Log file:
- `/config/berenstuff/scripts/sonarr_profile_extract_on_import.log`

---

## Radarr Auto-Trigger (same logic + Discord)

Configured in Radarr Connect:
- Name: `Profile Subtitle Extractor`
- Type: `Custom Script`
- Script path: `/config/berenstuff/scripts/radarr_profile_extract_on_import.sh`
- Enabled events: Download, Upgrade, Rename

How it decides language extraction:
- Reads movie `profileId` from Bazarr DB using Radarr movie ID.
- If profile contains `fr`, extracts FR (`.fr.forced.srt` and `.fr.srt`).
- If profile contains `zh` or `zt`, extracts Chinese (`.zh/.zt` forced/full files).

Discord notifications:
- Same webhook as Sonarr script.
- Sends `SUCCESS`, `INFO`, or `SKIP` message with event/movie/profile/file details.

Log file:
- `/config/berenstuff/scripts/radarr_profile_extract_on_import.log`
