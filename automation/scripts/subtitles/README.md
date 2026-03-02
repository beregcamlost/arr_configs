# 🎬 Subtitle System

> A full subtitle lifecycle pipeline for MKV/MP4/M4V media — from import-time extraction through ongoing quality maintenance, deduplication, and recovery. Bazarr-profile-aware, codec-smart, streaming-candidate-safe.

---

## 🗂️ Files

| File | Role |
|------|------|
| `subtitle_quality_manager.sh` | ⭐ Main tool — `audit` / `mux` / `strip` / `auto-maintain` subcommands |
| `lib_subtitle_common.sh` | 📚 Shared library sourced by all scripts — language helpers, codec helpers, path classifiers, Bazarr/Emby/Discord utilities |
| `arr_profile_extract_on_import.sh` | 📥 Sonarr + Radarr import hook — unified entry point for the full import pipeline |
| `library_subtitle_dedupe.sh` | 🗑️ Quality-deduplicate external subtitle files across the library |
| `bazarr_subtitle_recovery.sh` | 🔁 Escalating recovery — Bazarr retry → auto-translate → arr search → re-grab |
| `batch_extract_embedded.sh` | ⚙️ One-shot batch extraction for backfilling the library |

---

## ✨ Features

### 🔄 4-Phase Import Pipeline (per file)

```
Phase 0 ── Extract + Strip
           Non-profile embedded TEXT tracks → external .srt (DeepL translation source)
           Bitmap tracks (PGS/DVDSUB) → strip-only (not extractable)

Phase 1 ── Collision Detection
           Before muxing external SRTs, remove any embedded tracks that would conflict

Phase 1.5 ─ Profile Cleanup
           Once all profile languages are satisfied → delete non-profile external SRTs
           (they were only needed as DeepL translation sources)

Phase 2 ── Deduplication
           GOOD+GOOD duplicate embedded tracks per language → keep highest quality scorer
```

### 🧠 Quality Scoring (`subtitle_quality_score()`)
- Scores every subtitle track 0–100 based on source, codec, and size
- Watermark detection (`galaxytv`, `yify`, `yts`, `opensubtitles`, etc.) → instant FAIL
- Prefers external text SRTs over embedded bitmaps

### 🌐 Language Intelligence (from `lib_subtitle_common.sh`)
- `expand_lang_codes()` — accepts mixed 2-letter and 3-letter codes
- `normalize_track_lang()` — pure-bash 3→2 letter ISO normalization (safe in hot loops)
- `detect_srt_language()` — detects `und` SRTs using `langdetect` (offline) → DeepL API fallback; renames `und.srt` → `en.srt`
- `resolve_bazarr_profile_langs()` — reads actual Bazarr profile from SQLite
- `lang_to_iso639_2()`, `lang_in_set()`, `get_audio_languages()`

### 🛡️ Safety Features
- Streaming candidate skip — files flagged by the streaming checker are never muxed or stripped
- `</dev/null` on all ffmpeg/sqlite3 calls inside pipeline loops
- Orphaned temp file cleanup at scan start (`.striptmp.*`, `.collisiontmp.*`, etc.)
- `--since N` filter checks both MKV mtime AND SRT mtime (OR logic) so fresh imports without SRTs are caught by quick scans
- Emby refresh triggered per modified file (deduplicated per series/movie dir)

### 📢 Discord Notifications
- Rich embeds with per-file breakdown, emoji status indicators, muxed/stripped/skipped counters
- DeepL deferral tracking (files waiting on `.deepl` markers)
- Colors: green (success), orange (partial/warning), yellow (skip), blue (neutral)

---

## 🔧 CLI Usage

### `subtitle_quality_manager.sh` — Main Tool

```bash
SQM=/config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh

# Audit: score all subtitle tracks in a directory
$SQM audit --path "/APPBOX_DATA/storage/media/tv/Severance" --recursive

# Mux: embed good external SRTs into MKVs
$SQM mux --path "/APPBOX_DATA/storage/media/tv/Severance/Season 1" --dry-run
$SQM mux --path "/APPBOX_DATA/storage/media/tv/Severance/Season 1"

# Strip: remove specific language tracks
$SQM strip --path "/APPBOX_DATA/storage/media/tv/Show" \
     --track eng --recursive --dry-run

# Strip: keep only certain languages, remove everything else
$SQM strip --path "/APPBOX_DATA/storage/media/tv/Show" \
     --keep-only en,fr,es --recursive

# Auto-maintain: quick scan (every 10 min via cron)
$SQM auto-maintain --path-prefix /APPBOX_DATA/storage/media \
     --since 15 --keep-profile-langs

# Auto-maintain: full scan (daily 1 AM via cron)
$SQM auto-maintain --path-prefix /APPBOX_DATA/storage/media \
     --keep-profile-langs
```

### `bazarr_subtitle_recovery.sh` — Escalating Recovery

```bash
RECOVERY=/config/berenstuff/automation/scripts/subtitles/bazarr_subtitle_recovery.sh

# Run recovery for items missing subs not touched recently
$RECOVERY --since 30

# Dry-run to see what would happen
$RECOVERY --dry-run

# Report current recovery state
$RECOVERY --report
```

### `library_subtitle_dedupe.sh` — Deduplication

```bash
DEDUPE=/config/berenstuff/automation/scripts/subtitles/library_subtitle_dedupe.sh

# Quick scan (every 5 min via cron)
$DEDUPE --since 10

# Full library scan (weekly Sunday)
$DEDUPE

# Dry-run to preview pruning decisions
$DEDUPE --dry-run --since 10
```

---

## 🏗️ Architecture

```
Sonarr/Radarr Download Event
          │
          ▼
arr_profile_extract_on_import.sh
          │
          ├─ [Phase 0] Extract non-profile embedded TEXT subs → .srt files
          │            Strip bitmap subs that don't belong
          │
          ├─ [Phase 1] Collision check before mux
          │
          ├─ [Phase 1.5] Delete non-profile externals once profile satisfied
          │
          ├─ [Phase 2] Dedupe embedded GOOD-GOOD dupes
          │
          ├─ Bazarr rescan (picks up extracted .srt files)
          │
          ├─ Per-language Bazarr subtitle search
          │
          ├─ Translation fallback → translator.py --file (background)
          │
          └─ Codec enqueue-import (background)


Cron (every 10 min / daily):
  subtitle_quality_manager.sh auto-maintain
          │
          ├─ Load streaming candidates (skip if flagged)
          ├─ Scan for changed files (--since OR full)
          ├─ Mux GOOD external SRTs into MKV
          ├─ Strip BAD embedded tracks
          ├─ Emby refresh (deduplicated per dir)
          ├─ Bazarr scan-disk (deduplicated per dir)
          └─ Discord summary embed
```

### 🗄️ Shared Library (`lib_subtitle_common.sh`)

Sourced by every script in this directory. Never execute directly.

| Helper | Purpose |
|--------|---------|
| `is_tv_path()` / `is_movie_path()` | Library path classification |
| `expand_lang_codes()` | Accept 2-letter or 3-letter language codes |
| `normalize_track_lang()` | Fast ISO 639-2→1 normalization |
| `detect_srt_language()` | Identify language of an SRT file |
| `resolve_bazarr_profile_langs()` | Read profile from Bazarr DB |
| `get_audio_languages()` | Extract audio track languages from ffprobe JSON |
| `bazarr_rescan_for_file()` | Trigger Bazarr scan-disk (DRY-deduped) |
| `strip_all_embedded_subs()` | Full embedded subtitle strip via ffmpeg |
| `is_text_sub_codec()` | Distinguish extractable (SRT/ASS) from bitmap (PGS/DVDSUB) |
| `load_streaming_candidates()` | Pre-load streaming DB paths into associative array |
| `is_streaming_candidate()` | Pure-bash check — no subprocess in hot loop |

---

## 📅 Cron Schedule

| Interval | Command | Purpose |
|----------|---------|---------|
| `*/5 min` | `library_subtitle_dedupe.sh --since 10` | Quick dedupe scan |
| `0 4 Sun` | `library_subtitle_dedupe.sh` | Full weekly dedupe |
| `*/10 min` | `auto-maintain --since 15` | Quick mux/strip scan |
| `0 1 daily` | `auto-maintain` (full) | Full nightly maintenance |
| `*/15 min` | `bazarr_subtitle_recovery.sh --since 30` | Recovery escalation |
| `*/30 min` | `translator.py translate --since 60` | DeepL translation |

> All jobs use `flock` at the crontab level — auto-maintain and dedupe share a lock.

---

## ⚙️ Configuration

All secrets in `/config/berenstuff/.env`:

```bash
BAZARR_API_KEY=...
BAZARR_URL=http://127.0.0.1:6767/bazarr
EMBY_URL=http://...
EMBY_API_KEY=...
DISCORD_WEBHOOK_URL=...
RADARR_KEY=...
SONARR_KEY=...
```

Bazarr DB: `/opt/bazarr/data/db/bazarr.db`
State DBs: `/APPBOX_DATA/storage/.subtitle-quality-state/`, `.subtitle-dedupe-state/`, `.subtitle-recovery-state/`
