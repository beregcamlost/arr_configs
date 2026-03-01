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
KEEP_ONLY=""
KEEP_PROFILE_LANGS=0
BLOAT_THRESHOLD=6
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
  --keep-only LANGS     Comma-separated language codes to KEEP (removes everything else)
                        Accepts both 2-letter (en,es) and 3-letter (eng,spa) codes

Auto-maintain options:
  --path-prefix DIR     Root media directory to scan recursively (required)
  --since N             Only scan files with SRTs modified in last N minutes (quick mode)
  --keep-profile-langs  Auto-resolve Bazarr language profile per file, strip non-profile tracks
  --bloat-threshold N   Min embedded sub tracks to trigger profile cleanup (default: 6)
  --emby-url URL        Emby server URL (default: from EMBY_URL env)
  --emby-api-key KEY    Emby API key (default: from EMBY_API_KEY env)

Examples:
  subtitle_quality_manager.sh audit --path "/media/tv/Evil" --recursive
  subtitle_quality_manager.sh mux --path "/media/tv/Evil/Season 1" --dry-run
  subtitle_quality_manager.sh strip --path "/media/tv/Evil" --track eng --recursive --dry-run
  subtitle_quality_manager.sh strip --path "/media/tv/Show" --keep-only eng,spa --recursive
  subtitle_quality_manager.sh auto-maintain --path-prefix /media --since 15 --dry-run
  subtitle_quality_manager.sh auto-maintain --path-prefix /media --keep-profile-langs
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
    --keep-only)  KEEP_ONLY="${2:-}"; shift 2 ;;
    --keep-profile-langs) KEEP_PROFILE_LANGS=1; shift ;;
    --bloat-threshold) BLOAT_THRESHOLD="${2:-6}"; shift 2 ;;
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

if [[ "$COMMAND" == "strip" ]] && [[ -z "$TRACK_TARGET" ]] && [[ -z "$KEEP_ONLY" ]]; then
  echo "--track or --keep-only is required for strip command." >&2; exit 1
fi

if [[ "$COMMAND" == "strip" ]] && [[ -n "$TRACK_TARGET" ]] && [[ -n "$KEEP_ONLY" ]]; then
  echo "--track and --keep-only are mutually exclusive." >&2; exit 1
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
  running="$(sqlite3 -cmd ".timeout 5000" "$state_db" \
    "SELECT COUNT(*) FROM conversion_runs cr
     JOIN media_files m ON m.id = cr.media_id
     WHERE cr.status = 'running' AND cr.end_ts IS NULL
       AND m.path = '$(sql_escape "$filepath")';" </dev/null 2>/dev/null || echo 0)"
  [[ "${running:-0}" -gt 0 ]]
}

STREAMING_STATE_DB="/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db"

# Pre-loaded streaming candidate paths (populated by load_streaming_candidates)
declare -A _STREAMING_PATHS=()

load_streaming_candidates() {
  _STREAMING_PATHS=()
  [[ -f "$STREAMING_STATE_DB" ]] || return 0
  while IFS= read -r spath; do
    [[ -n "$spath" ]] && _STREAMING_PATHS["$spath"]=1
  done < <(sqlite3 -cmd ".timeout 5000" "$STREAMING_STATE_DB" \
    "SELECT path FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL;" 2>/dev/null)
}

# Pure bash lookup — safe inside pipelines (no subprocess spawning)
is_streaming_candidate() {
  local filepath="$1"
  [[ ${#_STREAMING_PATHS[@]} -eq 0 ]] && return 1
  local match_dir
  if is_tv_path "$filepath"; then
    if [[ "$filepath" == *"/Season "* ]]; then
      match_dir="${filepath%%/Season [0-9]*}"
    else
      match_dir="$(dirname "$filepath")"
    fi
  else
    match_dir="$(dirname "$filepath")"
  fi
  [[ -n "${_STREAMING_PATHS[$match_dir]:-}" ]]
}

# ---------------------------------------------------------------------------
# Language/path helpers — provided by lib_subtitle_common.sh:
#   lang_to_iso639_2(), expand_lang_codes(), lang_in_set(),
#   get_audio_languages(), resolve_bazarr_profile_langs(),
#   is_tv_path(), is_movie_path(), bazarr_rescan_for_file()
# ---------------------------------------------------------------------------

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
    | jq -c '[.streams[] | {index, codec_name, tags: {language: (.tags.language // "und"), title: (.tags.title // "")}, forced: (.disposition.forced // 0)}]'
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
  load_streaming_candidates
  log "Muxing external subtitles in: $PATH_PREFIX (recursive=$RECURSIVE, dry_run=$DRY_RUN, force=$FORCE)"

  local total_files=0 muxed=0 skipped=0 skipped_streaming=0 failed=0
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

    # Skip streaming candidates
    if is_streaming_candidate "$mkv_file"; then
      log "SKIP (streaming): $basename"
      skipped_streaming=$((skipped_streaming + 1))
      continue
    fi

    # Build embedded language map for collision detection
    local emb_json_mux emb_count_mux
    emb_json_mux="$(get_embedded_subs "$mkv_file")"
    emb_count_mux="$(jq 'length' <<<"$emb_json_mux")"
    declare -A embedded_lang_idx=()
    for ((ei=0; ei<emb_count_mux; ei++)); do
      local ei_lang ei_idx ei_norm
      ei_idx="$(jq -r ".[$ei].index" <<<"$emb_json_mux")"
      ei_lang="$(jq -r ".[$ei].tags.language" <<<"$emb_json_mux")"
      ei_norm="$(normalize_track_lang "$ei_lang")"
      # Keep first (oldest) track index per normalized language for collision detection
      [[ -z "${embedded_lang_idx[$ei_norm]:-}" ]] && embedded_lang_idx["$ei_norm"]="$ei_idx"
    done
    local -a premux_strip_indices=()

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

      # Pre-mux collision check: if embedded track exists for this language, mark for stripping
      local srt_norm
      srt_norm="$(normalize_track_lang "$ext_lang")"
      if [[ -n "${embedded_lang_idx[$srt_norm]:-}" ]]; then
        local collide_idx="${embedded_lang_idx[$srt_norm]}"
        premux_strip_indices+=("$collide_idx")
        log "COLLISION lang=$srt_norm: will replace embedded idx=$collide_idx with better external SRT: $srt_basename"
        unset "embedded_lang_idx[$srt_norm]"
      fi

      srt_files+=("$srt_file")
      srt_langs+=("$ext_lang")
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null | sort)

    [[ ${#srt_files[@]} -eq 0 ]] && continue

    total_files=$((total_files + 1))

    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "[DRY-RUN] Would mux ${#srt_files[@]} subtitle(s) into: $basename"
      for sf in "${srt_files[@]}"; do log "  + $(basename "$sf")"; done
      if [[ ${#premux_strip_indices[@]} -gt 0 ]]; then
        log "[DRY-RUN] Would strip ${#premux_strip_indices[@]} superseded embedded track(s) from: $basename"
      fi
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

    # Post-mux: strip superseded embedded tracks that collided with external SRTs
    if [[ ${#premux_strip_indices[@]} -gt 0 ]]; then
      local -a strip_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
      for idx in "${premux_strip_indices[@]}"; do
        strip_cmd+=(-map "-0:${idx}")
      done
      strip_cmd+=(-c copy)
      local strip_ext="${mkv_file##*.}"
      local strip_tmp="${mkv_file%.*}.collisiontmp.${strip_ext}"
      if "${strip_cmd[@]}" "$strip_tmp" </dev/null 2>/dev/null && [[ -s "$strip_tmp" ]]; then
        mv "$strip_tmp" "$mkv_file"
        log "STRIPPED ${#premux_strip_indices[@]} superseded embedded track(s) from: $basename"
      else
        rm -f "$strip_tmp"
        log "WARN: post-mux strip failed (non-fatal): $basename"
      fi
    fi

    mux_summary="${mux_summary}${basename}: ${#srt_files[@]} sub(s)\n"
    emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"

  done < <(find_media_files "$PATH_PREFIX")

  log "Done. ${muxed} muxed, ${skipped} skipped, ${skipped_streaming} skipped(streaming), ${failed} failed."

  # Bazarr rescan (non-fatal — mux already succeeded)
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    bazarr_rescan_for_file "$PATH_PREFIX" "$BAZARR_DB" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr scan-disk failed (non-fatal)"
  fi

  # Discord notification (non-fatal)
  if [[ "$muxed" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Mux" \
      "$(printf "Muxed %d subtitle(s) into %d file(s)\n\n%b" "$muxed" "$total_files" "$mux_summary")" \
      3066993 || log "WARN: Discord notification failed (non-fatal)"
  fi
}
cmd_strip() {
  load_streaming_candidates
  local mode_label
  if [[ -n "$KEEP_ONLY" ]]; then
    mode_label="keep-only=$KEEP_ONLY"
  else
    mode_label="track=$TRACK_TARGET"
  fi
  log "Stripping ($mode_label) from: $PATH_PREFIX (recursive=$RECURSIVE, dry_run=$DRY_RUN)"

  local total=0 stripped=0 skipped=0 skipped_streaming=0 failed=0

  # Pre-expand keep-only languages once
  local keep_set=""
  [[ -n "$KEEP_ONLY" ]] && keep_set="$(expand_lang_codes "$KEEP_ONLY")"

  while IFS= read -r mkv_file; do
    local basename
    basename="$(basename "$mkv_file")"

    # Check converter conflict
    if is_file_being_converted "$mkv_file"; then
      log "SKIP (converter running): $basename"
      skipped=$((skipped + 1))
      continue
    fi

    # Skip streaming candidates
    if is_streaming_candidate "$mkv_file"; then
      log "SKIP (streaming): $basename"
      skipped_streaming=$((skipped_streaming + 1))
      continue
    fi

    # Get embedded subtitle streams
    local embedded_json emb_count
    embedded_json="$(get_embedded_subs "$mkv_file")"
    emb_count="$(jq 'length' <<<"$embedded_json")"
    [[ "$emb_count" -eq 0 ]] && continue

    # Find matching stream indices to remove
    local -a remove_indices=()

    if [[ -n "$KEEP_ONLY" ]]; then
      # keep-only mode: remove tracks whose language is NOT in the keep set
      for ((i=0; i<emb_count; i++)); do
        local idx lang
        idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
        lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
        if ! lang_in_set "$lang" "$keep_set"; then
          remove_indices+=("$idx")
        fi
      done
    elif [[ "$TRACK_TARGET" =~ ^[0-9]+$ ]]; then
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

  log "Done. ${stripped} stripped, ${skipped} skipped, ${skipped_streaming} skipped(streaming), ${failed} failed."

  # Discord notification (non-fatal)
  if [[ "$stripped" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    notify_discord_embed "Subtitle Quality Manager — Strip" \
      "$(printf "Stripped %d track(s) (%s) from %d file(s)" "$stripped" "$mode_label" "$total")" \
      15158332 || log "WARN: Discord notification failed (non-fatal)"
  fi
}

cmd_auto_maintain() {
  load_streaming_candidates
  log "auto-maintain: path=$PATH_PREFIX_ROOT since=$SINCE_MINUTES keep_profile_langs=$KEEP_PROFILE_LANGS bloat_threshold=$BLOAT_THRESHOLD dry_run=$DRY_RUN"

  local state_db="$STATE_DIR/subtitle_quality_state.db"
  [[ "$SINCE_MINUTES" -eq 0 ]] && init_state_db "$state_db"

  local total_files=0 muxed_files=0 muxed_tracks=0 stripped_files=0 stripped_tracks=0
  local skipped_converter=0 skipped_playback=0 skipped_streaming=0 warned=0 deepl_deferred=0 cleaned_nonprofile=0 extracted_nonprofile=0
  local -a modified_dirs=()
  local -A bazarr_rescanned=()

  # Find MKV files across all media dirs
  local -a mkv_files=()
  while IFS= read -r mkv_file; do
    [[ -z "$mkv_file" ]] && continue

    # In quick mode (--since), only process MKVs that were recently modified
    # themselves (new import) OR have recently modified SRTs
    if [[ "$SINCE_MINUTES" -gt 0 ]]; then
      local stem dir mkv_age_ok=0
      stem="$(basename "${mkv_file%.*}")"
      dir="$(dirname "$mkv_file")"
      # Check if the MKV itself was recently modified (new import)
      if [[ -n "$(find "$mkv_file" -maxdepth 0 -mmin "-${SINCE_MINUTES}" 2>/dev/null)" ]]; then
        mkv_age_ok=1
      fi
      # Check if any SRT was recently modified
      local recent_srt
      recent_srt="$(find "$dir" -maxdepth 1 -name "${stem}.*.srt" -type f -mmin "-${SINCE_MINUTES}" 2>/dev/null | head -1)"
      [[ "$mkv_age_ok" -eq 0 && -z "$recent_srt" ]] && continue
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

    # Skip files that are streaming candidates (about to be deleted)
    if is_streaming_candidate "$mkv_file"; then
      log "SKIP (streaming): $basename"
      skipped_streaming=$((skipped_streaming + 1))
      continue
    fi

    # Full mode: skip files that haven't changed since last audit
    if [[ "$SINCE_MINUTES" -eq 0 ]]; then
      local current_mtime stored_mtime
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      stored_mtime="$(sqlite3 "$state_db" "PRAGMA busy_timeout=30000; SELECT mtime FROM file_audits WHERE file_path='$(sql_escape "$mkv_file")';" 2>/dev/null | tail -1)" || stored_mtime=0
      [[ -z "$stored_mtime" ]] && stored_mtime=0
      if [[ "$current_mtime" -eq "$stored_mtime" ]] && [[ "$stored_mtime" -gt 0 ]]; then
        debug "SKIP (unchanged): $basename"
        continue
      fi
    fi

    duration="$(get_video_duration "$mkv_file")"
    local file_modified=0

    # Resolve Bazarr profile once per file — used by Phase 0 (debloat), Phase 1 (mux filter), Phase 1.5 (cleanup)
    local am_profile_langs="" am_profile_set=""
    am_profile_langs="$(resolve_bazarr_profile_langs "$mkv_file" "$BAZARR_DB")" || am_profile_langs=""
    [[ -n "$am_profile_langs" ]] && am_profile_set="$(expand_lang_codes "$am_profile_langs")"

    # --- Phase 0: Extract + strip non-profile embedded tracks ---
    if [[ "$KEEP_PROFILE_LANGS" -eq 1 ]]; then
      local emb_json_p0 emb_count_p0
      emb_json_p0="$(get_embedded_subs "$mkv_file")"
      emb_count_p0="$(jq 'length' <<<"$emb_json_p0")"

      if [[ -n "$am_profile_set" ]] && [[ "$emb_count_p0" -gt 0 ]]; then
        local -a p0_strip_indices=()
        for ((i=0; i<emb_count_p0; i++)); do
          local p0_idx p0_lang p0_codec p0_forced
          p0_idx="$(jq -r ".[$i].index" <<<"$emb_json_p0")"
          p0_lang="$(jq -r ".[$i].tags.language" <<<"$emb_json_p0")"
          p0_codec="$(jq -r ".[$i].codec_name" <<<"$emb_json_p0")"
          p0_forced="$(jq -r ".[$i].forced" <<<"$emb_json_p0")"

          # Skip tracks that belong to the profile
          lang_in_set "$p0_lang" "$am_profile_set" && continue

          local p0_norm_lang
          p0_norm_lang="$(normalize_track_lang "$p0_lang")"

          # Build output filename: {stem}.{lang}[.forced].srt
          local p0_out_name="${name_stem}.${p0_norm_lang}"
          [[ "$p0_forced" -eq 1 ]] && p0_out_name+=".forced"
          p0_out_name+=".srt"
          local p0_out="${dir}/${p0_out_name}"

          # Extract text-based subs to external SRT (bitmap codecs can't be extracted)
          if is_text_sub_codec "$p0_codec"; then
            if [[ ! -f "$p0_out" ]]; then
              if [[ "$DRY_RUN" -eq 0 ]]; then
                if ffmpeg -v quiet -i "$mkv_file" -map "0:${p0_idx}" -f srt "$p0_out" </dev/null 2>/dev/null && [[ -s "$p0_out" ]]; then
                  log "EXTRACTED non-profile idx=${p0_idx} lang=${p0_norm_lang} → ${p0_out_name}: $basename"
                  extracted_nonprofile=$((extracted_nonprofile + 1))
                else
                  rm -f "$p0_out"
                  log "WARN: extraction failed idx=${p0_idx} lang=${p0_norm_lang} (non-fatal): $basename"
                fi
              else
                log "[DRY-RUN] Would extract non-profile idx=${p0_idx} lang=${p0_norm_lang} → ${p0_out_name}: $basename"
                extracted_nonprofile=$((extracted_nonprofile + 1))
              fi
            else
              debug "SKIP extract (external exists): ${p0_out_name}"
            fi
          else
            debug "SKIP extract (bitmap codec=${p0_codec}): idx=${p0_idx} lang=${p0_norm_lang}: $basename"
          fi

          # Mark for stripping regardless of codec type
          p0_strip_indices+=("$p0_idx")
        done

        if [[ ${#p0_strip_indices[@]} -gt 0 ]]; then
          if [[ "$DRY_RUN" -eq 1 ]]; then
            log "[DRY-RUN] Would strip ${#p0_strip_indices[@]} non-profile track(s) from: $basename (profile: $am_profile_langs)"
            stripped_tracks=$((stripped_tracks + ${#p0_strip_indices[@]}))
            stripped_files=$((stripped_files + 1))
          else
            local -a p0_strip_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
            for idx in "${p0_strip_indices[@]}"; do
              p0_strip_cmd+=(-map "-0:${idx}")
            done
            p0_strip_cmd+=(-c copy)
            local ext_p0="${mkv_file##*.}"
            local p0_strip_tmp="${mkv_file%.*}.bloattmp.${ext_p0}"
            if "${p0_strip_cmd[@]}" "$p0_strip_tmp" </dev/null 2>/dev/null && [[ -s "$p0_strip_tmp" ]]; then
              mv "$p0_strip_tmp" "$mkv_file"
              stripped_tracks=$((stripped_tracks + ${#p0_strip_indices[@]}))
              stripped_files=$((stripped_files + 1))
              file_modified=1
              log "STRIPPED ${#p0_strip_indices[@]} non-profile track(s) from: $basename (profile: $am_profile_langs)"
            else
              rm -f "$p0_strip_tmp"
              log "FAIL strip non-profile: $basename"
            fi
          fi
        fi
      elif [[ -z "$am_profile_set" ]]; then
        debug "SKIP Phase 0 (no Bazarr profile): $basename"
      fi
    fi

    # --- Phase 1: Audit & mux external SRTs ---
    # Build embedded language map for collision detection
    local emb_json_p1 emb_count_p1
    emb_json_p1="$(get_embedded_subs "$mkv_file")"
    emb_count_p1="$(jq 'length' <<<"$emb_json_p1")"
    declare -A embedded_lang_idx_p1=()
    for ((ei=0; ei<emb_count_p1; ei++)); do
      local ei_lang ei_idx ei_norm
      ei_idx="$(jq -r ".[$ei].index" <<<"$emb_json_p1")"
      ei_lang="$(jq -r ".[$ei].tags.language" <<<"$emb_json_p1")"
      ei_norm="$(normalize_track_lang "$ei_lang")"
      [[ -z "${embedded_lang_idx_p1[$ei_norm]:-}" ]] && embedded_lang_idx_p1["$ei_norm"]="$ei_idx"
    done
    local -a premux_strip_indices_p1=()

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
          # Check for .deepl marker (DeepL-translated SRT grace period)
          if [[ -f "${srt_file}.deepl" ]]; then
            local marker_mtime srt_mtime
            marker_mtime="$(stat -c %Y "${srt_file}.deepl" 2>/dev/null || echo 0)"
            srt_mtime="$(stat -c %Y "$srt_file" 2>/dev/null || echo 0)"
            # If SRT was replaced after marker created, human sub found — delete marker and mux
            if [[ "$srt_mtime" -gt "$marker_mtime" ]]; then
              rm -f "${srt_file}.deepl"
              log "DeepL marker removed (SRT replaced by human sub): $srt_basename"
            else
              # Grace period: 7 days (604800 seconds)
              local now marker_age
              now="$(date +%s)"
              marker_age=$(( now - marker_mtime ))
              if [[ "$marker_age" -lt 604800 ]]; then
                local days_left=$(( (604800 - marker_age) / 86400 ))
                debug "SKIP mux (DeepL grace ${days_left}d remaining): $srt_basename"
                deepl_deferred=$((deepl_deferred + 1))
                continue
              else
                # Grace expired — mux the DeepL translation
                rm -f "${srt_file}.deepl"
                log "DeepL grace expired, muxing: $srt_basename"
              fi
            fi
          fi
          # Profile filter: skip non-profile languages (they're kept as DeepL sources, not muxed)
          local srt_norm_p1
          srt_norm_p1="$(normalize_track_lang "$ext_lang")"
          if [[ -n "$am_profile_set" ]] && ! lang_in_set "$srt_norm_p1" "$am_profile_set"; then
            debug "SKIP non-profile external SRT: $srt_basename (lang=$srt_norm_p1, profile=$am_profile_langs)"
            continue
          fi

          # Pre-mux collision check
          if [[ -n "${embedded_lang_idx_p1[$srt_norm_p1]:-}" ]]; then
            local collide_idx_p1="${embedded_lang_idx_p1[$srt_norm_p1]}"
            premux_strip_indices_p1+=("$collide_idx_p1")
            log "COLLISION lang=$srt_norm_p1: will replace embedded idx=$collide_idx_p1 with external SRT: $srt_basename"
            unset "embedded_lang_idx_p1[$srt_norm_p1]"
          fi

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
        if [[ ${#premux_strip_indices_p1[@]} -gt 0 ]]; then
          log "[DRY-RUN] Would strip ${#premux_strip_indices_p1[@]} superseded embedded track(s) from: $basename"
        fi
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

            # Post-mux: strip superseded embedded tracks
            if [[ ${#premux_strip_indices_p1[@]} -gt 0 ]]; then
              local -a p1_strip_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
              for idx in "${premux_strip_indices_p1[@]}"; do
                p1_strip_cmd+=(-map "-0:${idx}")
              done
              p1_strip_cmd+=(-c copy)
              local p1_strip_ext="${mkv_file##*.}"
              local p1_strip_tmp="${mkv_file%.*}.collisiontmp.${p1_strip_ext}"
              if "${p1_strip_cmd[@]}" "$p1_strip_tmp" </dev/null 2>/dev/null && [[ -s "$p1_strip_tmp" ]]; then
                mv "$p1_strip_tmp" "$mkv_file"
                stripped_tracks=$((stripped_tracks + ${#premux_strip_indices_p1[@]}))
                stripped_files=$((stripped_files + 1))
                log "STRIPPED ${#premux_strip_indices_p1[@]} superseded embedded track(s) from: $basename"
              else
                rm -f "$p1_strip_tmp"
                log "WARN: post-mux collision strip failed (non-fatal): $basename"
              fi
            fi
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

    # --- Phase 1.5: Clean up non-profile external SRTs ---
    # Once all profile languages are satisfied (embedded or external), non-profile
    # SRTs are no longer needed as DeepL translation sources — safe to remove.
    if [[ -n "$am_profile_set" ]]; then
      # Check if ALL profile languages have subtitles (embedded or external)
      # Re-read embedded after potential mux
      local emb_json_p15 emb_count_p15
      emb_json_p15="$(get_embedded_subs "$mkv_file")"
      emb_count_p15="$(jq 'length' <<<"$emb_json_p15")"

      declare -A emb_langs_p15=()
      for ((ei=0; ei<emb_count_p15; ei++)); do
        local el
        el="$(jq -r ".[$ei].tags.language" <<<"$emb_json_p15")"
        el="$(normalize_track_lang "$el")"
        emb_langs_p15["$el"]=1
      done

      # Build set of remaining external SRT languages
      declare -A ext_langs_p15=()
      while IFS= read -r remaining_srt; do
        [[ -z "$remaining_srt" ]] && continue
        local rb el_p15
        rb="$(basename "$remaining_srt")"
        el_p15="$(echo "$rb" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
        [[ -z "$el_p15" ]] && el_p15="und"
        el_p15="$(normalize_track_lang "$el_p15")"
        ext_langs_p15["$el_p15"]=1
      done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)

      # Check each profile language
      local all_profile_satisfied=1
      IFS=',' read -ra _profile_codes <<< "$am_profile_langs"
      for pc in "${_profile_codes[@]}"; do
        local pc_norm
        pc_norm="$(normalize_track_lang "$pc")"
        if [[ -z "${emb_langs_p15[$pc_norm]:-}" ]] && [[ -z "${ext_langs_p15[$pc_norm]:-}" ]]; then
          all_profile_satisfied=0
          break
        fi
      done

      if [[ "$all_profile_satisfied" -eq 1 ]]; then
        # Delete non-profile external SRTs
        while IFS= read -r remaining_srt; do
          [[ -z "$remaining_srt" ]] && continue
          local rb el_p15
          rb="$(basename "$remaining_srt")"
          el_p15="$(echo "$rb" | sed "s/^${name_stem}\.//" | sed 's/\.srt$//' | sed 's/\.forced$//' | sed 's/\.hi$//')"
          [[ -z "$el_p15" ]] && el_p15="und"
          local el_norm_p15
          el_norm_p15="$(normalize_track_lang "$el_p15")"
          if ! lang_in_set "$el_norm_p15" "$am_profile_set"; then
            if [[ "$DRY_RUN" -eq 0 ]]; then
              rm -f "$remaining_srt" "${remaining_srt}.deepl"
            fi
            log "CLEANUP non-profile external SRT: $rb (lang=$el_norm_p15, profile=$am_profile_langs)"
            cleaned_nonprofile=$((cleaned_nonprofile + 1))
            file_modified=1
          fi
        done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)
      else
        debug "SKIP cleanup (profile not fully satisfied): $basename (profile=$am_profile_langs)"
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
        local -a emb_ratings=() emb_norm_langs=() emb_stream_ids=() emb_scores=()
        local media_secs_p2
        media_secs_p2="$(media_duration_seconds "$mkv_file")"
        [[ -z "$media_secs_p2" ]] && media_secs_p2=0

        for ((i=0; i<emb_count; i++)); do
          local stream_idx lang title
          stream_idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
          lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
          title="$(jq -r ".[$i].tags.title" <<<"$embedded_json")"

          emb_stream_ids+=("$stream_idx")
          local norm_lang
          norm_lang="$(normalize_track_lang "$lang")"
          emb_norm_langs+=("$norm_lang")

          # Extract to temp file for scoring
          local tmpfile="/tmp/sub_auto_${$}_${stream_idx}.srt"
          if ! ffmpeg -v quiet -i "$mkv_file" -map "0:${stream_idx}" -f srt "$tmpfile" </dev/null 2>/dev/null; then
            rm -f "$tmpfile"
            emb_ratings+=("UNKNOWN")
            emb_scores+=(0)
            continue
          fi

          local analysis cues first_sec last_sec mojibake watermarks emb_rating
          analysis="$(analyze_srt_file "$tmpfile")"
          read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
          if echo "$title" | grep -qiE "$WATERMARK_PATTERNS" 2>/dev/null; then
            watermarks=1
          fi
          emb_rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

          # Compute numeric quality score for tie-breaking
          local num_score
          num_score="$(subtitle_quality_score "$tmpfile" "$media_secs_p2" 0)"
          rm -f "$tmpfile"

          emb_ratings+=("$emb_rating")
          emb_scores+=("$num_score")

          [[ "$emb_rating" == "WARN" ]] && warned=$((warned + 1))
        done

        # Second pass: dedup — strip BAD tracks with GOOD replacement, AND collapse
        # GOOD-GOOD duplicates for the same normalized language (keep highest scorer)
        local -a strip_indices=()
        declare -A lang_best_idx=()   # norm_lang -> array index of best track
        declare -A lang_best_score=()  # norm_lang -> best numeric score

        # Normalize good_langs from Phase 1 for comparison
        declare -A muxed_lang_set=()
        for gl in "${good_langs[@]+"${good_langs[@]}"}"; do
          local gl_norm
          gl_norm="$(normalize_track_lang "$gl")"
          muxed_lang_set["$gl_norm"]=1
        done

        for ((i=0; i<emb_count; i++)); do
          [[ "${emb_ratings[$i]}" == "UNKNOWN" ]] && continue
          local norm_lang="${emb_norm_langs[$i]}"
          local this_score="${emb_scores[$i]}"

          if [[ "${emb_ratings[$i]}" == "BAD" ]]; then
            # BAD track: strip if a GOOD mux or GOOD embedded exists for this lang
            local has_good=0
            [[ -n "${muxed_lang_set[$norm_lang]:-}" ]] && has_good=1
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
            continue
          fi

          # GOOD/WARN track: dedup by language — keep highest scorer
          if [[ -z "${lang_best_idx[$norm_lang]:-}" ]]; then
            lang_best_idx["$norm_lang"]="$i"
            lang_best_score["$norm_lang"]="$this_score"
          else
            local prev_idx="${lang_best_idx[$norm_lang]}"
            local prev_score="${lang_best_score[$norm_lang]}"
            if [[ "$this_score" -gt "$prev_score" ]]; then
              # Current track is better — strip previous
              strip_indices+=("${emb_stream_ids[$prev_idx]}")
              log "DEDUP lang=$norm_lang: keep idx=${emb_stream_ids[$i]} (score=$this_score) over idx=${emb_stream_ids[$prev_idx]} (score=$prev_score): $basename"
              lang_best_idx["$norm_lang"]="$i"
              lang_best_score["$norm_lang"]="$this_score"
            else
              # Previous is better or equal — strip current
              strip_indices+=("${emb_stream_ids[$i]}")
              log "DEDUP lang=$norm_lang: keep idx=${emb_stream_ids[$prev_idx]} (score=$prev_score) over idx=${emb_stream_ids[$i]} (score=$this_score): $basename"
            fi
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
            log "STRIPPED ${#strip_indices[@]} track(s) from: $basename"
          else
            rm -f "$strip_tmp"
            log "FAIL strip: $basename"
          fi
        elif [[ ${#strip_indices[@]} -gt 0 ]]; then
          log "[DRY-RUN] Would strip ${#strip_indices[@]} track(s) from: $basename"
          stripped_tracks=$((stripped_tracks + ${#strip_indices[@]}))
          stripped_files=$((stripped_files + 1))
        fi
      fi
    fi

    # --- Phase 3: Emby refresh + Bazarr rescan per modified file ---
    if [[ "$DRY_RUN" -eq 0 ]] && [[ "$file_modified" -eq 1 ]]; then
      emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"
      modified_dirs+=("$mkv_file")

      # Bazarr scan-disk (deduplicated per series/movie dir)
      if [[ -n "$BAZARR_API_KEY" ]]; then
        local rescan_key
        if is_tv_path "$mkv_file"; then
          rescan_key="$(echo "$mkv_file" | sed 's|/Season.*||' | sed 's|/$||')"
        else
          rescan_key="$(dirname "$mkv_file")"
        fi
        if [[ -z "${bazarr_rescanned[$rescan_key]:-}" ]]; then
          bazarr_rescanned["$rescan_key"]=1
          bazarr_rescan_for_file "$mkv_file" "$BAZARR_DB" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed"
        fi
      fi
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

  log "auto-maintain done: files=$total_files muxed=$muxed_files($muxed_tracks tracks) stripped=$stripped_files($stripped_tracks tracks) extracted_nonprofile=$extracted_nonprofile cleaned_nonprofile=$cleaned_nonprofile warned=$warned skipped_converter=$skipped_converter skipped_playback=$skipped_playback skipped_streaming=$skipped_streaming"

  # Discord notification (non-fatal, only when actions taken)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ $((muxed_files + stripped_files + extracted_nonprofile + cleaned_nonprofile)) -gt 0 ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
    local mode="quick"
    [[ "$SINCE_MINUTES" -eq 0 ]] && mode="full"

    # Build file list (basenames, capped at 20)
    local file_list="" file_count=${#modified_dirs[@]}
    local show_count=$file_count
    [[ "$show_count" -gt 20 ]] && show_count=20
    for ((fi=0; fi<show_count; fi++)); do
      file_list+="• $(basename "${modified_dirs[$fi]}")\n"
    done
    [[ "$file_count" -gt 20 ]] && file_list+="+ $((file_count - 20)) more\n"

    # Build description lines
    local desc=""
    [[ "$muxed_files" -gt 0 ]] && desc+="📥 Muxed: ${muxed_files} file(s), ${muxed_tracks} track(s)\n"
    [[ "$stripped_files" -gt 0 ]] && desc+="🗑 Stripped: ${stripped_files} file(s), ${stripped_tracks} track(s)\n"
    [[ "$extracted_nonprofile" -gt 0 ]] && desc+="📤 Extracted non-profile: ${extracted_nonprofile} track(s)\n"
    [[ "$cleaned_nonprofile" -gt 0 ]] && desc+="🧹 Cleaned non-profile: ${cleaned_nonprofile} SRT(s)\n"
    [[ "$deepl_deferred" -gt 0 ]] && desc+="⏳ DeepL deferred: ${deepl_deferred} (grace period)\n"
    [[ "$warned" -gt 0 ]] && desc+="⚠️ Manual review: ${warned}\n"
    [[ "$skipped_converter" -gt 0 ]] && desc+="🔄 Skipped (converter): ${skipped_converter}\n"
    [[ "$skipped_playback" -gt 0 ]] && desc+="▶️ Skipped (playback): ${skipped_playback}\n"
    [[ "$skipped_streaming" -gt 0 ]] && desc+="📺 Skipped (streaming): ${skipped_streaming}\n"
    desc+="🔍 Scanned: ${total_files} files\n"
    [[ -n "$file_list" ]] && desc+="\n**Files modified:**\n${file_list}"

    local payload desc_rendered
    desc_rendered="$(printf '%b' "$desc")"
    payload="$(jq -nc --arg title "Subtitle Auto-Maintain ($mode)" \
      --arg desc "$desc_rendered" \
      --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      '{embeds: [{
        title: $title,
        description: $desc,
        color: 3066993,
        timestamp: $ts
      }]}')"

    curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
      -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 \
      || log "WARN: Discord notification failed (non-fatal)"
  fi
}

case "$COMMAND" in
  audit)        cmd_audit ;;
  mux)          cmd_mux ;;
  strip)        cmd_strip ;;
  auto-maintain) cmd_auto_maintain ;;
esac
