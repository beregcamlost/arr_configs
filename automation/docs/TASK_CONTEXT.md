# Task Context: Bazarr Subtitle + Codec Automation

Last updated: 2026-02-15

## Purpose
Centralized operational context for:
1. Subtitle extraction automation by Bazarr language profile (Sonarr + Radarr hooks).
2. Library codec audit/transcode manager (x264 + AAC target workflow).

This file is intended to preserve continuity across future sessions.

## Canonical Automation Root
- `/config/berenstuff/automation`

## Folder Layout
- `scripts/subtitles/`:
  - `extract_fr_embedded_for_profile5.sh`
  - `extract_zh_embedded_for_chinese_profiles.sh`
  - `sonarr_profile_extract_on_import.sh`
  - `radarr_profile_extract_on_import.sh`
- `scripts/transcode/`:
  - `library_codec_manager.sh`
- `logs/`:
  - `sonarr_profile_extract_on_import.log`
  - `radarr_profile_extract_on_import.log`
- `docs/`:
  - `TASK_CONTEXT.md` (this file)

## Compatibility Layer (important)
Compatibility paths under `/config/berenstuff/scripts/` are kept for Sonarr/Radarr Connect.
Canonical script source remains under `/config/berenstuff/automation/scripts/`.

## Subtitle Automation Summary

### Bazarr profile behavior
- `enesfr` profile (id `5`) contains: `fr forced`, `fr`, `en`, `es`.
- Embedded subtitle global unknown-language fallback in Bazarr is set empty.
- Embedded subtitle extraction in Bazarr is enabled.

### Sonarr hook
- Script path in Sonarr Connect: `/config/berenstuff/scripts/sonarr_profile_extract_on_import.sh`
- Event-driven extraction on import-like events.
- Reads profile language items from Bazarr DB and attempts extraction for each profile language.
- Writes external subtitles next to media (e.g. `.fr.srt`, `.fr.forced.srt`, `.zh.srt`, `.zt.srt`, etc.).
- Log file: `/config/berenstuff/scripts/sonarr_profile_extract_on_import.log`

### Radarr hook
- Script path in Radarr Connect: `/config/berenstuff/scripts/radarr_profile_extract_on_import.sh`
- Same logic as Sonarr, but for movie events.
- Log file: `/config/berenstuff/scripts/radarr_profile_extract_on_import.log`

### Discord for subtitle hooks
Both Sonarr/Radarr hook scripts notify to webhook:
- `https://discord.com/api/webhooks/1471677059360227478/REDACTED_WEBHOOK_TOKEN`

## Codec Manager Summary

### Script
- Canonical: `/config/berenstuff/automation/scripts/transcode/library_codec_manager.sh`
- Compat path: `/config/berenstuff/scripts/library_codec_manager.sh`

### State
- `/APPBOX_DATA/storage/.transcode-state/library_codec_state.db`
- `/APPBOX_DATA/storage/.transcode-state/manager.log`
- `/APPBOX_DATA/storage/.transcode-state/latest_report.md`
- Backups: `/APPBOX_DATA/storage/.transcode-state/backups`

### Commands
- `audit`
- `plan`
- `report`
- `convert`
- `resume`
- `prune-backups`

### Conversion policy (current)
- Target video: x264 (H.264), `yuv420p`, CRF 19
- Target audio: AAC stereo, 48kHz
- Keep source container
- Skip UHD/4K and HDR in conversion planning
- Single-file sequential workflow
- Copy -> verify -> swap with backup retention window

### Discord for audit completion
`library_codec_manager.sh` sends audit completion summary to:
- `https://discord.com/api/webhooks/1471699428581576747/REDACTED_WEBHOOK_TOKEN`

## Existing documentation
- `/config/berenstuff/automation/docs/enesfr-profile-extraction-runbook.md`
- `/config/berenstuff/automation/docs/transcode-manager-runbook.md`
- `/config/berenstuff/automation/docs/bazarr-config-docs.md`

## Operational notes
- If host powers off, background jobs stop.
- Bazarr runtime data/config/DB lives under `/opt/bazarr/data` (not under `/config`).
- Hooks rely on Bazarr DB at `/opt/bazarr/data/db/bazarr.db`.
- Keep compatibility paths unless Sonarr/Radarr Connect paths are explicitly migrated.

---

## Documentation Policy
- Canonical docs live in `/config/berenstuff/automation/docs`
- Guide for Claude Code: `/config/berenstuff/CLAUDE.md`
