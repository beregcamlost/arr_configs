# Library Codec Manager Runbook

Date: 2026-02-13

## Script
- `/config/berenstuff/scripts/library_codec_manager.sh`

## Goal
Normalize media to broad-compat targets with strong safety controls:
- Video: H.264 (x264), CRF 19, yuv420p
- Audio: AAC stereo, 48kHz
- Container: keep source container
- Skip conversion: UHD/4K and HDR
- Processing: single-file sequential, steady and slow

## State and Logs
- State dir: `/APPBOX_DATA/storage/.transcode-state`
- SQLite DB: `/APPBOX_DATA/storage/.transcode-state/library_codec_state.db`
- Log: `/APPBOX_DATA/storage/.transcode-state/manager.log`
- Backup originals: `/APPBOX_DATA/storage/.transcode-state/backups`
- Report: `/APPBOX_DATA/storage/.transcode-state/latest_report.md`

## Commands

### 1) Full audit (no conversion)
```bash
/config/berenstuff/scripts/library_codec_manager.sh audit --log-level info
```

### 2) Build conversion plan
```bash
/config/berenstuff/scripts/library_codec_manager.sh plan --log-level info
```

### 3) Generate report
```bash
/config/berenstuff/scripts/library_codec_manager.sh report --log-level info
```

### 4) Safety check conversion commands only (no file changes)
```bash
/config/berenstuff/scripts/library_codec_manager.sh convert --dry-run --batch-size 20 --log-level info
```

### 5) Real conversion (unattended, sequential)
```bash
/config/berenstuff/scripts/library_codec_manager.sh convert --log-level info
```

### 6) Resume after interruption
```bash
/config/berenstuff/scripts/library_codec_manager.sh resume --log-level info
```

### 7) Prune backups older than 7 days
```bash
/config/berenstuff/scripts/library_codec_manager.sh prune-backups --log-level info
```

## Safety Behavior
- Never writes in place directly.
- Creates transcoded temp output first.
- Verifies output (codec + duration tolerance).
- Swaps only after verification.
- Keeps original in backup tree for rollback.
- Marks failed items and continues queue.

## Quick SQL Checks
```bash
sqlite3 -header -column /APPBOX_DATA/storage/.transcode-state/library_codec_state.db "SELECT COUNT(*) AS media FROM media_files;"
sqlite3 -header -column /APPBOX_DATA/storage/.transcode-state/library_codec_state.db "SELECT eligible, COUNT(*) FROM conversion_plan GROUP BY eligible;"
sqlite3 -header -column /APPBOX_DATA/storage/.transcode-state/library_codec_state.db "SELECT status, COUNT(*) FROM conversion_runs GROUP BY status;"
```
