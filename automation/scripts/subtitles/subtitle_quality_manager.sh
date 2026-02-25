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

# Override lib's log() with our own prefix
log() {
  printf '%s [sub-quality] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

debug() {
  [[ "$LOG_LEVEL" == "debug" ]] && log "DEBUG $*"
  return 0
}

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
    local name_stem="${basename%.mkv}"
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

  done < <(find_mkv_files "$PATH_PREFIX")

  printf '\n--- Summary: %d files, %d tracks (%d GOOD, %d WARN, %d BAD) ---\n' \
    "$total_files" "$total_tracks" "$good" "$warn" "$bad" >&2
}

cmd_mux() { log "mux not yet implemented"; }
cmd_strip() { log "strip not yet implemented"; }

case "$COMMAND" in
  audit) cmd_audit ;;
  mux)   cmd_mux ;;
  strip) cmd_strip ;;
esac
