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
PATH_PREFIX_ROOT=""
SINCE_MINUTES=0
STATE_DIR="/APPBOX_DATA/storage/.subtitle-quality-state"
EMBY_URL="${EMBY_URL:-}"
EMBY_API_KEY="${EMBY_API_KEY:-}"

WATERMARK_PATTERNS="galaxytv|yify|yts|opensubtitles|addic7ed|subscene|podnapisi|sub[sz]cene"

usage() {
  cat <<'EOF'
Usage: subtitle_quality_manager.sh <command> [options]

Commands:
  audit          Score subtitle tracks (embedded + external) and output quality report
  mux            Embed good external .srt files into MKV/MP4/M4V (runs audit first)
  strip          Remove specific embedded subtitle tracks from MKV/MP4/M4V
  auto-maintain  Automated mux/strip with safety checks (quick + full mode)

Common options:
  --path DIR            Media directory to process (required for audit/mux/strip)
  --recursive           Process subdirectories recursively
  --dry-run             Preview changes without modifying files
  --bazarr-url URL      Bazarr base URL (default: http://127.0.0.1:6767/bazarr)
  --bazarr-db PATH      Bazarr DB path (default: /opt/bazarr/data/db/bazarr.db)
  --state-dir DIR       State DB directory (default: /APPBOX_DATA/storage/.subtitle-quality-state)
  --codec-state-dir DIR Codec converter state dir for conflict detection
  --log-level LEVEL     Log level: info or debug (default: info)
  --help                Show this help

Mux options:
  --force               Mux even if audit rates subtitles as WARN/BAD

Strip options:
  --track TARGET        Language code (e.g. eng) or stream index (e.g. 2) to remove

Auto-maintain options:
  --path-prefix DIR     Root media directory to scan recursively (required)
  --since N             Only scan files with SRTs modified in last N minutes (quick mode)
  --emby-url URL        Emby server URL (default: from EMBY_URL env)
  --emby-api-key KEY    Emby API key (default: from EMBY_API_KEY env)

Examples:
  subtitle_quality_manager.sh audit --path "/media/tv/Evil" --recursive
  subtitle_quality_manager.sh mux --path "/media/tv/Evil/Season 1" --dry-run
  subtitle_quality_manager.sh strip --path "/media/tv/Evil" --track eng --recursive --dry-run
  subtitle_quality_manager.sh auto-maintain --path-prefix /media --since 15 --dry-run
  subtitle_quality_manager.sh auto-maintain --path-prefix /media  # full incremental scan
EOF
}

COMMAND="${1:-}"
[[ -z "$COMMAND" ]] && { usage; exit 1; }
shift

case "$COMMAND" in
  audit|mux|strip|auto-maintain) ;;
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
    --state-dir)  STATE_DIR="${2:-}"; shift 2 ;;
    --codec-state-dir) CODEC_STATE_DIR="${2:-}"; shift 2 ;;
    --log-level)  LOG_LEVEL="${2:-}"; shift 2 ;;
    --path-prefix) PATH_PREFIX_ROOT="${2:-}"; shift 2 ;;
    --since)       SINCE_MINUTES="${2:-0}"; shift 2 ;;
    --emby-url)    EMBY_URL="${2:-}"; shift 2 ;;
    --emby-api-key) EMBY_API_KEY="${2:-}"; shift 2 ;;
    --help|-h)    usage; exit 0 ;;
    *)            echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$PATH_PREFIX" ]] && [[ "$COMMAND" != "auto-maintain" ]]; then
  echo "--path is required." >&2; exit 1
fi

if [[ "$COMMAND" == "auto-maintain" ]] && [[ -z "$PATH_PREFIX_ROOT" ]]; then
  echo "--path-prefix is required for auto-maintain." >&2; exit 1
fi

if [[ "$COMMAND" == "strip" ]] && [[ -z "$TRACK_TARGET" ]]; then
  echo "--track is required for strip command." >&2; exit 1
fi

BAZARR_API_KEY="${BAZARR_API_KEY:-$(getenv_fallback BAZARR_API_KEY BAZARR_KEY)}"

# Override lib's log() with our own prefix
log() {
  printf '%s [sub-quality] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

debug() {
  [[ "$LOG_LEVEL" == "debug" ]] && log "DEBUG $*"
  return 0
}

init_state_db() {
  local db="$1"
  mkdir -p "$(dirname "$db")"
  sqlite3 "$db" "
    CREATE TABLE IF NOT EXISTS file_audits (
      file_path TEXT PRIMARY KEY,
      mtime INTEGER NOT NULL,
      last_audit_ts INTEGER NOT NULL,
      embedded_json TEXT DEFAULT '[]',
      external_json TEXT DEFAULT '[]',
      action_taken TEXT DEFAULT 'none'
    );
    PRAGMA journal_mode=WAL;
    PRAGMA busy_timeout=30000;
  "
}

# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

find_media_files() {
  local dir="$1"
  if [[ "$RECURSIVE" -eq 1 ]]; then
    find "$dir" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) | sort
  else
    find "$dir" -maxdepth 1 -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) | sort
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
# Language code helpers (MP4/M4V require ISO 639-2 three-letter codes)
# ---------------------------------------------------------------------------

lang_to_iso639_2() {
  local code="$1"
  case "${code,,}" in
    en)  echo "eng" ;; es)  echo "spa" ;; fr)  echo "fra" ;;
    de)  echo "deu" ;; it)  echo "ita" ;; pt)  echo "por" ;;
    zh)  echo "zho" ;; ja)  echo "jpn" ;; ko)  echo "kor" ;;
    ar)  echo "ara" ;; ru)  echo "rus" ;; nl)  echo "nld" ;;
    sv)  echo "swe" ;; da)  echo "dan" ;; fi)  echo "fin" ;;
    no)  echo "nor" ;; pl)  echo "pol" ;; cs)  echo "ces" ;;
    hu)  echo "hun" ;; ro)  echo "ron" ;; tr)  echo "tur" ;;
    th)  echo "tha" ;; vi)  echo "vie" ;; el)  echo "ell" ;;
    he)  echo "heb" ;; hi)  echo "hin" ;; id)  echo "ind" ;;
    uk)  echo "ukr" ;; bg)  echo "bul" ;; hr)  echo "hrv" ;;
    *)   echo "$code" ;;  # pass through 3-letter codes and 'und' as-is
  esac
}

# ---------------------------------------------------------------------------
# SRT parsing helpers
# ---------------------------------------------------------------------------

analyze_srt_file() {
  local srt_file="$1"
  local cue_count=0 first_ms=0 last_ms=0

  cue_count="$(grep -cE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> ' "$srt_file" 2>/dev/null || echo 0)"

  if [[ "$cue_count" -gt 0 ]]; then
    first_ms="$(grep -oEm1 '^([0-9]{2}):([0-9]{2}):([0-9]{2}),([0-9]{3}) -->' "$srt_file" | head -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
    last_ms="$(grep -oE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} -->' "$srt_file" | tail -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
  fi

  local mojibake=0
  if grep -qP '[\x{FFFD}]|Ã©|Ã¡|Ã±|Ã³|Ã­|â€™|â€œ|â€' "$srt_file" 2>/dev/null; then
    mojibake=1
  fi

  local watermarks=0
  if grep -qiE "$WATERMARK_PATTERNS" "$srt_file" 2>/dev/null; then
    watermarks=1
  fi

  printf '%d %.1f %.1f %d %d' "$cue_count" "$first_ms" "$last_ms" "$mojibake" "$watermarks"
}

get_video_duration() {
  local mkv_file="$1"
  ffprobe -v quiet -print_format json -show_format "$mkv_file" 2>/dev/null \
    | jq -r '.format.duration // "0"' | awk '{printf "%.1f", $1}'
}

get_embedded_subs() {
  local mkv_file="$1"
  ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null \
    | jq -c '[.streams[] | {index, codec_name, tags: {language: (.tags.language // "und"), title: (.tags.title // "")}}]'
}

# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

score_subtitle() {
  local cues="$1" first="$2" last="$3" duration="$4" mojibake="$5" watermarks="$6"
  local rating="GOOD"

  [[ "$mojibake" -eq 1 ]] && { echo "BAD"; return; }

  if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]]; then
    local cues_per_hour
    cues_per_hour="$(awk "BEGIN { printf \"%.0f\", ($cues / $duration) * 3600 }")"
    if [[ "$cues_per_hour" -lt 200 ]]; then
      echo "BAD"; return
    elif [[ "$cues_per_hour" -gt 1200 ]]; then
      rating="WARN"
    fi
  fi

  if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
    local coverage
    coverage="$(awk "BEGIN { printf \"%.0f\", ($last / $duration) * 100 }")"
    if [[ "$coverage" -lt 50 ]]; then
      echo "BAD"; return
    elif [[ "$coverage" -lt 70 ]]; then
      rating="WARN"
    fi
  fi

  if [[ "$(awk "BEGIN { print ($first > 120) }")" -eq 1 ]]; then
    rating="WARN"
  fi

  [[ "$watermarks" -eq 1 ]] && rating="WARN"

  echo "$rating"
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_audit() {
  log "Auditing subtitles in: $PATH_PREFIX (recursive=$RECURSIVE)"

  local total_files=0 total_tracks=0 good=0 warn=0 bad=0

  while IFS= read -r mkv_file; do
    total_files=$((total_files + 1))
    local basename dir duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    duration="$(get_video_duration "$mkv_file")"

    printf '\n=== %s (%.0fs) ===\n' "$basename" "$duration" >&2
    printf '%-6s %-8s %-7s %-6s %-6s %-5s %-4s %-4s %s\n' \
      "TYPE" "LANG" "CODEC" "CUES" "COVER" "SYNC" "WM" "ENC" "RATING" >&2
    printf '%s\n' "--------------------------------------------------------------" >&2

    # Embedded subtitle tracks
    local embedded_json emb_count
    embedded_json="$(get_embedded_subs "$mkv_file")"
    emb_count="$(jq 'length' <<<"$embedded_json")"

    for ((i=0; i<emb_count; i++)); do
      local stream_idx lang title codec_name
      stream_idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
      lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
      title="$(jq -r ".[$i].tags.title" <<<"$embedded_json")"
      codec_name="$(jq -r ".[$i].codec_name" <<<"$embedded_json")"

      local tmpfile="/tmp/sub_audit_${$}_${stream_idx}.srt"
      if ! ffmpeg -v quiet -i "$mkv_file" -map "0:${stream_idx}" -f srt "$tmpfile" </dev/null 2>/dev/null; then
        rm -f "$tmpfile"
        continue
      fi

      local analysis cues first_sec last_sec mojibake watermarks
      analysis="$(analyze_srt_file "$tmpfile")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
      rm -f "$tmpfile"

      # Check title for watermarks too
      if echo "$title" | grep -qiE "$WATERMARK_PATTERNS" 2>/dev/null; then
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

      printf '%-6s %-8s %-7s %-6s %-6s %-5s %-4s %-4s %s\n' \
        "EMB" "$lang" "$codec_name" "$cues" "$coverage" "$sync_ok" \
        "$([[ "$watermarks" -eq 1 ]] && echo "YES" || echo "--")" \
        "$([[ "$mojibake" -eq 1 ]] && echo "BAD" || echo "OK")" \
        "$rating" >&2

      total_tracks=$((total_tracks + 1))
      case "$rating" in
        GOOD) good=$((good + 1)) ;;
        WARN) warn=$((warn + 1)) ;;
        BAD)  bad=$((bad + 1)) ;;
      esac
    done

    # External subtitle files
    local name_stem="${basename%.*}"
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename ext_lang
      srt_basename="$(basename "$srt_file")"
      ext_lang="$(echo "$srt_basename" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      local analysis cues first_sec last_sec mojibake watermarks
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

      printf '%-6s %-8s %-7s %-6s %-6s %-5s %-4s %-4s %s\n' \
        "EXT" "$ext_lang" "srt" "$cues" "$coverage" "$sync_ok" \
        "$([[ "$watermarks" -eq 1 ]] && echo "YES" || echo "--")" \
        "$([[ "$mojibake" -eq 1 ]] && echo "BAD" || echo "OK")" \
        "$rating" >&2

      total_tracks=$((total_tracks + 1))
      case "$rating" in
        GOOD) good=$((good + 1)) ;;
        WARN) warn=$((warn + 1)) ;;
        BAD)  bad=$((bad + 1)) ;;
      esac
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null | sort)

  done < <(find_media_files "$PATH_PREFIX")

  printf '\n--- Summary: %d files, %d tracks (%d GOOD, %d WARN, %d BAD) ---\n' \
    "$total_files" "$total_tracks" "$good" "$warn" "$bad" >&2
}

cmd_mux() {
  log "Muxing external subtitles in: $PATH_PREFIX (recursive=$RECURSIVE, dry_run=$DRY_RUN, force=$FORCE)"

  local total_files=0 muxed=0 skipped=0 failed=0
  local mux_summary=""

  while IFS= read -r mkv_file; do
    local basename dir name_stem duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    name_stem="${basename%.*}"
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
      local analysis cues first_sec last_sec mojibake watermarks rating
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

    # Build ffmpeg command
    local -a ffmpeg_cmd=(ffmpeg -y -v quiet -i "$mkv_file")
    local -a map_args=(-map 0)
    local existing_sub_count total_stream_count
    existing_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null | jq '.streams | length')"
    total_stream_count="$(ffprobe -v quiet -print_format json -show_streams "$mkv_file" 2>/dev/null | jq '.streams | length')"

    local is_mp4=0
    [[ "${mkv_file##*.}" == "mp4" || "${mkv_file##*.}" == "m4v" ]] && is_mp4=1

    for ((i=0; i<${#srt_files[@]}; i++)); do
      ffmpeg_cmd+=(-i "${srt_files[$i]}")
      map_args+=(-map "$((i + 1)):0")
      local lang="${srt_langs[$i]}"
      if [[ "$is_mp4" -eq 1 ]]; then
        lang="$(lang_to_iso639_2 "$lang")"
        local abs_idx=$((total_stream_count + i))
        map_args+=(-metadata:s:${abs_idx} "language=${lang}")
      else
        local metadata_idx=$((existing_sub_count + i))
        map_args+=(-metadata:s:s:${metadata_idx} "language=${lang}")
      fi
    done

    local ext="${mkv_file##*.}"
    local sub_codec="copy"
    [[ "${ext,,}" == "mp4" || "${ext,,}" == "m4v" ]] && sub_codec="mov_text"
    local tmp_out="${mkv_file%.*}.subtmp.${ext}"
    if ! "${ffmpeg_cmd[@]}" "${map_args[@]}" -c:v copy -c:a copy -c:s "$sub_codec" "$tmp_out" </dev/null 2>/dev/null; then
      log "FAIL mux: $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    if [[ ! -s "$tmp_out" ]]; then
      log "FAIL mux (empty output): $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    # Verify subtitle count
    local new_sub_count expected
    new_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$tmp_out" 2>/dev/null | jq '.streams | length')"
    expected=$((existing_sub_count + ${#srt_files[@]}))

    if [[ "$new_sub_count" -ne "$expected" ]]; then
      log "FAIL mux (sub count mismatch: got $new_sub_count, expected $expected): $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    # Swap original with muxed version
    mv "$tmp_out" "$mkv_file"

    # Delete external SRT files
    for sf in "${srt_files[@]}"; do
      rm -f "$sf"
      log "  Deleted: $(basename "$sf")"
    done

    muxed=$((muxed + ${#srt_files[@]}))
    log "MUXED ${#srt_files[@]} subtitle(s) into: $basename"
    mux_summary="${mux_summary}${basename}: ${#srt_files[@]} sub(s)\n"
    emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"

  done < <(find_media_files "$PATH_PREFIX")

  log "Done. ${muxed} muxed, ${skipped} skipped, ${failed} failed."

  # Bazarr rescan (non-fatal — mux already succeeded)
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    if [[ "$PATH_PREFIX" == *"/tv/"* ]] || [[ "$PATH_PREFIX" == *"/tvanimated/"* ]]; then
      local sonarr_id
      sonarr_id="$(sqlite3 "$BAZARR_DB" "SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(sql_escape "$(basename "$(echo "$PATH_PREFIX" | sed 's|/Season.*||' | sed 's|/$||')")")%' LIMIT 1;" 2>/dev/null || echo "")"
      if [[ -n "$sonarr_id" ]]; then
        bazarr_scan_disk_series "$sonarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr scan-disk failed (non-fatal)"
      fi
    elif [[ "$PATH_PREFIX" == *"/movies/"* ]]; then
      local radarr_id
      radarr_id="$(sqlite3 "$BAZARR_DB" "SELECT radarrId FROM table_movies WHERE path LIKE '%$(sql_escape "$(basename "$PATH_PREFIX")")%' LIMIT 1;" 2>/dev/null || echo "")"
      if [[ -n "$radarr_id" ]]; then
        bazarr_scan_disk_movie "$radarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr scan-disk failed (non-fatal)"
      fi
    fi
  fi

  # Discord notification (non-fatal)
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Mux" \
      "$(printf "Muxed %d subtitle(s) into %d file(s)\n\n%b" "$muxed" "$total_files" "$mux_summary")" \
      3066993 || log "WARN: Discord notification failed (non-fatal)"
  fi
}
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
    local embedded_json emb_count
    embedded_json="$(get_embedded_subs "$mkv_file")"
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

    local ext="${mkv_file##*.}"
    local tmp_out="${mkv_file%.*}.striptmp.${ext}"
    if ! "${ffmpeg_cmd[@]}" "$tmp_out" </dev/null 2>/dev/null; then
      log "FAIL strip: $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    if [[ ! -s "$tmp_out" ]]; then
      log "FAIL strip (empty output): $basename"
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    mv "$tmp_out" "$mkv_file"
    stripped=$((stripped + ${#remove_indices[@]}))
    log "STRIPPED ${#remove_indices[@]} track(s) from: $basename"
    emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"

  done < <(find_media_files "$PATH_PREFIX")

  log "Done. ${stripped} stripped, ${skipped} skipped, ${failed} failed."

  # Discord notification (non-fatal)
  if [[ "$stripped" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Strip" \
      "$(printf "Stripped %d track(s) matching '%s' from %d file(s)" "$stripped" "$TRACK_TARGET" "$total")" \
      15158332 || log "WARN: Discord notification failed (non-fatal)"
  fi
}

cmd_auto_maintain() {
  log "auto-maintain: path=$PATH_PREFIX_ROOT since=$SINCE_MINUTES dry_run=$DRY_RUN"

  local state_db="$STATE_DIR/subtitle_quality_state.db"
  [[ "$SINCE_MINUTES" -eq 0 ]] && init_state_db "$state_db"

  local total_files=0 muxed_files=0 muxed_tracks=0 stripped_files=0 stripped_tracks=0
  local skipped_converter=0 skipped_playback=0 warned=0
  local -a modified_dirs=()

  # Find MKV files across all media dirs
  local -a mkv_files=()
  while IFS= read -r mkv_file; do
    [[ -z "$mkv_file" ]] && continue

    # In quick mode (--since), only process MKVs that have recently modified SRTs
    if [[ "$SINCE_MINUTES" -gt 0 ]]; then
      local stem dir
      stem="$(basename "${mkv_file%.*}")"
      dir="$(dirname "$mkv_file")"
      local recent_srt
      recent_srt="$(find "$dir" -maxdepth 1 -name "${stem}.*.srt" -type f -mmin "-${SINCE_MINUTES}" 2>/dev/null | head -1)"
      [[ -z "$recent_srt" ]] && continue
    fi

    mkv_files+=("$mkv_file")
  done < <(find "$PATH_PREFIX_ROOT" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) 2>/dev/null | sort)

  log "auto-maintain: found ${#mkv_files[@]} candidate files"

  for mkv_file in "${mkv_files[@]}"; do
    local basename dir name_stem duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    name_stem="${basename%.*}"

    total_files=$((total_files + 1))

    # Safety: skip if converter is running on this file
    if is_file_being_converted "$mkv_file"; then
      log "SKIP (converter): $basename"
      skipped_converter=$((skipped_converter + 1))
      continue
    fi

    # Safety: skip if someone is playing this file
    if is_file_being_played "$mkv_file"; then
      log "SKIP (playback): $basename"
      skipped_playback=$((skipped_playback + 1))
      continue
    fi

    # Full mode: skip files that haven't changed since last audit
    if [[ "$SINCE_MINUTES" -eq 0 ]]; then
      local current_mtime stored_mtime
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      stored_mtime="$(sqlite3 "$state_db" "PRAGMA busy_timeout=30000; SELECT mtime FROM file_audits WHERE file_path='$(sql_escape "$mkv_file")';" 2>/dev/null || echo 0)"
      if [[ "$current_mtime" -eq "$stored_mtime" ]] && [[ "$stored_mtime" -gt 0 ]]; then
        debug "SKIP (unchanged): $basename"
        continue
      fi
    fi

    duration="$(get_video_duration "$mkv_file")"
    local file_modified=0

    # --- Phase 1: Audit & mux external SRTs ---
    local -a good_srts=() good_langs=()
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename ext_lang
      srt_basename="$(basename "$srt_file")"
      ext_lang="$(echo "$srt_basename" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      local analysis cues first_sec last_sec mojibake watermarks rating
      analysis="$(analyze_srt_file "$srt_file")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      case "$rating" in
        GOOD)
          good_srts+=("$srt_file")
          good_langs+=("$ext_lang")
          ;;
        WARN)
          warned=$((warned + 1))
          ;;
        BAD)
          debug "SKIP BAD external: $srt_basename"
          ;;
      esac
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null | sort)

    # Mux GOOD external SRTs
    if [[ ${#good_srts[@]} -gt 0 ]]; then
      if [[ "$DRY_RUN" -eq 1 ]]; then
        log "[DRY-RUN] Would mux ${#good_srts[@]} sub(s) into: $basename"
        muxed_tracks=$((muxed_tracks + ${#good_srts[@]}))
        muxed_files=$((muxed_files + 1))
      else
        local -a ffmpeg_cmd=(ffmpeg -y -v quiet -i "$mkv_file")
        local -a map_args=(-map 0)
        local existing_sub_count total_stream_count
        existing_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null | jq '.streams | length')"
        total_stream_count="$(ffprobe -v quiet -print_format json -show_streams "$mkv_file" 2>/dev/null | jq '.streams | length')"

        local is_mp4=0
        [[ "${mkv_file##*.}" == "mp4" || "${mkv_file##*.}" == "m4v" ]] && is_mp4=1

        for ((i=0; i<${#good_srts[@]}; i++)); do
          ffmpeg_cmd+=(-i "${good_srts[$i]}")
          map_args+=(-map "$((i + 1)):0")
          local lang="${good_langs[$i]}"
          if [[ "$is_mp4" -eq 1 ]]; then
            lang="$(lang_to_iso639_2 "$lang")"
            local abs_idx=$((total_stream_count + i))
            map_args+=(-metadata:s:${abs_idx} "language=${lang}")
          else
            local metadata_idx=$((existing_sub_count + i))
            map_args+=(-metadata:s:s:${metadata_idx} "language=${lang}")
          fi
        done

        local ext="${mkv_file##*.}"
        local sub_codec="copy"
        [[ "${ext,,}" == "mp4" || "${ext,,}" == "m4v" ]] && sub_codec="mov_text"
        local tmp_out="${mkv_file%.*}.subtmp.${ext}"
        if "${ffmpeg_cmd[@]}" "${map_args[@]}" -c:v copy -c:a copy -c:s "$sub_codec" "$tmp_out" </dev/null 2>/dev/null; then
          local new_sub_count expected
          new_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$tmp_out" 2>/dev/null | jq '.streams | length')"
          expected=$((existing_sub_count + ${#good_srts[@]}))

          if [[ "$new_sub_count" -eq "$expected" ]]; then
            mv "$tmp_out" "$mkv_file"
            for sf in "${good_srts[@]}"; do rm -f "$sf"; done
            muxed_tracks=$((muxed_tracks + ${#good_srts[@]}))
            muxed_files=$((muxed_files + 1))
            file_modified=1
            log "MUXED ${#good_srts[@]} sub(s) into: $basename"
          else
            log "FAIL mux (count mismatch got=$new_sub_count expect=$expected): $basename"
            rm -f "$tmp_out"
          fi
        else
          log "FAIL mux: $basename"
          rm -f "$tmp_out"
        fi
      fi
    fi

    # --- Phase 2: Auto-strip BAD embedded tracks ---
    # Quick mode: only strip if we just muxed a GOOD replacement this run
    # Full mode: always run Phase 2 (full audit)
    if [[ "$SINCE_MINUTES" -gt 0 ]] && [[ ${#good_langs[@]} -eq 0 ]]; then
      debug "SKIP Phase 2 (quick mode, no mux this run): $basename"
    else
      local embedded_json emb_count
      embedded_json="$(get_embedded_subs "$mkv_file")"
      emb_count="$(jq 'length' <<<"$embedded_json")"

      if [[ "$emb_count" -gt 1 ]]; then
        # First pass: score ALL embedded tracks and cache results
        local -a emb_ratings=() emb_norm_langs=() emb_stream_ids=()
        for ((i=0; i<emb_count; i++)); do
          local stream_idx lang title
          stream_idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
          lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
          title="$(jq -r ".[$i].tags.title" <<<"$embedded_json")"

          emb_stream_ids+=("$stream_idx")
          local norm_lang="$lang"
          [[ "$norm_lang" == "eng" ]] && norm_lang="en"
          [[ "$norm_lang" == "spa" ]] && norm_lang="es"
          emb_norm_langs+=("$norm_lang")

          # Extract to temp file for scoring
          local tmpfile="/tmp/sub_auto_${$}_${stream_idx}.srt"
          if ! ffmpeg -v quiet -i "$mkv_file" -map "0:${stream_idx}" -f srt "$tmpfile" </dev/null 2>/dev/null; then
            rm -f "$tmpfile"
            emb_ratings+=("UNKNOWN")
            continue
          fi

          local analysis cues first_sec last_sec mojibake watermarks emb_rating
          analysis="$(analyze_srt_file "$tmpfile")"
          read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
          if echo "$title" | grep -qiE "$WATERMARK_PATTERNS" 2>/dev/null; then
            watermarks=1
          fi
          emb_rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"
          rm -f "$tmpfile"
          emb_ratings+=("$emb_rating")

          [[ "$emb_rating" == "WARN" ]] && warned=$((warned + 1))
        done

        # Second pass: identify BAD tracks with a GOOD replacement
        local -a strip_indices=()
        for ((i=0; i<emb_count; i++)); do
          [[ "${emb_ratings[$i]}" != "BAD" ]] && continue

          local norm_lang="${emb_norm_langs[$i]}"
          local has_good=0

          # Check 1: Was a GOOD SRT just muxed in this language? (good_langs from Phase 1)
          for gl in "${good_langs[@]+"${good_langs[@]}"}"; do
            if [[ "$gl" == "$norm_lang" ]]; then
              has_good=1
              break
            fi
          done

          # Check 2: Is another embedded track in the same language rated GOOD?
          if [[ "$has_good" -eq 0 ]]; then
            for ((j=0; j<emb_count; j++)); do
              [[ "$j" -eq "$i" ]] && continue
              if [[ "${emb_norm_langs[$j]}" == "$norm_lang" ]] && [[ "${emb_ratings[$j]}" == "GOOD" ]]; then
                has_good=1
                break
              fi
            done
          fi

          if [[ "$has_good" -eq 1 ]]; then
            strip_indices+=("${emb_stream_ids[$i]}")
            log "AUTO-STRIP BAD embedded idx=${emb_stream_ids[$i]} lang=${norm_lang}: $basename"
          fi
        done

        if [[ ${#strip_indices[@]} -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
          local -a strip_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
          for idx in "${strip_indices[@]}"; do
            strip_cmd+=(-map "-0:${idx}")
          done
          strip_cmd+=(-c copy)
          local ext="${mkv_file##*.}"
          local strip_tmp="${mkv_file%.*}.striptmp.${ext}"
          if "${strip_cmd[@]}" "$strip_tmp" </dev/null 2>/dev/null && [[ -s "$strip_tmp" ]]; then
            mv "$strip_tmp" "$mkv_file"
            stripped_tracks=$((stripped_tracks + ${#strip_indices[@]}))
            stripped_files=$((stripped_files + 1))
            file_modified=1
            log "STRIPPED ${#strip_indices[@]} BAD track(s) from: $basename"
          else
            rm -f "$strip_tmp"
            log "FAIL strip: $basename"
          fi
        elif [[ ${#strip_indices[@]} -gt 0 ]]; then
          log "[DRY-RUN] Would strip ${#strip_indices[@]} BAD track(s) from: $basename"
          stripped_tracks=$((stripped_tracks + ${#strip_indices[@]}))
          stripped_files=$((stripped_files + 1))
        fi
      fi
    fi

    # --- Phase 3: Emby refresh per modified file ---
    if [[ "$DRY_RUN" -eq 0 ]] && [[ "$file_modified" -eq 1 ]]; then
      emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"
      # Track modified dirs for Bazarr rescan
      local show_dir
      if [[ "$mkv_file" == *"/tv/"* ]] || [[ "$mkv_file" == *"/tvanimated/"* ]]; then
        show_dir="$(echo "$mkv_file" | sed 's|/Season.*||' | sed 's|/$||')"
      else
        show_dir="$(dirname "$mkv_file")"
      fi
      modified_dirs+=("$show_dir")
    fi

    # Update state DB (full mode only)
    if [[ "$SINCE_MINUTES" -eq 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
      local current_mtime action_val="none"
      [[ "$muxed_files" -gt 0 ]] && [[ "$file_modified" -eq 1 ]] && action_val="muxed"
      [[ "$stripped_files" -gt 0 ]] && [[ "$file_modified" -eq 1 ]] && action_val="stripped"
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      sqlite3 "$state_db" "PRAGMA busy_timeout=30000; INSERT OR REPLACE INTO file_audits (file_path, mtime, last_audit_ts, action_taken) VALUES ('$(sql_escape "$mkv_file")', $current_mtime, $(date +%s), '$action_val');" 2>/dev/null || true
    fi
  done

  # Bazarr rescan (batch by series/movie, deduplicated, non-fatal)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ ${#modified_dirs[@]} -gt 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    local -A rescanned=()
    for show_dir in "${modified_dirs[@]}"; do
      [[ -n "${rescanned[$show_dir]:-}" ]] && continue
      rescanned["$show_dir"]=1

      if [[ "$show_dir" == *"/tv/"* ]] || [[ "$show_dir" == *"/tvanimated/"* ]]; then
        local sonarr_id
        sonarr_id="$(sqlite3 "$BAZARR_DB" "SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(sql_escape "$(basename "$show_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
        [[ -n "$sonarr_id" ]] && { bazarr_scan_disk_series "$sonarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed"; }
      elif [[ "$show_dir" == *"/movies/"* ]] || [[ "$show_dir" == *"/moviesanimated/"* ]]; then
        local radarr_id
        radarr_id="$(sqlite3 "$BAZARR_DB" "SELECT radarrId FROM table_movies WHERE path LIKE '%$(sql_escape "$(basename "$show_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
        [[ -n "$radarr_id" ]] && { bazarr_scan_disk_movie "$radarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed"; }
      fi
    done
  fi

  log "auto-maintain done: files=$total_files muxed=$muxed_files($muxed_tracks tracks) stripped=$stripped_files($stripped_tracks tracks) warned=$warned skipped_converter=$skipped_converter skipped_playback=$skipped_playback"

  # Discord notification (non-fatal, only when actions taken)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ $((muxed_files + stripped_files)) -gt 0 ]]; then
    local mode="quick"
    [[ "$SINCE_MINUTES" -eq 0 ]] && mode="full"
    notify_discord_embed "Subtitle Auto-Maintain ($mode)" \
      "$(printf "Muxed: %d file(s), %d track(s)\nStripped: %d file(s), %d track(s)\nWARN (manual review): %d\nSkipped (converter): %d\nSkipped (playback): %d" \
        "$muxed_files" "$muxed_tracks" "$stripped_files" "$stripped_tracks" "$warned" "$skipped_converter" "$skipped_playback")" \
      3066993 || log "WARN: Discord notification failed (non-fatal)"
  fi
}

case "$COMMAND" in
  audit)        cmd_audit ;;
  mux)          cmd_mux ;;
  strip)        cmd_strip ;;
  auto-maintain) cmd_auto_maintain ;;
esac
