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
# Subcommand placeholders (implemented in subsequent tasks)
# ---------------------------------------------------------------------------
cmd_audit() { log "audit not yet implemented"; }
cmd_mux() { log "mux not yet implemented"; }
cmd_strip() { log "strip not yet implemented"; }

case "$COMMAND" in
  audit) cmd_audit ;;
  mux)   cmd_mux ;;
  strip) cmd_strip ;;
esac
