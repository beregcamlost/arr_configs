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

# Streaming checker tests (169 tests)
PYTHONPATH=automation/scripts python3 -m pytest automation/scripts/streaming/tests/ -x -q

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

Subcommands: `audit`, `plan`, `report`, `convert`, `resume`, `daily-status`, `enqueue-import`, `prune-backups`.

State is persisted in SQLite at `/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db` with tables: `media_files`, `conversion_plan`, `conversion_runs`, `audit_status`, `probe_streams`.

Conversion policy: H.264 CRF 19, AAC 192k stereo 48kHz. Skips UHD/4K/HDR. Safety workflow: temp output → codec+duration+subtitle verification → swap with original backed up to `/APPBOX_DATA/storage/.transcode-state-media/backups/`.

Discord notifications: `DISCORD_WEBHOOK_AUDIT_DONE` (audit complete with progress bar, conversion stats, rate/ETA) and `DISCORD_WEBHOOK_STATUS` (daily status). Helper functions: `comma_fmt()`, `progress_bar()` (uses `printf '\uNNNN'` — never `tr` for multi-byte UTF-8).

### Streaming checker (`automation/scripts/streaming/`)

Python CLI (`streaming_checker.py`) cross-referencing library against TMDB streaming providers (Netflix, Disney+). State DB at streaming state path.

Subcommands: `scan`, `report`, `confirm-delete`, `check-seasons`, `stale-flag`, `stale-delete`, `stale-cleanup`, `summary`, `providers`, `exclude`, `check-import`, `check-audio`.

**Three-tier cleanup (cron):**
- **Tier 1 (weekly Sunday 6 AM):** `check-seasons` + `confirm-delete --yes` — deletes all items available on streaming (no play-day filter). `check-seasons` auto-tags `keep-local` for series with seasons not on the provider. **Currently disabled.**
- **Tier 1.5 (weekly Sunday 5:30+6:30 AM):** `stale-flag --no-play-days 90` flags items unwatched 90+ days that are on streaming. `stale-delete --grace-days 15 --yes` deletes items flagged 15+ days ago that are still unwatched and still on streaming. Also adds `stale_candidate` skip reason to codec manager (skips conversion of flagged items). Safety: aborts if keep-local API check fails.
- **Tier 2 (monthly 1st 7 AM):** `stale-cleanup --yes --no-play-days 365 --min-size-gb 3.0` — scans entire library, auto-deletes >3 GB items not played in 1 year, Discord reports the rest. Excludes keep-local and active streaming matches.

All tiers exclude keep-local tagged items, dual-audio items (jpn+spa or eng+spa), manually excluded items, and verify against Emby active playback before deleting.

**False positive prevention:** `scan` cross-validates TMDB matches against RapidAPI (`_verify_with_rapidapi()`). Only providers confirmed by both sources are kept. Gated behind `RAPIDAPI_KEY` env var; `--skip-verify` flag bypasses when quota exhausted. Manual exclusions via `exclude` subcommand (`--add`/`--remove`/`--list`) stored in `streaming_exclusions` DB table — excluded items skipped before TMDB batch_check.

**Key internal helpers:**
- `_parse_dt(s)` — ISO8601 datetime parsing with Z-suffix handling
- `_get_keep_local_tag_ids(cfg)` — returns `(kl_radarr, kl_sonarr)` tag IDs, reuse instead of calling `get_tag_id` directly
- `_get_keep_local_set(cfg)` — returns `set((arr_id, media_type))`, fail-open (logs + continues)
- `_verify_with_rapidapi(cfg, tmdb_results, items_by_key)` — cross-check TMDB against RapidAPI, returns filtered results
- Test helpers in `tests/test_cli.py`: `_make_db()`, `_seed_fight_club()`, `_seed_toy_story()`, `_seed_both_movies(**overrides)`

**Dual-audio auto-protect rule:** Any item with dual audio tracks (Japanese+Spanish or English+Spanish) is automatically tagged `keep-local` via `check-audio` subcommand. These are hard to re-acquire and must never be auto-deleted.

### Trending auto-add (`automation/scripts/streaming/trending_add.py`)

Python CLI that fetches trending movies (30) and series (10) from Apple TV+, Paramount+, HBO Max via Movie of the Night API (RapidAPI) and adds new items to Radarr/Sonarr.

Subcommands: `sync`, `preview`, `cache-status`.

Features: auto-detects animated content (genre-based routing to `moviesanimated`/`tvanimated`), `trending-add` tag on all items, JSON file cache (7-day TTL at `/APPBOX_DATA/storage/.trending-cache/`), cross-service dedup, Discord notification. Client-side service filtering in `search_catalog()` via `_is_on_service()` — the MoTN API `services` param does NOT filter by actual availability, so results are over-fetched 3x and filtered locally.

Cron: weekly Sunday 8 AM (`0 8 * * 0`).

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
- **D.R.Y. — Don't Repeat Yourself:**
  - Never hardcode values that appear in more than one place — use constants or shared helpers
  - Shared logic belongs in `lib_subtitle_common.sh` (subtitles) or equivalent shared libs
  - When iterating over old code, fix any DRY violations you encounter (extract helpers, remove duplication)
  - Library path patterns (`/tv/`, `/movies/`, etc.) must use `is_tv_path()`/`is_movie_path()` helpers, never inline globs
  - Language code mappings use `expand_lang_codes()`, `lang_in_set()`, `lang_to_iso639_2()` from the shared lib
  - Codec manager skip-category helpers use bash namerefs: `_load_candidate_paths()`, `_check_path_in_set()`, `_resolve_match_dir()` — add new skip reasons by declaring a new array + calling shared helpers, not duplicating logic
  - Prefer `${filepath%/*}` over `$(dirname "$filepath")` in hot loops (pure bash, no subprocess)

## Discord Notification Standard

**ALL** Discord notifications **MUST** use rich embeds with fields. No plain-text or description-only embeds. Every notification needs: emoji title, fields (inline for metrics, non-inline for lists), footer, timestamp, and color coding.

Rich embed format:

- **Title:** Emoji + system name + context (e.g., `📺 Streaming Scan`, `📥 Subtitle Auto-Maintain (quick)`)
- **Description:** Summary stats with emoji prefixes and bold values (e.g., `📥 Muxed: **3** file(s)`)
- **Fields:** Use inline fields for key metrics (3 per row). Use non-inline fields for item lists.
- **Footer:** System identifier or context info (e.g., `Duration: 15.3s`, `Event: Download`)
- **Timestamp:** Always include UTC ISO8601 timestamp
- **Colors:** `3066993` (green=success), `15105570` (orange=partial/warning), `15844367` (yellow=info/skip), `15158332` (red=error/deletion), `3447003` (blue=neutral)
- **Item lists:** Backtick-wrapped titles (`` `Title` ``), capped at 10–20 items with "…and N more"
- **Curl options (bash):** `-sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors`
- **Error handling:** Always non-fatal (`|| log "WARN: Discord failed"` or `|| true`)
- Reference: codec manager audit notification (best example) in `library_codec_manager.sh`

## Commit Style

Conventional commits: `feat(subtitles): ...`, `fix(transcode): ...`

**NEVER** include `Co-Authored-By: Claude`, `Co-Authored-By: Claude Code`, or any Claude/Anthropic references in commit messages. No AI attribution in commits — ever.

## Key External Paths

- Bazarr runtime: `/opt/bazarr/data/` (config, DB, backups — not under `/config`)
- Bazarr DB: `/opt/bazarr/data/db/bazarr.db`
- Media root: `/APPBOX_DATA/storage/media/`
- Transcode state: `/APPBOX_DATA/storage/.transcode-state-media/`
- Canonical docs: `automation/docs/` (TASK_CONTEXT.md, runbooks)

## Documentation

- `automation/docs/TASK_CONTEXT.md` — operational context and continuity across sessions
- `automation/docs/enesfr-profile-extraction-runbook.md` — subtitle profile extraction guide
- `automation/docs/transcode-manager-runbook.md` — codec manager commands and safety behavior
- `automation/docs/bazarr-config-docs.md` — Bazarr instance settings and integration reference
