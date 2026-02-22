# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Operations-focused Bash workspace for Bazarr/Sonarr/Radarr media automation. Two main systems:
1. **Subtitle extraction pipeline** — event-driven hooks that extract embedded subtitles based on Bazarr language profiles
2. **Codec manager** — SQLite-backed audit/plan/convert workflow targeting x264+AAC normalization

## Validation Commands

There is no build step. All scripts are Bash.

```bash
# Syntax-check any script (minimum gate for all changes)
bash -n automation/scripts/transcode/library_codec_manager.sh
bash -n automation/scripts/subtitles/sonarr_profile_extract_on_import.sh

# Codec manager validation sequence (run in order)
/config/berenstuff/scripts/library_codec_manager.sh audit --log-level info
/config/berenstuff/scripts/library_codec_manager.sh plan --log-level info
/config/berenstuff/scripts/library_codec_manager.sh convert --dry-run --batch-size 20 --log-level info

# Check logs after runs
# Codec manager: /APPBOX_DATA/storage/.transcode-state/manager.log
# Sonarr hook:   /config/berenstuff/automation/logs/sonarr_profile_extract_on_import.log
# Radarr hook:   /config/berenstuff/automation/logs/radarr_profile_extract_on_import.log
```

## Architecture

### Dual-path layout

- **`automation/scripts/`** — canonical source for all scripts
- **`scripts/`** — compatibility copies referenced by Sonarr/Radarr Connect hooks; paths must stay stable unless explicitly migrated

### Subtitle hooks (`automation/scripts/subtitles/`)

Sonarr/Radarr import events trigger `sonarr_profile_extract_on_import.sh` / `radarr_profile_extract_on_import.sh`. These query the Bazarr SQLite DB (`/opt/bazarr/data/db/bazarr.db`) to resolve the series/movie language profile, then use ffprobe+ffmpeg to extract matching embedded subtitle streams to external `.srt` files. Results are posted to Discord via webhook.

Key profiles: `enesfr` (id 5, languages: fr/en/es), profiles 3-4 (Chinese zh/zt).

### Codec manager (`automation/scripts/transcode/library_codec_manager.sh`)

Subcommands: `audit`, `plan`, `report`, `convert`, `resume`, `daily-status`, `prune-backups`.

State is persisted in SQLite at `/APPBOX_DATA/storage/.transcode-state/library_codec_state.db` with tables: `media_files`, `conversion_plan`, `conversion_runs`.

Conversion policy: H.264 CRF 19, AAC 192k stereo 48kHz. Skips UHD/4K/HDR. Safety workflow: temp output → codec+duration verification → swap with original backed up to `/APPBOX_DATA/storage/.transcode-state/backups/`.

### Supporting scripts

- `library_subtitle_dedupe.sh` — removes duplicate/low-quality external subtitles
- `bazarr_subtitle_recovery.sh` — retries missing subtitle downloads with translation fallback
- `arr_cleanup_importblocked.sh` — clears stale "already imported" queue entries from Sonarr/Radarr (requires env vars: `RADARR_KEY`, `SONARR_KEY`, `TRANSMISSION_URL`, `TRANSMISSION_USER`, `TRANSMISSION_PASS`)
- `emby_last_played_report.sh` — CSV report of last-watched timestamps across Emby users

## Agent Model Usage

Use **Sonnet** (`model: "sonnet"`) for subagents by default — file reads, searches, simple edits, compat syncs, syntax checks. Only use **Opus** for tasks requiring complex reasoning: multi-file refactors, architectural decisions, debugging subtle issues, or writing new non-trivial logic.

Use **Haiku** (`model: "haiku"`) for trivial lookups, grep-and-report, or single-file reads.

## Compat Sync Rule

After editing any canonical script in `automation/scripts/`, always sync the compat copy in `scripts/` and run `bash -n` on it. Compat copies are what Sonarr/Radarr Connect hooks actually execute.

## Coding Conventions

- Shebang: `#!/usr/bin/env bash` with `set -euo pipefail`
- `snake_case` for functions/variables, uppercase for exported/config constants
- Small single-purpose functions (e.g., `log`, `cleanup_app`, `extract_stream`)
- New scripts go under `automation/scripts/`; only mirror to `scripts/` when compatibility requires it

## Commit Style

Conventional commits: `feat(subtitles): ...`, `fix(transcode): ...`

## Key External Paths

- Bazarr runtime: `/opt/bazarr/data/` (config, DB, backups — not under `/config`)
- Bazarr DB: `/opt/bazarr/data/db/bazarr.db`
- Media root: `/APPBOX_DATA/storage/media/`
- Transcode state: `/APPBOX_DATA/storage/.transcode-state/`
- Canonical docs: `automation/docs/` (TASK_CONTEXT.md, runbooks)

## Documentation

- `automation/docs/TASK_CONTEXT.md` — operational context and continuity across sessions
- `automation/docs/enesfr-profile-extraction-runbook.md` — subtitle profile extraction guide
- `automation/docs/transcode-manager-runbook.md` — codec manager commands and safety behavior
- `automation/docs/bazarr-config-docs.md` — Bazarr instance settings and integration reference
