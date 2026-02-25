# Subtitle Quality Manager Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a manual tool that audits, muxes, and strips subtitle tracks from MKV files, solving the Emby external subtitle delivery failure.

**Architecture:** Single Bash script with three subcommands (`audit`, `mux`, `strip`). Sources `lib_subtitle_common.sh` for shared helpers. Uses ffprobe to analyze tracks, ffmpeg to remux. Checks codec manager state DB before modifying files to avoid conflicts.

**Tech Stack:** Bash, ffprobe, ffmpeg, jq, sqlite3, Bazarr API

---

### Task 1: Script skeleton with CLI parsing

**Files:**
- Create: `automation/scripts/subtitles/subtitle_quality_manager.sh`

**Context:** This is a new script. It needs shebang, `set -euo pipefail`, source the shared lib, parse subcommands (`audit`, `mux`, `strip`) and common flags.

**Step 1: Create the script skeleton**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib_subtitle_common.sh"

# Defaults
PATH_PREFIX=""
RECURSIVE=0
DRY_RUN=0
FORCE=0
TRACK_TARGET=""
BAZARR_URL="http://127.0.0.1:6767/bazarr"
BAZARR_API_KEY="${BAZARR_API_KEY:-}"
BAZARR_DB="/opt/bazarr/data/db/bazarr.db"
CODEC_STATE_DIR="/APPBOX_DATA/storage/.transcode-state-media"
LOG_LEVEL="info"

# Watermark patterns (case-insensitive grep)
WATERMARK_PATTERNS="galaxytv|yify|yts|opensubtitles|addic7ed|subscene|podnapisi|sub[sz]cene"

usage() {
  cat <<'EOF'
Usage: subtitle_quality_manager.sh <command> [options]

Commands:
  audit    Score subtitle tracks (embedded + external) and output quality report
  mux      Embed good external .srt files into MKV (runs audit first)
  strip    Remove specific embedded subtitle tracks from MKV

Common options:
  --path DIR            Media directory to process (required)
  --recursive           Process subdirectories recursively
  --dry-run             Preview changes without modifying files
  --bazarr-url URL      Bazarr base URL (default: http://127.0.0.1:6767/bazarr)
  --bazarr-db PATH      Bazarr DB path (default: /opt/bazarr/data/db/bazarr.db)
  --state-dir DIR       Codec manager state dir (default: /APPBOX_DATA/storage/.transcode-state-media)
  --log-level LEVEL     Log level: info or debug (default: info)
  --help                Show this help

Mux options:
  --force               Mux even if audit rates subtitles as WARN/BAD

Strip options:
  --track TARGET        Language code (e.g. eng) or stream index (e.g. 2) to remove

Examples:
  subtitle_quality_manager.sh audit --path "/APPBOX_DATA/storage/media/tv/Evil" --recursive
  subtitle_quality_manager.sh mux --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" --dry-run
  subtitle_quality_manager.sh strip --path "/APPBOX_DATA/storage/media/tv/Evil" --track eng --recursive --dry-run
EOF
}

COMMAND="${1:-}"
[[ -z "$COMMAND" ]] && { usage; exit 1; }
shift

case "$COMMAND" in
  audit|mux|strip) ;;
  --help|-h) usage; exit 0 ;;
  *) echo "Unknown command: $COMMAND" >&2; usage; exit 1 ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)       PATH_PREFIX="${2:-}"; shift 2 ;;
    --recursive)  RECURSIVE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --force)      FORCE=1; shift ;;
    --track)      TRACK_TARGET="${2:-}"; shift 2 ;;
    --bazarr-url) BAZARR_URL="${2:-}"; shift 2 ;;
    --bazarr-db)  BAZARR_DB="${2:-}"; shift 2 ;;
    --state-dir)  CODEC_STATE_DIR="${2:-}"; shift 2 ;;
    --log-level)  LOG_LEVEL="${2:-}"; shift 2 ;;
    --help|-h)    usage; exit 0 ;;
    *)            echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$PATH_PREFIX" ]]; then
  echo "--path is required." >&2; exit 1
fi

if [[ "$COMMAND" == "strip" ]] && [[ -z "$TRACK_TARGET" ]]; then
  echo "--track is required for strip command." >&2; exit 1
fi

BAZARR_API_KEY="${BAZARR_API_KEY:-$(getenv_fallback BAZARR_API_KEY BAZARR_KEY)}"

log() {
  printf '%s [sub-quality] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

debug() {
  [[ "$LOG_LEVEL" == "debug" ]] && log "DEBUG $*"
  return 0
}

# Placeholder functions — implemented in subsequent tasks
cmd_audit() { log "audit not yet implemented"; }
cmd_mux() { log "mux not yet implemented"; }
cmd_strip() { log "strip not yet implemented"; }

case "$COMMAND" in
  audit) cmd_audit ;;
  mux)   cmd_mux ;;
  strip) cmd_strip ;;
esac
```

**Step 2: Make executable and syntax check**

Run: `chmod +x automation/scripts/subtitles/subtitle_quality_manager.sh && bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: no output (clean parse)

**Step 3: Test CLI parsing**

Run: `bash automation/scripts/subtitles/subtitle_quality_manager.sh --help`
Expected: usage text displayed

Run: `bash automation/scripts/subtitles/subtitle_quality_manager.sh audit --path /tmp`
Expected: `audit not yet implemented`

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): script skeleton with CLI parsing"
```

---

### Task 2: Converter conflict check and file discovery

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh`

**Context:** Before modifying any MKV, we need to check if the codec manager is currently converting it. Also need a function to discover MKV files (with optional recursion).

**Step 1: Add helper functions**

Add these functions before the placeholder `cmd_audit`:

```bash
# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

find_mkv_files() {
  local dir="$1"
  if [[ "$RECURSIVE" -eq 1 ]]; then
    find "$dir" -type f -name "*.mkv" | sort
  else
    find "$dir" -maxdepth 1 -type f -name "*.mkv" | sort
  fi
}

# ---------------------------------------------------------------------------
# Converter conflict safety
# ---------------------------------------------------------------------------

is_file_being_converted() {
  local filepath="$1"
  local state_db="$CODEC_STATE_DIR/library_codec_state.db"
  [[ -f "$state_db" ]] || return 1
  local running
  running="$(sqlite3 "$state_db" "SELECT COUNT(*) FROM conversion_plan WHERE status='running' AND media_id IN (SELECT media_id FROM media_files WHERE file_path='$(sql_escape "$filepath")');" 2>/dev/null || echo 0)"
  [[ "$running" -gt 0 ]]
}

# ---------------------------------------------------------------------------
# SRT parsing helpers
# ---------------------------------------------------------------------------

# Parse SRT file and return: cue_count, first_cue_sec, last_cue_sec, has_mojibake, watermark_hits
analyze_srt_file() {
  local srt_file="$1"
  local cue_count=0 first_ms=0 last_ms=0

  # Count cues (lines matching HH:MM:SS,mmm --> HH:MM:SS,mmm)
  cue_count="$(grep -cE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> ' "$srt_file" 2>/dev/null || echo 0)"

  # First and last cue timestamps (in seconds)
  if [[ "$cue_count" -gt 0 ]]; then
    first_ms="$(grep -oEm1 '^([0-9]{2}):([0-9]{2}):([0-9]{2}),([0-9]{3}) -->' "$srt_file" | head -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
    last_ms="$(grep -oE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} -->' "$srt_file" | tail -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
  fi

  # Mojibake detection: look for common replacement chars
  local mojibake=0
  if grep -qP '[\x{FFFD}]|Ã©|Ã¡|Ã±|Ã³|Ã­|â€™|â€œ|â€' "$srt_file" 2>/dev/null; then
    mojibake=1
  fi

  # Watermark detection
  local watermarks=0
  if grep -qiE "$WATERMARK_PATTERNS" "$srt_file" 2>/dev/null; then
    watermarks=1
  fi

  printf '%d %.1f %.1f %d %d' "$cue_count" "$first_ms" "$last_ms" "$mojibake" "$watermarks"
}

# Get video duration in seconds via ffprobe
get_video_duration() {
  local mkv_file="$1"
  ffprobe -v quiet -print_format json -show_format "$mkv_file" 2>/dev/null \
    | jq -r '.format.duration // "0"' | awk '{printf "%.1f", $1}'
}

# Get embedded subtitle streams as JSON array
get_embedded_subs() {
  local mkv_file="$1"
  ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null \
    | jq -c '[.streams[] | {index, codec_name, tags: {language: (.tags.language // "und"), title: (.tags.title // "")}}]'
}
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: no output

**Step 3: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): add file discovery, conflict check, SRT analysis helpers"
```

---

### Task 3: Implement `audit` command

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh`

**Context:** The `audit` command scans MKV files, analyzes embedded and external subtitle tracks, scores each one, and outputs a formatted report.

**Step 1: Implement `cmd_audit`**

Replace the placeholder `cmd_audit` with:

```bash
# Score a subtitle: returns GOOD, WARN, or BAD
# $1=cue_count $2=first_sec $3=last_sec $4=video_duration $5=mojibake $6=watermarks
score_subtitle() {
  local cues="$1" first="$2" last="$3" duration="$4" mojibake="$5" watermarks="$6"
  local rating="GOOD"

  # Encoding check (highest priority)
  [[ "$mojibake" -eq 1 ]] && { echo "BAD"; return; }

  # Cue count per hour
  if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]]; then
    local cues_per_hour
    cues_per_hour="$(awk "BEGIN { printf \"%.0f\", ($cues / $duration) * 3600 }")"
    if [[ "$cues_per_hour" -lt 200 ]]; then
      echo "BAD"; return
    elif [[ "$cues_per_hour" -gt 1200 ]]; then
      rating="WARN"
    fi
  fi

  # Duration coverage
  if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
    local coverage
    coverage="$(awk "BEGIN { printf \"%.0f\", ($last / $duration) * 100 }")"
    if [[ "$coverage" -lt 50 ]]; then
      echo "BAD"; return
    elif [[ "$coverage" -lt 70 ]]; then
      rating="WARN"
    fi
  fi

  # Timing sync
  if [[ "$(awk "BEGIN { print ($first > 120) }")" -eq 1 ]]; then
    rating="WARN"
  fi

  # Watermarks (downgrade to WARN but don't BAD)
  [[ "$watermarks" -eq 1 ]] && rating="WARN"

  echo "$rating"
}

cmd_audit() {
  log "Auditing subtitles in: $PATH_PREFIX (recursive=$RECURSIVE)"

  local total_files=0 total_tracks=0 good=0 warn=0 bad=0

  while IFS= read -r mkv_file; do
    total_files=$((total_files + 1))
    local basename
    basename="$(basename "$mkv_file")"
    local dir
    dir="$(dirname "$mkv_file")"
    local duration
    duration="$(get_video_duration "$mkv_file")"

    printf '\n=== %s (%.0fs) ===\n' "$basename" "$duration"
    printf '%-6s %-8s %-5s %-6s %-6s %-5s %-4s %-4s %s\n' \
      "TYPE" "LANG" "CODEC" "CUES" "COVER" "SYNC" "WM" "ENC" "RATING"
    printf '%s\n' "--------------------------------------------------------------"

    # Embedded subtitle tracks
    local embedded_json
    embedded_json="$(get_embedded_subs "$mkv_file")"
    local emb_count
    emb_count="$(jq 'length' <<<"$embedded_json")"

    for ((i=0; i<emb_count; i++)); do
      local stream_idx lang title codec_name
      stream_idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
      lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
      title="$(jq -r ".[$i].tags.title" <<<"$embedded_json")"
      codec_name="$(jq -r ".[$i].codec_name" <<<"$embedded_json")"

      # Extract embedded sub to temp file for analysis
      local tmpfile="/tmp/sub_audit_${$}_${stream_idx}.srt"
      ffmpeg -v quiet -i "$mkv_file" -map "0:${stream_idx}" -f srt "$tmpfile" 2>/dev/null || { rm -f "$tmpfile"; continue; }

      local analysis
      analysis="$(analyze_srt_file "$tmpfile")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
      rm -f "$tmpfile"

      # Check title for watermarks too
      if echo "$title" | grep -qiE "$WATERMARK_PATTERNS"; then
        watermarks=1
      fi

      local coverage="--"
      if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
        coverage="$(awk "BEGIN { printf \"%.0f%%\", ($last_sec / $duration) * 100 }")"
      fi
      local sync_ok="OK"
      [[ "$(awk "BEGIN { print ($first_sec > 120) }")" -eq 1 ]] && sync_ok="LATE"

      local rating
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      printf '%-6s %-8s %-5s %-6s %-6s %-5s %-4s %-4s %s\n' \
        "EMB" "$lang" "$codec_name" "$cues" "$coverage" "$sync_ok" \
        "$([[ "$watermarks" -eq 1 ]] && echo "YES" || echo "--")" \
        "$([[ "$mojibake" -eq 1 ]] && echo "BAD" || echo "OK")" \
        "$rating"

      total_tracks=$((total_tracks + 1))
      case "$rating" in
        GOOD) good=$((good + 1)) ;;
        WARN) warn=$((warn + 1)) ;;
        BAD)  bad=$((bad + 1)) ;;
      esac
    done

    # External subtitle files
    local name_stem="${basename%.mkv}"
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename
      srt_basename="$(basename "$srt_file")"
      # Extract language from filename (e.g., "show.en.srt" -> "en")
      local ext_lang
      ext_lang="$(echo "$srt_basename" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      local analysis
      analysis="$(analyze_srt_file "$srt_file")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"

      local coverage="--"
      if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
        coverage="$(awk "BEGIN { printf \"%.0f%%\", ($last_sec / $duration) * 100 }")"
      fi
      local sync_ok="OK"
      [[ "$(awk "BEGIN { print ($first_sec > 120) }")" -eq 1 ]] && sync_ok="LATE"

      local rating
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      printf '%-6s %-8s %-5s %-6s %-6s %-5s %-4s %-4s %s\n' \
        "EXT" "$ext_lang" "srt" "$cues" "$coverage" "$sync_ok" \
        "$([[ "$watermarks" -eq 1 ]] && echo "YES" || echo "--")" \
        "$([[ "$mojibake" -eq 1 ]] && echo "BAD" || echo "OK")" \
        "$rating"

      total_tracks=$((total_tracks + 1))
      case "$rating" in
        GOOD) good=$((good + 1)) ;;
        WARN) warn=$((warn + 1)) ;;
        BAD)  bad=$((bad + 1)) ;;
      esac
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null | sort)

  done < <(find_mkv_files "$PATH_PREFIX")

  printf '\n--- Summary: %d files, %d tracks (%d GOOD, %d WARN, %d BAD) ---\n' \
    "$total_files" "$total_tracks" "$good" "$warn" "$bad"
}
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: no output

**Step 3: Test with Evil S01E01**

Run: `bash automation/scripts/subtitles/subtitle_quality_manager.sh audit --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" 2>&1 | head -30`

Expected: per-file table showing embedded SubRip (with watermark flag for "GalaxyTV") and external .en.srt + .es.srt with GOOD/WARN/BAD ratings.

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): implement audit command with quality scoring"
```

---

### Task 4: Implement `mux` command

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh`

**Context:** The `mux` command runs audit internally, then embeds external SRTs rated GOOD (or with `--force`) into the MKV via ffmpeg remux. Deletes the external files after and triggers Bazarr scan-disk.

**Step 1: Implement `cmd_mux`**

Replace the placeholder `cmd_mux` with:

```bash
cmd_mux() {
  log "Muxing external subtitles in: $PATH_PREFIX (recursive=$RECURSIVE, dry_run=$DRY_RUN, force=$FORCE)"

  local total_files=0 muxed=0 skipped=0 failed=0
  local mux_summary=""

  while IFS= read -r mkv_file; do
    local basename dir name_stem duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    name_stem="${basename%.mkv}"
    duration="$(get_video_duration "$mkv_file")"

    # Check converter conflict
    if is_file_being_converted "$mkv_file"; then
      log "SKIP (converter running): $basename"
      skipped=$((skipped + 1))
      continue
    fi

    # Find external SRT files for this MKV
    local -a srt_files=()
    local -a srt_langs=()
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename ext_lang
      srt_basename="$(basename "$srt_file")"
      ext_lang="$(echo "$srt_basename" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      # Audit the SRT
      local analysis rating
      analysis="$(analyze_srt_file "$srt_file")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      if [[ "$rating" == "BAD" ]] && [[ "$FORCE" -eq 0 ]]; then
        log "SKIP (BAD rating): $srt_basename"
        skipped=$((skipped + 1))
        continue
      fi
      if [[ "$rating" == "WARN" ]] && [[ "$FORCE" -eq 0 ]]; then
        log "SKIP (WARN rating, use --force): $srt_basename"
        skipped=$((skipped + 1))
        continue
      fi

      srt_files+=("$srt_file")
      srt_langs+=("$ext_lang")
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null | sort)

    [[ ${#srt_files[@]} -eq 0 ]] && continue

    total_files=$((total_files + 1))

    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "[DRY-RUN] Would mux ${#srt_files[@]} subtitle(s) into: $basename"
      for sf in "${srt_files[@]}"; do log "  + $(basename "$sf")"; done
      muxed=$((muxed + ${#srt_files[@]}))
      continue
    fi

    # Build ffmpeg command: input MKV + all SRT inputs, map everything, copy codecs
    local -a ffmpeg_cmd=(ffmpeg -y -v quiet -i "$mkv_file")
    local -a map_args=(-map 0)
    local metadata_idx
    # Get current subtitle stream count to set metadata correctly
    local existing_sub_count
    existing_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null | jq '.streams | length')"

    for ((i=0; i<${#srt_files[@]}; i++)); do
      ffmpeg_cmd+=(-i "${srt_files[$i]}")
      map_args+=(-map "$((i + 1)):0")
      metadata_idx=$((existing_sub_count + i))
      map_args+=(-metadata:s:s:${metadata_idx} "language=${srt_langs[$i]}")
    done

    local tmp_out="${mkv_file}.subtmp.mkv"
    "${ffmpeg_cmd[@]}" "${map_args[@]}" -c copy "$tmp_out" 2>/dev/null

    if [[ $? -ne 0 ]] || [[ ! -s "$tmp_out" ]]; then
      log "FAIL mux: $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    # Verify the output has the expected subtitle count
    local new_sub_count
    new_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$tmp_out" 2>/dev/null | jq '.streams | length')"
    local expected=$((existing_sub_count + ${#srt_files[@]}))

    if [[ "$new_sub_count" -ne "$expected" ]]; then
      log "FAIL mux (sub count mismatch: got $new_sub_count, expected $expected): $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    # Swap: replace original with muxed version
    mv "$tmp_out" "$mkv_file"

    # Delete the external SRT files
    for sf in "${srt_files[@]}"; do
      rm -f "$sf"
      log "  Deleted: $(basename "$sf")"
    done

    muxed=$((muxed + ${#srt_files[@]}))
    log "MUXED ${#srt_files[@]} subtitle(s) into: $basename"
    mux_summary="${mux_summary}${basename}: ${#srt_files[@]} sub(s)\n"

  done < <(find_mkv_files "$PATH_PREFIX")

  log "Done. ${muxed} muxed, ${skipped} skipped, ${failed} failed."

  # Bazarr rescan (if we muxed anything)
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    # Determine if this is TV or movies by path
    if [[ "$PATH_PREFIX" == *"/tv/"* ]] || [[ "$PATH_PREFIX" == *"/tvanimated/"* ]]; then
      # Get Sonarr series ID from Bazarr DB
      local sonarr_id
      sonarr_id="$(sqlite3 "$BAZARR_DB" "SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(basename "$(dirname "$PATH_PREFIX")")%' LIMIT 1;" 2>/dev/null || echo "")"
      if [[ -n "$sonarr_id" ]]; then
        bazarr_scan_disk_series "$sonarr_id" "$BAZARR_URL" "$BAZARR_API_KEY"
      fi
    elif [[ "$PATH_PREFIX" == *"/movies/"* ]]; then
      local radarr_id
      radarr_id="$(sqlite3 "$BAZARR_DB" "SELECT radarrId FROM table_movies WHERE path LIKE '%$(basename "$PATH_PREFIX")%' LIMIT 1;" 2>/dev/null || echo "")"
      if [[ -n "$radarr_id" ]]; then
        bazarr_scan_disk_movie "$radarr_id" "$BAZARR_URL" "$BAZARR_API_KEY"
      fi
    fi
  fi

  # Discord notification
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Mux" \
      "$(printf "Muxed %d subtitle(s) into %d file(s)\n\n%b" "$muxed" "$total_files" "$mux_summary")" \
      3066993
  fi
}
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: no output

**Step 3: Test with Evil (dry-run)**

Run: `source /config/berenstuff/.env && bash automation/scripts/subtitles/subtitle_quality_manager.sh mux --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" --dry-run 2>&1`

Expected: `[DRY-RUN] Would mux 2 subtitle(s) into: Evil - S01E01 - ...` for each episode (one .en.srt + one .es.srt).

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): implement mux command with audit gate and Bazarr sync"
```

---

### Task 5: Implement `strip` command

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh`

**Context:** The `strip` command removes specific embedded subtitle tracks from MKV by language code or stream index.

**Step 1: Implement `cmd_strip`**

Replace the placeholder `cmd_strip` with:

```bash
cmd_strip() {
  log "Stripping track '$TRACK_TARGET' from: $PATH_PREFIX (recursive=$RECURSIVE, dry_run=$DRY_RUN)"

  local total=0 stripped=0 skipped=0 failed=0

  while IFS= read -r mkv_file; do
    local basename
    basename="$(basename "$mkv_file")"

    # Check converter conflict
    if is_file_being_converted "$mkv_file"; then
      log "SKIP (converter running): $basename"
      skipped=$((skipped + 1))
      continue
    fi

    # Get embedded subtitle streams
    local embedded_json
    embedded_json="$(get_embedded_subs "$mkv_file")"
    local emb_count
    emb_count="$(jq 'length' <<<"$embedded_json")"
    [[ "$emb_count" -eq 0 ]] && continue

    # Find matching stream indices to remove
    local -a remove_indices=()

    if [[ "$TRACK_TARGET" =~ ^[0-9]+$ ]]; then
      # Numeric: treat as stream index
      for ((i=0; i<emb_count; i++)); do
        local idx
        idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
        [[ "$idx" == "$TRACK_TARGET" ]] && remove_indices+=("$idx")
      done
    else
      # String: treat as language code
      for ((i=0; i<emb_count; i++)); do
        local idx lang
        idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
        lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
        [[ "$lang" == "$TRACK_TARGET" ]] && remove_indices+=("$idx")
      done
    fi

    [[ ${#remove_indices[@]} -eq 0 ]] && continue

    total=$((total + 1))

    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "[DRY-RUN] Would strip ${#remove_indices[@]} track(s) from: $basename (indices: ${remove_indices[*]})"
      stripped=$((stripped + ${#remove_indices[@]}))
      continue
    fi

    # Build ffmpeg command to remove specific streams
    local -a ffmpeg_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
    for idx in "${remove_indices[@]}"; do
      ffmpeg_cmd+=(-map "-0:${idx}")
    done
    ffmpeg_cmd+=(-c copy)

    local tmp_out="${mkv_file}.striptmp.mkv"
    "${ffmpeg_cmd[@]}" "$tmp_out" 2>/dev/null

    if [[ $? -ne 0 ]] || [[ ! -s "$tmp_out" ]]; then
      log "FAIL strip: $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    mv "$tmp_out" "$mkv_file"
    stripped=$((stripped + ${#remove_indices[@]}))
    log "STRIPPED ${#remove_indices[@]} track(s) from: $basename"

  done < <(find_mkv_files "$PATH_PREFIX")

  log "Done. ${stripped} stripped, ${skipped} skipped, ${failed} failed."

  # Discord notification
  if [[ "$stripped" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Strip" \
      "$(printf "Stripped %d track(s) matching '%s' from %d file(s)" "$stripped" "$TRACK_TARGET" "$total")" \
      15158332
  fi
}
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: no output

**Step 3: Test with Evil (dry-run) — strip the "GalaxyTV" watermarked embedded track**

Run: `bash automation/scripts/subtitles/subtitle_quality_manager.sh strip --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" --track eng --recursive --dry-run 2>&1`

Expected: `[DRY-RUN] Would strip 1 track(s) from: Evil - S01E01 - ... (indices: 2)` for each episode.

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): implement strip command"
```

---

### Task 6: Compat sync, end-to-end test with Evil, and push

**Files:**
- Create: `scripts/subtitle_quality_manager.sh` (compat copy)

**Step 1: Sync compat copy**

```bash
cp automation/scripts/subtitles/subtitle_quality_manager.sh scripts/subtitle_quality_manager.sh
chmod +x scripts/subtitle_quality_manager.sh
```

**Step 2: Syntax check both**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh && bash -n scripts/subtitle_quality_manager.sh`
Expected: no output

**Step 3: End-to-end test with Evil (audit)**

Run: `bash automation/scripts/subtitles/subtitle_quality_manager.sh audit --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" 2>&1`

Verify:
- Per-file tables showing each subtitle track
- Embedded eng track flagged with WM=YES (GalaxyTV watermark)
- External .en.srt and .es.srt scored

**Step 4: End-to-end test with Evil (mux dry-run)**

Run: `source /config/berenstuff/.env && bash automation/scripts/subtitles/subtitle_quality_manager.sh mux --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" --dry-run 2>&1`

Verify: dry-run shows which SRTs would be muxed, no files modified.

**Step 5: End-to-end test with Evil S01E01 only (mux live — single file)**

Run: `source /config/berenstuff/.env && bash automation/scripts/subtitles/subtitle_quality_manager.sh mux --path "/APPBOX_DATA/storage/media/tv/Evil/Season 1" --force 2>&1`

Verify:
- External .en.srt and .es.srt are now embedded in the MKV
- External files are deleted
- `ffprobe` shows additional subtitle streams in the MKV
- Discord notification received

**Step 6: Commit and push**

```bash
git add scripts/subtitle_quality_manager.sh
git commit -m "chore: add subtitle_quality_manager.sh compat copy"
```
