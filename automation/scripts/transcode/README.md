# 🎞️ Library Codec Manager

> SQLite-backed audit → plan → convert pipeline that normalizes your entire media library to H.264 + AAC. Priority queue, UHD/4K skip, streaming-candidate awareness, and full Discord telemetry.

---

## 🗂️ Files

| File | Role |
|------|------|
| `library_codec_manager.sh` | 🚀 Single-file manager — all subcommands, state management, conversion logic |

### State (external)

| Path | Contents |
|------|---------|
| `/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db` | SQLite — `media_files`, `conversion_plan`, `conversion_runs`, `audit_status`, `probe_streams` |
| `/APPBOX_DATA/storage/.transcode-state-media/backups/` | Original files kept for 7 days post-swap |
| `/APPBOX_DATA/storage/.transcode-state-media/manager.log` | Timestamped operation log |

---

## ✨ Features

- **🎯 H.264 CRF 19 + AAC 192k** — high quality, broad compatibility target codec profile
- **📦 MKV/MP4 output container** — preserves subtitle tracks, chapters, and all metadata
- **⚡ Priority queue** — audio-only remux (priority 1) before video transcode (priority 10); import-hook files get priority 0 (highest)
- **🛡️ UHD/4K/HDR skip** — never touches 4K or HDR content
- **🌊 Streaming candidate skip** — files flagged by the streaming checker are excluded from conversion
- **🔄 Profile-aware audio selection** — resolves Bazarr profile + original language from metadata; keeps only profile + orig audio streams
- **✅ Triple verification** — codec check + duration check + subtitle count check before swapping original
- **🔒 Safe swap workflow** — temp output → verify → backup original → atomic rename
- **📡 Post-swap rescan** — triggers Sonarr/Radarr rescan + Bazarr scan-disk + direct Bazarr DB `audio_language` update after every swap
- **📊 Rich Discord audit notification** — progress bar, conversion rate, ETA, per-category counts
- **🗑️ Backup pruning** — auto-expire originals after 7-day retention window

---

## 🔧 CLI Usage

```bash
CODEC=/config/berenstuff/automation/scripts/transcode/library_codec_manager.sh
```

### 🔍 `audit` — Probe files from Bazarr DB, store metadata

```bash
# Audit entire library (runs daily at 3 AM via cron, ~7 min)
$CODEC audit

# Audit with debug logging
$CODEC audit --log-level debug

# Restrict to a path prefix
$CODEC audit --path-prefix /APPBOX_DATA/storage/media/tv
```

### 📋 `plan` — Build conversion eligibility plan

```bash
# Plan from latest audit results
$CODEC plan

# Dry-run: see what would be planned
$CODEC plan --dry-run --log-level debug
```

### 📊 `report` — Show audit + plan summary

```bash
$CODEC report
# Shows: total files, eligible count, skip reasons, priority breakdown
```

### ▶️ `convert` / `resume` — Convert planned files

```bash
# Convert one file (cron: every 15 min, batch-size 1)
$CODEC resume --batch-size 1

# Convert up to 5 files in one run
$CODEC resume --batch-size 5

# Dry-run: preview what would be converted
$CODEC convert --dry-run --batch-size 20
```

### 📥 `enqueue-import` — Fast-path for newly imported files

```bash
# Called automatically by the import hook (highest priority = 0)
$CODEC enqueue-import \
  --file "/APPBOX_DATA/storage/media/tv/Show/S01E01.mkv" \
  --media-type series \
  --ref-id 12345

# If already compliant → skip; if already priority 0 → no-op;
# if priority 1/10 → upgrade to 0; if ineligible → priority 99
```

### 📈 `daily-status` — Send daily Discord status

```bash
# Runs weekly Tuesday 3:35 AM via cron
$CODEC daily-status
```

### 🗑️ `prune-backups` — Remove old backup files

```bash
# Remove backups older than 7 days (default retention)
$CODEC prune-backups

# Custom retention window
$CODEC prune-backups --retention-days 14
```

---

## 🏗️ Architecture

```
Daily 3 AM                      Every 15 min
     │                               │
     ▼                               ▼
  audit cmd                      resume cmd
  (probe all files               (pick next eligible
   from Bazarr DB)                from conversion_plan
     │                            ORDER BY priority ASC)
     ▼                               │
  plan cmd                           ▼
  (evaluate eligibility)       ffmpeg transcode
  ┌── eligible=1, priority=1    H.264 CRF19 / AAC 192k
  │   (audio-only remux)              │
  ├── eligible=1, priority=10         ▼
  │   (video transcode)         verify_transcoded_file()
  └── eligible=0, reason=...    ├─ codec check
      (UHD, HDR, streaming,     ├─ duration check
       already compliant, etc.) └─ subtitle count check
                                       │
                                       ▼ (all pass)
                               backup original
                               atomic rename temp → final
                                       │
                                       ▼
                               arr_rescan_for_media()
                               bazarr_rescan_for_media()
                               update_bazarr_audio_language()
                               emby_refresh_item()
                               discord notify


Import event (parallel path):
  arr_profile_extract_on_import.sh
       └──► enqueue-import  (priority=0, highest)
```

### 🗄️ Database Schema

```sql
-- All probed files
media_files (id, path, media_type, bazarr_ref_id, size_bytes, ...)

-- Eligibility decisions
conversion_plan (media_id, eligible, reason, priority, skip_reason, ...)
-- priority: 0=import, 1=audio-only, 10=video, 99=ineligible

-- Conversion execution history
conversion_runs (id, media_id, status, start_ts, end_ts, ...)
-- status: running | swapped | failed | skipped

-- Per-scan probe results
audit_status (media_id, probe_ok, last_probed, ...)

-- Stream-level details
probe_streams (media_id, stream_index, codec_type, codec_name, ...)
```

---

## 📅 Cron Schedule

| Time | Command | Purpose |
|------|---------|---------|
| `0 3 daily` | `audit` | Incremental probe of Bazarr DB (~7 min) |
| `0 3 daily` | `plan` | Rebuild eligibility after audit |
| `*/15 min` | `resume --batch-size 1` | Convert next file in queue |
| `35 3 Tue` | `daily-status` | Weekly Discord status ping |

> Uses `flock` at crontab level — audit/plan/resume never overlap.

---

## ⚙️ Conversion Policy

| Decision | Codec / Condition |
|----------|------------------|
| **Target video** | `h264` (libx264, CRF 19, preset medium, yuv420p) |
| **Target audio** | `aac` (192k, stereo, 48kHz) |
| **Container** | MKV or MP4 (preserved); others → MKV |
| **Skip: already compliant** | video=h264 + audio=aac + container=mkv/mp4 |
| **Skip: UHD/4K/HDR** | width > 3800 OR HDR metadata detected |
| **Skip: streaming candidate** | path in streaming checker DB |
| **Audio stream selection** | Bazarr profile langs + detected original language |
| **Subtitle streams** | All pass-through (never re-encoded) |

---

## ⚙️ Configuration

All secrets in `/config/berenstuff/.env`:

```bash
EMBY_URL=http://...
EMBY_API_KEY=...
SONARR_URL=http://127.0.0.1:8989/sonarr
SONARR_KEY=...
RADARR_URL=http://127.0.0.1:7878/radarr
RADARR_KEY=...
BAZARR_API_KEY=...
DISCORD_WEBHOOK_AUDIT_DONE=https://discord.com/api/webhooks/...
DISCORD_WEBHOOK_STATUS=https://discord.com/api/webhooks/...
```
