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
SONARR_URL="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
SONARR_KEY="${SONARR_KEY:-}"
RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
RADARR_KEY="${RADARR_KEY:-}"
DEEPL_API_KEY="${DEEPL_API_KEY:-}"
BAZARR_MIN_SCORE="${BAZARR_MIN_SCORE:-80}"
UPGRADE_TRANSLATE_RETRY="${UPGRADE_TRANSLATE_RETRY:-3}"
UPGRADE_ALERT_RETRY="${UPGRADE_ALERT_RETRY:-4}"
UPGRADE_MAX_RETRY="${UPGRADE_MAX_RETRY:-5}"
# Dead-end sources (embedded_desync/missing) cap at UPGRADE_MAX_RETRY; other
# sources use a higher ceiling before abandonment.
UPGRADE_PROVIDER_MAX_RETRY="${UPGRADE_PROVIDER_MAX_RETRY:-30}"
COMPLIANCE_FORMAT="text"
COMPLIANCE_VERBOSE=0

WATERMARK_PATTERNS="galaxytv|yify|yts|opensubtitles|addic7ed|subscene|podnapisi|sub[sz]cene"
_CACHED_WATERMARK_PATTERNS=""

usage() {
  cat <<'EOF'
Usage: subtitle_quality_manager.sh <command> [options]

Commands:
  audit          Score subtitle tracks (embedded + external) and output quality report
  mux            Embed good external .srt files into MKV/MP4/M4V (runs audit first)
  strip          Remove specific embedded subtitle tracks from MKV/MP4/M4V
  auto-maintain  Automated mux/strip with safety checks (quick + full mode)
  enqueue        Add file path(s) to pending work queue for next auto-maintain run
  compliance     Report subtitle compliance against Bazarr profiles for all media
  watermark      Manage subtitle watermark patterns (list/add/remove/test)

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
  subtitle_quality_manager.sh enqueue /path/to/file.mkv [/path/to/file2.mkv ...]
  subtitle_quality_manager.sh compliance --path-prefix /media
  subtitle_quality_manager.sh compliance --path-prefix /media --format json
  subtitle_quality_manager.sh compliance --path-prefix /media --verbose

Compliance options:
  --path-prefix DIR   Root media directory to scan (required)
  --format FORMAT     Output format: text or json (default: text)
  --verbose           Include OK files in text output
EOF
}

# Override lib's log() with our own prefix
log() {
  printf '%s [sub-quality] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*" >&2
}

debug() {
  [[ "$LOG_LEVEL" == "debug" ]] && log "DEBUG $*"
  return 0
}

# Log pre-rewrite metadata for audit trail before in-place MKV overwrites.
# Usage: log_mkv_rewrite_audit "$mkv_file"
log_mkv_rewrite_audit() {
  local _mkv="$1"
  local _rw_size _rw_mtime _rw_inode
  read -r _rw_size _rw_mtime _rw_inode < <(stat -c '%s %Y %i' "$_mkv" 2>/dev/null || echo "0 0 0")
  log "MKV_REWRITE_AUDIT: size=$_rw_size mtime=$_rw_mtime inode=$_rw_inode path=$_mkv"
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
    CREATE TABLE IF NOT EXISTS pending_work (
      file_path TEXT PRIMARY KEY,
      enqueued_at INTEGER NOT NULL,
      source TEXT DEFAULT 'unknown'
    );
    CREATE TABLE IF NOT EXISTS needs_upgrade (
      file_path      TEXT    NOT NULL,
      lang           TEXT    NOT NULL,
      forced         INTEGER NOT NULL DEFAULT 0,
      current_rating TEXT    NOT NULL DEFAULT 'BAD',
      current_score  INTEGER NOT NULL DEFAULT 0,
      source         TEXT    NOT NULL DEFAULT 'external',
      first_seen_ts  INTEGER NOT NULL,
      last_retry_ts  INTEGER NOT NULL DEFAULT 0,
      retry_count    INTEGER NOT NULL DEFAULT 0,
      resolved_ts    INTEGER,
      PRIMARY KEY (file_path, lang, forced)
    );
    CREATE INDEX IF NOT EXISTS idx_nu_last_retry ON needs_upgrade(last_retry_ts);
    CREATE INDEX IF NOT EXISTS idx_nu_resolved   ON needs_upgrade(resolved_ts);
    CREATE TABLE IF NOT EXISTS watermark_patterns (
      pattern    TEXT PRIMARY KEY,
      source     TEXT DEFAULT 'builtin',
      added_ts   INTEGER NOT NULL,
      hit_count  INTEGER DEFAULT 0,
      last_hit   INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sync_drift_cache (
      file_path    TEXT NOT NULL,
      target_lang  TEXT NOT NULL,
      file_mtime   INTEGER NOT NULL,
      ref_lang     TEXT NOT NULL,
      max_drift    REAL NOT NULL,
      drift_rating TEXT NOT NULL,
      checked_ts   INTEGER NOT NULL,
      PRIMARY KEY (file_path, target_lang)
    );
    CREATE TABLE IF NOT EXISTS quality_checks (
      srt_path      TEXT NOT NULL,
      srt_mtime     INTEGER NOT NULL,
      expected_lang TEXT NOT NULL,
      actual_lang   TEXT DEFAULT '',
      quality       TEXT NOT NULL,
      reason        TEXT DEFAULT '',
      confidence    REAL DEFAULT 0.0,
      checked_ts    INTEGER NOT NULL,
      provider      TEXT DEFAULT 'gemini',
      PRIMARY KEY (srt_path, expected_lang)
    );
    PRAGMA journal_mode=WAL;
    PRAGMA busy_timeout=30000;
  " </dev/null >/dev/null 2>&1
}

init_watermark_patterns() {
  local db="$1"
  local now
  now="$(date +%s)"
  # One multi-row INSERT covers all builtins — idempotent via OR IGNORE
  sqm_db "$db" "
    INSERT OR IGNORE INTO watermark_patterns(pattern, source, added_ts) VALUES
      ('galaxytv',         'builtin', $now),
      ('yify',             'builtin', $now),
      ('yts',              'builtin', $now),
      ('opensubtitles',    'builtin', $now),
      ('addic7ed',         'builtin', $now),
      ('subscene',         'builtin', $now),
      ('podnapisi',        'builtin', $now),
      ('sub[sz]cene',      'builtin', $now),
      ('the evil team',    'builtin', $now),
      ('dr\\.? ?infinito', 'builtin', $now),
      ('grupots',          'builtin', $now),
      ('grupo ?ts',        'builtin', $now);
  " 2>/dev/null || true
}

load_watermark_patterns() {
  local db="$1"
  sqm_db "$db" -separator '|' "SELECT pattern FROM watermark_patterns ORDER BY source, pattern;" 2>/dev/null
}

# SQLite wrapper: sets busy_timeout via -cmd (no stdout leakage), passes through flags and query
sqm_db() {
  local db="$1"; shift
  local flags=()
  while [[ "${1:-}" == -* ]]; do
    flags+=("$1" "$2"); shift 2
  done
  sqlite3 "${flags[@]}" -cmd ".timeout 30000" "$db" "$@" </dev/null
}

# Enqueue a file for auto-maintain processing (survives --since window)
enqueue_pending() {
  local db="$1" file_path="$2" source="${3:-manual}"
  init_state_db "$db"
  sqm_db "$db" "INSERT OR REPLACE INTO pending_work (file_path, enqueued_at, source) VALUES ('$(sql_escape "$file_path")', $(date +%s), '$source');" >/dev/null 2>&1 || true
}

# Drain pending queue, returns file paths one per line
drain_pending() {
  local db="$1"
  [[ ! -f "$db" ]] && return 0
  local -a paths=()
  while IFS= read -r p; do
    [[ -n "$p" ]] && paths+=("$p")
  done < <(sqm_db "$db" "SELECT file_path FROM pending_work;" 2>/dev/null || true)
  # Delete drained entries
  sqm_db "$db" "DELETE FROM pending_work;" 2>/dev/null || true
  for p in "${paths[@]}"; do
    printf '%s\n' "$p"
  done
}

# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

find_media_files() {
  local dir="$1"
  if [[ "$RECURSIVE" -eq 1 ]]; then
    find "$dir" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) ! -name "*tmp.*" | sort
  else
    find "$dir" -maxdepth 1 -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) ! -name "*tmp.*" | sort
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
    "SELECT path FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL AND COALESCE(keep_local,0) = 0;" 2>/dev/null)
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

  cue_count="$(grep -cE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} --> ' "$srt_file" 2>/dev/null)" || cue_count=0

  if [[ "$cue_count" -gt 0 ]]; then
    first_ms="$(grep -oEm1 '^([0-9]{2}):([0-9]{2}):([0-9]{2}),([0-9]{3}) -->' "$srt_file" | head -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
    last_ms="$(grep -oE '^[0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} -->' "$srt_file" | tail -1 | sed 's/ -->//' | awk -F'[:,]' '{print ($1*3600 + $2*60 + $3) + $4/1000}')"
  fi

  local mojibake=0
  if grep -qP '[\x{FFFD}]|Ã©|Ã¡|Ã±|Ã³|Ã­|â€™|â€œ|â€' "$srt_file" 2>/dev/null; then
    mojibake=1
  fi

  local watermarks=0
  local _wm_pat="${_CACHED_WATERMARK_PATTERNS:-$WATERMARK_PATTERNS}"
  if [[ -n "$_wm_pat" ]] && grep -qiE "$_wm_pat" "$srt_file" 2>/dev/null; then
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

build_embedded_lang_map() {
  local mkv_file="$1"
  declare -n _idx_map="$2"
  declare -n _codec_map="$3"
  local emb_json
  emb_json="$(get_embedded_subs "$mkv_file")"
  while IFS=$'\t' read -r ei_idx ei_lang ei_codec; do
    [[ -z "$ei_idx" ]] && continue
    local ei_norm
    ei_norm="$(normalize_track_lang "$ei_lang")"
    if [[ -z "${_idx_map[$ei_norm]:-}" ]]; then
      _idx_map["$ei_norm"]="$ei_idx"
      _codec_map["$ei_norm"]="$ei_codec"
    fi
  done < <(jq -r '.[] | [(.index | tostring), (.tags.language // "und"), .codec_name] | @tsv' <<<"$emb_json")
}

# Returns 0 = external wins (strip embedded), 1 = embedded wins (protect)
check_embedded_collision() {
  local mkv_file="$1" lang="$2" collide_idx="$3" collide_codec="$4"
  local ext_rating="$5" ext_cues="$6" duration="$7" log_label="$8"
  local ext_wins=1
  if is_text_sub_codec "$collide_codec"; then
    local emb_tmp="${mkv_file%.*}.emb_cmp_${lang}.srt"
    if ffmpeg -y -v quiet -i "$mkv_file" -map "0:${collide_idx}" "$emb_tmp" </dev/null 2>/dev/null && [[ -s "$emb_tmp" ]]; then
      local emb_analysis emb_cues emb_first emb_last emb_mojibake emb_wm emb_rating
      emb_analysis="$(analyze_srt_file "$emb_tmp")"
      read -r emb_cues emb_first emb_last emb_mojibake emb_wm <<<"$emb_analysis"
      emb_rating="$(score_subtitle "$emb_cues" "$emb_first" "$emb_last" "$duration" "$emb_mojibake" "$emb_wm")"
      if [[ "$emb_rating" == "GOOD" && "$ext_rating" == "GOOD" && "$emb_cues" -gt "$ext_cues" ]]; then
        ext_wins=0
        log "PROTECT embedded idx=$collide_idx lang=$lang (${emb_cues} cues > external ${ext_cues} cues): $log_label"
      elif [[ "$emb_rating" == "GOOD" && "$ext_rating" != "GOOD" ]]; then
        ext_wins=0
        log "PROTECT embedded idx=$collide_idx lang=$lang (GOOD vs external ${ext_rating}): $log_label"
      fi
    fi
    rm -f "$emb_tmp"
  fi
  return $(( 1 - ext_wins ))
}

# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

score_subtitle() {
  local cues="$1" first="$2" last="$3" duration="$4" mojibake="$5" watermarks="$6"
  local sync_drift_rating="${7:-}"
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
    # Late first-cue: only WARN when coverage is also low (high coverage = artistic choice)
    if [[ "$(awk "BEGIN { print ($first > 300) }")" -eq 1 ]] && [[ "$coverage" -lt 80 ]]; then
      rating="WARN"
    fi
  fi

  # Sync drift override (7th param from check_sync_drift)
  if [[ "$sync_drift_rating" == "BAD" ]]; then
    echo "BAD"; return
  elif [[ "$sync_drift_rating" == "WARN" && "$rating" == "GOOD" ]]; then
    rating="WARN"
  fi

  echo "$rating"
}

# ---------------------------------------------------------------------------
# Sync drift detection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# get_drift_anchor_lang emb_json profile_langs
#
# Determines the drift anchor language: first profile lang that has an
# embedded text-based subtitle track.  Outputs the normalized lang code,
# or "" when no anchor can be determined.
# ---------------------------------------------------------------------------
get_drift_anchor_lang() {
  local emb_json="$1" profile_langs="$2"
  [[ -z "$profile_langs" ]] && return 0

  declare -A _gda_text_langs=()
  while IFS=$'\t' read -r _gda_lang _gda_codec; do
    is_text_sub_codec "$_gda_codec" && _gda_text_langs["$(normalize_track_lang "$_gda_lang")"]=1
  done < <(jq -r '.[] | [(.tags.language // "und"), .codec_name] | @tsv' <<<"$emb_json")

  IFS=',' read -ra _gda_codes <<< "$profile_langs"
  for _gda_c in "${_gda_codes[@]}"; do
    local _gda_norm
    _gda_norm="$(normalize_track_lang "$_gda_c")"
    if [[ -n "${_gda_text_langs[$_gda_norm]:-}" ]]; then
      echo "$_gda_norm"
      return
    fi
  done
}

# ---------------------------------------------------------------------------
# find_reference_track mkv_file target_lang [profile_langs]
#
# Finds the best reference subtitle track for sync-drift comparison against
# target_lang.  Outputs "ref_stream_idx ref_lang" on stdout, or "SKIP none"
# when no usable reference exists.
#
# Cascade:
#   1. Profile anchor — first profile lang that is NOT target_lang and HAS a
#      text-based embedded subtitle track.
#   2. Audio language — first audio track language (if != target_lang) that
#      has a matching text-based embedded subtitle track.
#   3. SKIP — no usable reference found.
# ---------------------------------------------------------------------------
find_reference_track() {
  local mkv_file="$1" target_lang="$2" profile_langs="${3:-}"

  # Retrieve all embedded subtitle streams as a JSON array.
  local emb_json
  emb_json="$(get_embedded_subs "$mkv_file")"

  # Need at least 2 tracks for a meaningful reference (target + reference).
  [[ "$(jq 'length' <<<"$emb_json")" -lt 2 ]] && { echo "SKIP none"; return; }

  # Build map: normalized_lang -> first stream index for text-based tracks
  # that are NOT the target language.
  declare -A avail_tracks=()
  while IFS=$'\t' read -r idx lang codec; do
    is_text_sub_codec "$codec" || continue
    local norm
    norm="$(normalize_track_lang "$lang")"
    [[ "$norm" == "$target_lang" ]] && continue  # Skip the target itself
    # First text track per language wins
    [[ -z "${avail_tracks[$norm]:-}" ]] && avail_tracks["$norm"]="$idx"
  done < <(jq -r '.[] | [(.index | tostring), (.tags.language // "und"), .codec_name] | @tsv' <<<"$emb_json")

  # Cascade 1: Profile anchor — first profile lang (not target) with a track
  if [[ -n "$profile_langs" ]]; then
    IFS=',' read -ra prof_codes <<< "$profile_langs"
    for pc in "${prof_codes[@]}"; do
      local pc_norm
      pc_norm="$(normalize_track_lang "$pc")"
      [[ "$pc_norm" == "$target_lang" ]] && continue
      if [[ -n "${avail_tracks[$pc_norm]:-}" ]]; then
        echo "${avail_tracks[$pc_norm]} $pc_norm"
        return
      fi
    done
  fi

  # Cascade 2: Audio language — first audio lang (not target) with a sub track
  local audio_langs
  audio_langs="$(get_audio_languages "$mkv_file")" || audio_langs=""
  if [[ -n "$audio_langs" ]]; then
    IFS=',' read -ra a_codes <<< "$audio_langs"
    for ac in "${a_codes[@]}"; do
      local ac_norm
      ac_norm="$(normalize_track_lang "$ac")"
      [[ "$ac_norm" == "$target_lang" ]] && continue
      if [[ -n "${avail_tracks[$ac_norm]:-}" ]]; then
        echo "${avail_tracks[$ac_norm]} $ac_norm"
        return
      fi
    done
  fi

  # Cascade 3: No usable reference
  echo "SKIP none"
}

# ---------------------------------------------------------------------------
# check_sync_drift mkv_file target_lang duration [profile_langs]
#
# Measures the maximum timing drift between the target subtitle track and a
# reference track chosen by find_reference_track().
#
# Output: "max_drift_sec drift_rating ref_lang"
#   drift_rating: GOOD (<30s), WARN (30-60s), BAD (>60s), SKIP (no reference)
# ---------------------------------------------------------------------------
check_sync_drift() {
  local mkv_file="$1" target_lang="$2" duration="$3" profile_langs="${4:-}"

  # Find reference track
  local ref_result ref_idx ref_lang
  ref_result="$(find_reference_track "$mkv_file" "$target_lang" "$profile_langs")"
  read -r ref_idx ref_lang <<< "$ref_result"

  if [[ "$ref_idx" == "SKIP" ]]; then
    echo "0 SKIP none"
    return
  fi

  # Find target track index among text-based embedded subs
  local emb_json target_idx=""
  emb_json="$(get_embedded_subs "$mkv_file")"
  while IFS=$'\t' read -r idx lang codec; do
    is_text_sub_codec "$codec" || continue
    local norm
    norm="$(normalize_track_lang "$lang")"
    if [[ "$norm" == "$target_lang" ]]; then
      target_idx="$idx"
      break
    fi
  done < <(jq -r '.[] | [(.index | tostring), (.tags.language // "und"), .codec_name] | @tsv' <<<"$emb_json")

  [[ -z "$target_idx" ]] && { echo "0 SKIP none"; return; }

  # Extract both tracks to temporary SRT files
  local tmp_ref="/tmp/drift_ref_${$}.srt"
  local tmp_tgt="/tmp/drift_tgt_${$}.srt"
  # shellcheck disable=SC2064
  trap "rm -f '$tmp_ref' '$tmp_tgt'" RETURN

  ffmpeg -v quiet -i "$mkv_file" -map "0:${ref_idx}" -f srt "$tmp_ref" </dev/null 2>/dev/null \
    || { echo "0 SKIP none"; return; }
  ffmpeg -v quiet -i "$mkv_file" -map "0:${target_idx}" -f srt "$tmp_tgt" </dev/null 2>/dev/null \
    || { echo "0 SKIP none"; return; }

  [[ ! -s "$tmp_ref" || ! -s "$tmp_tgt" ]] && { echo "0 SKIP none"; return; }

  # Parse both SRT files with a single awk pass; sample ~20 evenly-spaced
  # timestamp pairs and compute the maximum absolute drift.
  # Uses substr/index rather than three-argument match() for mawk compatibility.
  local max_drift
  max_drift="$(awk '
    function parse_ts(s,   parts, hms, n) {
      # SRT timestamp: HH:MM:SS,mmm — split on comma
      n = split(s, parts, ",")
      split(parts[1], hms, ":")
      return hms[1]*3600 + hms[2]*60 + hms[3] + (n > 1 ? parts[2]+0 : 0)/1000
    }

    BEGIN {
      ref_count  = 0
      tgt_count  = 0
      phase      = 1   # 1 = reading ref file, 2 = reading target file
      prev_file  = ""
    }

    # Detect file boundary
    FILENAME != prev_file {
      if (NR > 1) phase = 2
      prev_file = FILENAME
    }

    phase == 1 && /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9] -->/ {
      ts = substr($0, 1, index($0, " -->") - 1)
      ref_times[ref_count++] = parse_ts(ts)
    }

    phase == 2 && /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9] -->/ {
      ts = substr($0, 1, index($0, " -->") - 1)
      tgt_times[tgt_count++] = parse_ts(ts)
    }

    END {
      if (ref_count < 5 || tgt_count < 5) { print 0; exit }

      # Sample ~20 evenly-spaced points; map target index to proportional ref index
      shorter = (tgt_count < ref_count) ? tgt_count : ref_count
      samples = (shorter < 20) ? shorter : 20
      max_d   = 0

      for (s = 0; s < samples; s++) {
        i = int(s * (tgt_count - 1) / (samples - 1))
        j = int(i * ref_count / tgt_count)
        if (j >= ref_count) j = ref_count - 1

        d = tgt_times[i] - ref_times[j]
        if (d < 0) d = -d
        if (d > max_d) max_d = d
      }

      printf "%.0f", max_d
    }
  ' "$tmp_ref" "$tmp_tgt" 2>/dev/null)" || max_drift=0

  [[ -z "$max_drift" ]] && max_drift=0

  # Classify drift
  local drift_rating
  if [[ "$max_drift" -lt 30 ]]; then
    drift_rating="GOOD"
  elif [[ "$max_drift" -lt 60 ]]; then
    drift_rating="WARN"
  else
    drift_rating="BAD"
  fi

  echo "$max_drift $drift_rating $ref_lang"
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_audit() {
  log "Auditing subtitles in: $PATH_PREFIX (recursive=$RECURSIVE)"

  local state_db="$STATE_DIR/subtitle_quality_state.db"
  init_state_db "$state_db"
  init_watermark_patterns "$state_db"
  _CACHED_WATERMARK_PATTERNS="$(load_watermark_patterns "$state_db")"
  [[ -z "$_CACHED_WATERMARK_PATTERNS" ]] && _CACHED_WATERMARK_PATTERNS="$WATERMARK_PATTERNS"

  local total_files=0 total_tracks=0 good=0 warn=0 bad=0

  while IFS= read -r mkv_file; do
    total_files=$((total_files + 1))
    local basename dir duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    duration="$(get_video_duration "$mkv_file")"

    local am_profile_langs=""
    am_profile_langs="$(resolve_bazarr_profile_langs "$mkv_file" "$BAZARR_DB")" || am_profile_langs=""

    printf '\n=== %s (%.0fs) ===\n' "$basename" "$duration" >&2
    printf '%-6s %-8s %-7s %-6s %-6s %-7s %-4s %-4s %s\n' \
      "TYPE" "LANG" "CODEC" "CUES" "COVER" "DRIFT" "WM" "ENC" "RATING" >&2
    printf '%s\n' "-----------------------------------------------------------------" >&2

    # Embedded subtitle tracks
    local embedded_json emb_count
    embedded_json="$(get_embedded_subs "$mkv_file")"
    emb_count="$(jq 'length' <<<"$embedded_json")"

    # Determine drift anchor language (first profile lang with a text track)
    local drift_anchor_lang=""
    drift_anchor_lang="$(get_drift_anchor_lang "$embedded_json" "$am_profile_langs")"

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
      local _wm_pat="${_CACHED_WATERMARK_PATTERNS:-$WATERMARK_PATTERNS}"
      if [[ -n "$_wm_pat" ]] && echo "$title" | grep -qiE "$_wm_pat" 2>/dev/null; then
        watermarks=1
      fi

      local coverage="--"
      if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
        coverage="$(awk "BEGIN { printf \"%.0f%%\", ($last_sec / $duration) * 100 }")"
      fi

      # Compute sync drift against reference track (skip anchor — it's the reference)
      local drift_display="--"
      local drift_rating_val=""
      local lang_norm
      lang_norm="$(normalize_track_lang "$lang")"
      if [[ -n "$drift_anchor_lang" && "$lang_norm" != "$drift_anchor_lang" ]]; then
        local drift_result drift_max drift_rate drift_ref
        drift_result="$(check_sync_drift "$mkv_file" "$lang_norm" "$duration" "$am_profile_langs")"
        read -r drift_max drift_rate drift_ref <<< "$drift_result"
        drift_rating_val="$drift_rate"
        case "$drift_rate" in
          GOOD) drift_display="${drift_max}s" ;;
          WARN) drift_display="${drift_max}s?" ;;
          BAD)  drift_display="${drift_max}s!" ;;
          SKIP) drift_display="--" ;;
        esac
      fi

      local rating
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks" "$drift_rating_val")"

      printf '%-6s %-8s %-7s %-6s %-6s %-7s %-4s %-4s %s\n' \
        "EMB" "$lang" "$codec_name" "$cues" "$coverage" "$drift_display" \
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
      ext_lang="$(extract_srt_lang "$srt_basename" "$name_stem")"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      local analysis cues first_sec last_sec mojibake watermarks
      analysis="$(analyze_srt_file "$srt_file")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"

      local coverage="--"
      if [[ "$duration" != "0" ]] && [[ "$duration" != "0.0" ]] && [[ "$cues" -gt 0 ]]; then
        coverage="$(awk "BEGIN { printf \"%.0f%%\", ($last_sec / $duration) * 100 }")"
      fi

      local rating
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      printf '%-6s %-8s %-7s %-6s %-6s %-7s %-4s %-4s %s\n' \
        "EXT" "$ext_lang" "srt" "$cues" "$coverage" "--" \
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
    declare -A embedded_lang_idx=()
    declare -A embedded_lang_codec=()
    build_embedded_lang_map "$mkv_file" embedded_lang_idx embedded_lang_codec
    local -a premux_strip_indices=()

    # Find external SRT files for this MKV
    local -a srt_files=()
    local -a srt_langs=()
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename ext_lang
      srt_basename="$(basename "$srt_file")"
      ext_lang="$(extract_srt_lang "$srt_basename" "$name_stem")"
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

      # Pre-mux collision check: only strip embedded if external is actually better
      local srt_norm
      srt_norm="$(normalize_track_lang "$ext_lang")"
      if [[ -n "${embedded_lang_idx[$srt_norm]:-}" ]]; then
        if check_embedded_collision "$mkv_file" "$srt_norm" \
             "${embedded_lang_idx[$srt_norm]}" "${embedded_lang_codec[$srt_norm]:-unknown}" \
             "$rating" "$cues" "$duration" "$srt_basename"; then
          premux_strip_indices+=("${embedded_lang_idx[$srt_norm]}")
          log "COLLISION lang=$srt_norm: will replace embedded idx=${embedded_lang_idx[$srt_norm]} with external SRT: $srt_basename"
        fi
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
        map_args+=(-metadata:s:"${abs_idx}" "language=${lang}")
      else
        local metadata_idx=$((existing_sub_count + i))
        map_args+=(-metadata:s:s:"${metadata_idx}" "language=${lang}")
      fi
    done

    local ext="${mkv_file##*.}"
    local sub_codec="copy"
    [[ "${ext,,}" == "mp4" || "${ext,,}" == "m4v" ]] && sub_codec="mov_text"
    local tmp_out="${mkv_file%/*}/.${mkv_file##*/}"
    tmp_out="${tmp_out%.*}.subtmp.${ext}"
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

    if ! validate_streams_match "$mkv_file" "$tmp_out" "mux"; then
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    # Swap original with muxed version
    log_mkv_rewrite_audit "$mkv_file"
    mv "$tmp_out" "$mkv_file"

    # Delete external SRT files (KEEP if inferior translator marker exists — gemini/deepl/google
    # subs are user-flagged for upgrade; ollama subs are good and may be deleted)
    for sf in "${srt_files[@]}"; do
      if [[ -f "${sf}.gemini" || -f "${sf}.deepl" || -f "${sf}.google" ]]; then
        log "  Kept (inferior translator marker preserved): $(basename "$sf")"
      else
        rm -f "$sf"
        log "  Deleted: $(basename "$sf")"
      fi
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
      local strip_tmp="${mkv_file%/*}/.${mkv_file##*/}"
      strip_tmp="${strip_tmp%.*}.collisiontmp.${strip_ext}"
      if "${strip_cmd[@]}" "$strip_tmp" </dev/null 2>/dev/null && [[ -s "$strip_tmp" ]] && validate_streams_match "$mkv_file" "$strip_tmp" "post_mux_strip"; then
        log_mkv_rewrite_audit "$mkv_file"
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
    local _mux_fields
    _mux_fields="$(jq -nc \
      --arg muxed "$muxed" \
      --arg files "$total_files" \
      --arg skipped "$skipped" \
      --arg summary "$(printf '%b' "$mux_summary")" \
      '[
        {name: "📥 Muxed",   value: ($muxed + " track(s)"), inline: true},
        {name: "📁 Files",   value: $files,                 inline: true},
        {name: "⏭️ Skipped", value: $skipped,               inline: true},
        {name: "📝 Details", value: $summary,               inline: false}
      ]')"
    notify_discord_embed "📥 Subtitle Mux" \
      "Muxed **$muxed** subtitle(s) into **$total_files** file(s)" \
      3066993 "Subtitle Quality Manager" "$_mux_fields" \
      || log "WARN: Discord notification failed (non-fatal)"
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
    local tmp_out="${mkv_file%/*}/.${mkv_file##*/}"
    tmp_out="${tmp_out%.*}.striptmp.${ext}"
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

    if ! validate_streams_match "$mkv_file" "$tmp_out" "cmd_strip"; then
      rm -f "$tmp_out"
      failed=$((failed + 1))
      continue
    fi

    log_mkv_rewrite_audit "$mkv_file"
    mv "$tmp_out" "$mkv_file"
    stripped=$((stripped + ${#remove_indices[@]}))
    log "STRIPPED ${#remove_indices[@]} track(s) from: $basename"
    emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"

  done < <(find_media_files "$PATH_PREFIX")

  log "Done. ${stripped} stripped, ${skipped} skipped, ${skipped_streaming} skipped(streaming), ${failed} failed."

  # Discord notification (non-fatal)
  if [[ "$stripped" -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    local _strip_fields
    _strip_fields="$(jq -nc \
      --arg stripped "$stripped" \
      --arg files "$total" \
      --arg skipped "$skipped" \
      --arg mode "$mode_label" \
      '[
        {name: "✂️ Stripped", value: ($stripped + " track(s)"), inline: true},
        {name: "📁 Files",   value: $files,                   inline: true},
        {name: "⏭️ Skipped", value: $skipped,                 inline: true},
        {name: "🏷️ Mode",    value: $mode,                    inline: true}
      ]')"
    notify_discord_embed "✂️ Subtitle Strip" \
      "Stripped **$stripped** track(s) from **$total** file(s)" \
      15158332 "Subtitle Quality Manager" "$_strip_fields" \
      || log "WARN: Discord notification failed (non-fatal)"
  fi
}

# ---------------------------------------------------------------------------
# Provider cycling: try all Bazarr manual-search results for a language
# ---------------------------------------------------------------------------

try_providers_for_lang() {
  local mkv_file="$1" lang="$2" duration="$3"
  local max_attempts=8

  # Resolve Bazarr IDs
  local media_type endpoint_type id_param
  if is_tv_path "$mkv_file"; then
    media_type="episode"
    endpoint_type="episodes"
    local series_dir="${mkv_file%%/Season*}"
    local bsq_series_id bsq_episode_id
    bsq_series_id="$(sqlite3 "$BAZARR_DB" "PRAGMA busy_timeout=30000; SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(sql_escape "$(basename "$series_dir")")%' LIMIT 1;" </dev/null 2>/dev/null)" || true
    bsq_episode_id="$(sqlite3 "$BAZARR_DB" "PRAGMA busy_timeout=30000; SELECT sonarrEpisodeId FROM table_episodes WHERE path LIKE '%$(sql_escape "$(basename "${mkv_file%.*}")")%' LIMIT 1;" </dev/null 2>/dev/null)" || true
    [[ -z "$bsq_series_id" || -z "$bsq_episode_id" ]] && { log "PROVIDER_CYCLE: no Bazarr episode ID for $(basename "$mkv_file")"; return 1; }
    id_param="episodeid=${bsq_episode_id}"
  else
    media_type="movie"
    endpoint_type="movies"
    local bsq_radarr_id
    bsq_radarr_id="$(sqlite3 "$BAZARR_DB" "PRAGMA busy_timeout=30000; SELECT radarrId FROM table_movies WHERE path LIKE '%$(sql_escape "$(basename "${mkv_file%.*}")")%' LIMIT 1;" </dev/null 2>/dev/null)" || true
    [[ -z "$bsq_radarr_id" ]] && { log "PROVIDER_CYCLE: no Bazarr movie ID for $(basename "$mkv_file")"; return 1; }
    id_param="radarrid=${bsq_radarr_id}"
  fi

  # Manual search: get all provider results
  local search_json
  search_json="$(curl -sS -m 60 --connect-timeout 10 \
    -H "X-API-KEY: ${BAZARR_API_KEY}" \
    "${BAZARR_URL}/api/providers/${endpoint_type}?${id_param}" </dev/null 2>/dev/null)" || { log "PROVIDER_CYCLE: manual search request failed"; return 1; }

  # Filter by target language and sort by score descending
  local target_lang3
  target_lang3="$(lang_to_iso639_2 "$lang")"
  local filtered
  filtered="$(jq -c --arg lang2 "$lang" --arg lang3 "$target_lang3" --argjson min_score "$BAZARR_MIN_SCORE" '
    [.data // . | if type == "array" then .[] else empty end
     | select(
         (.language // "" | ascii_downcase) == ($lang2 | ascii_downcase) or
         (.language // "" | ascii_downcase) == ($lang3 | ascii_downcase) or
         (.code2 // "" | ascii_downcase) == ($lang2 | ascii_downcase) or
         (.code3 // "" | ascii_downcase) == ($lang3 | ascii_downcase)
       )]
    | sort_by(-.score)
    | [.[] | select(.score >= $min_score)]
    | .[:'"$max_attempts"']
  ' <<<"$search_json" 2>/dev/null)" || filtered="[]"

  local total_before_filter
  total_before_filter="$(jq -c --arg lang2 "$lang" --arg lang3 "$target_lang3" '
    [.data // . | if type == "array" then .[] else empty end
     | select(
         (.language // "" | ascii_downcase) == ($lang2 | ascii_downcase) or
         (.language // "" | ascii_downcase) == ($lang3 | ascii_downcase) or
         (.code2 // "" | ascii_downcase) == ($lang2 | ascii_downcase) or
         (.code3 // "" | ascii_downcase) == ($lang3 | ascii_downcase)
       )] | length
  ' <<<"$search_json" 2>/dev/null)" || total_before_filter=0

  local result_count
  result_count="$(jq 'length' <<<"$filtered")"
  if [[ "$result_count" -eq 0 ]]; then
    log "PROVIDER_CYCLE: 0 results for lang=$lang: $(basename "$mkv_file")"
    return 1
  fi
  if [[ "$total_before_filter" -gt 0 && "$result_count" -lt "$total_before_filter" ]]; then
    log "PROVIDER_CYCLE: filtered ${total_before_filter} → ${result_count} results (min_score=${BAZARR_MIN_SCORE}) for lang=$lang: $(basename "$mkv_file")"
  fi

  log "PROVIDER_CYCLE: trying $result_count result(s) for lang=$lang: $(basename "$mkv_file")"

  local dir name_stem
  dir="$(dirname "$mkv_file")"
  name_stem="$(basename "${mkv_file%.*}")"

  local -a _prov_arr=() _sub_arr=() _score_arr=()
  while IFS=$'\t' read -r _p _s _sc; do
    _prov_arr+=("$_p")
    _sub_arr+=("$_s")
    _score_arr+=("$_sc")
  done < <(jq -r '.[] | [.provider, .subtitle, (.score | tostring)] | @tsv' <<<"$filtered")

  local attempt=0
  for ((ri=0; ri<result_count; ri++)); do
    attempt=$((attempt + 1))
    local provider subtitle_obj score
    provider="${_prov_arr[$ri]}"
    subtitle_obj="${_sub_arr[$ri]}"
    score="${_score_arr[$ri]}"

    # Download this specific result
    local dl_payload
    if [[ "$media_type" == "episode" ]]; then
      dl_payload="$(jq -nc --arg p "$provider" --arg s "$subtitle_obj" \
        --arg sid "$bsq_series_id" --arg eid "$bsq_episode_id" \
        '{provider: $p, subtitle: $s, seriesid: ($sid | tonumber), episodeid: ($eid | tonumber), language: "'"$lang"'"}')"
    else
      dl_payload="$(jq -nc --arg p "$provider" --arg s "$subtitle_obj" \
        --arg rid "$bsq_radarr_id" \
        '{provider: $p, subtitle: $s, radarrid: ($rid | tonumber), language: "'"$lang"'"}')"
    fi

    local dl_http
    dl_http="$(curl -sS -m 30 --connect-timeout 10 -o /dev/null -w '%{http_code}' \
      -X POST -H "X-API-KEY: ${BAZARR_API_KEY}" -H "Content-Type: application/json" \
      -d "$dl_payload" "${BAZARR_URL}/api/providers/${endpoint_type}" </dev/null 2>/dev/null)" || dl_http="000"

    if [[ "$dl_http" != "200" && "$dl_http" != "201" && "$dl_http" != "204" ]]; then
      log "PROVIDER_CYCLE: download failed (http=$dl_http) provider=$provider attempt=$attempt/$result_count"
      continue
    fi

    # Wait for Bazarr to write the file
    sleep 3

    # Find the newly written SRT
    local new_srt=""
    while IFS= read -r candidate; do
      [[ -z "$candidate" ]] && continue
      [[ -f "${candidate}.deepl-source" ]] && continue
      new_srt="$candidate"
      break
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.${lang}*.srt" -type f -newer "$mkv_file" 2>/dev/null | sort -r)

    # Fallback: check for any SRT matching the lang (Bazarr may use different naming)
    if [[ -z "$new_srt" ]]; then
      while IFS= read -r candidate; do
        [[ -z "$candidate" ]] && continue
        [[ -f "${candidate}.deepl-source" ]] && continue
        local cand_base cand_lang
        cand_base="$(basename "$candidate")"
        cand_lang="$(extract_srt_lang "$cand_base" "$name_stem")"
        cand_lang="$(normalize_track_lang "$cand_lang")"
        if [[ "$cand_lang" == "$lang" ]]; then
          new_srt="$candidate"
          break
        fi
      done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f -mmin -1 2>/dev/null | sort -r)
    fi

    if [[ -z "$new_srt" ]]; then
      log "PROVIDER_CYCLE: no SRT found after download, provider=$provider attempt=$attempt/$result_count"
      continue
    fi

    # Score the downloaded SRT
    local analysis cues first_sec last_sec mojibake watermarks rating
    analysis="$(analyze_srt_file "$new_srt")"
    read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
    rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

    if [[ "$rating" == "GOOD" || "$rating" == "WARN" ]]; then
      log "PROVIDER_CYCLE: SUCCESS provider=$provider score=$score rating=$rating attempt=$attempt/$result_count: $(basename "$new_srt")"
      return 0
    fi

    # BAD — delete, blacklist, try next
    rm -f "$new_srt"
    log "PROVIDER_CYCLE: BAD result, blacklisting provider=$provider score=$score attempt=$attempt/$result_count"

    # Blacklist this result
    local bl_payload
    if [[ "$media_type" == "episode" ]]; then
      bl_payload="$(jq -nc --arg p "$provider" --arg s "$subtitle_obj" \
        --arg sid "$bsq_series_id" --arg eid "$bsq_episode_id" \
        --arg lang "$lang" \
        '{provider: $p, subtitle: $s, seriesid: ($sid | tonumber), episodeid: ($eid | tonumber), language: $lang}')"
    else
      bl_payload="$(jq -nc --arg p "$provider" --arg s "$subtitle_obj" \
        --arg rid "$bsq_radarr_id" --arg lang "$lang" \
        '{provider: $p, subtitle: $s, radarrid: ($rid | tonumber), language: $lang}')"
    fi
    curl -sS -m 15 --connect-timeout 8 -o /dev/null \
      -X POST -H "X-API-KEY: ${BAZARR_API_KEY}" -H "Content-Type: application/json" \
      -d "$bl_payload" "${BAZARR_URL}/api/${endpoint_type}/blacklist" </dev/null 2>/dev/null || true
  done

  log "PROVIDER_CYCLE: all $result_count result(s) exhausted for lang=$lang: $(basename "$mkv_file")"
  return 1
}

try_translate_inline() {
  local mkv_file="$1" target_lang="$2"
  log "TRANSLATE_INLINE: attempting translation for lang=$target_lang: $(basename "$mkv_file")"
  # Per-file flock: ensures the WSL and debian workers never translate the same file simultaneously
  local _tl_lock_dir="/tmp/sub-translate-locks"
  mkdir -p "$_tl_lock_dir"
  local _tl_lock_hash
  _tl_lock_hash="$(printf '%s' "$mkv_file" | sha1sum | cut -d' ' -f1)"
  local _tl_lock_path="${_tl_lock_dir}/${_tl_lock_hash}.lock"
  exec 8>"$_tl_lock_path"
  if ! flock -n 8; then
    log "TRANSLATE_INLINE: per-file lock held by another worker, skipping: $(basename "$mkv_file")"
    exec 8>&-
    return 1
  fi
  PYTHONPATH=/config/berenstuff/automation/scripts python3 \
    -m translation.translator translate --file "$mkv_file" </dev/null 2>&1 | while IFS= read -r line; do
    log "TRANSLATE_INLINE: $line"
  done
  flock -u 8
  exec 8>&-
  # Check if the SRT was created
  local dir name_stem
  dir="$(dirname "$mkv_file")"
  name_stem="$(basename "${mkv_file%.*}")"
  local translated_srt=""
  while IFS= read -r candidate; do
    [[ -z "$candidate" ]] && continue
    local cand_lang
    cand_lang="$(extract_srt_lang "$(basename "$candidate")" "$name_stem")"
    cand_lang="$(normalize_track_lang "$cand_lang")"
    if [[ "$cand_lang" == "$target_lang" ]]; then
      translated_srt="$candidate"
      break
    fi
  done < <(find "$dir" -maxdepth 1 -name "${name_stem}.${target_lang}*.srt" -type f -mmin -2 2>/dev/null | sort -r)
  if [[ -n "$translated_srt" ]]; then
    log "TRANSLATE_INLINE: SUCCESS created $(basename "$translated_srt")"
    return 0
  fi
  log "TRANSLATE_INLINE: no SRT produced for lang=$target_lang"
  return 1
}

# ---------------------------------------------------------------------------
# DeepL quota check — returns 0 if credits available, 1 if exhausted
# ---------------------------------------------------------------------------

check_deepl_quota() {
  # Returns: 0 = credits available, 1 = confirmed exhausted, 2 = unreachable/unknown
  [[ -z "${DEEPL_API_KEY:-}" ]] && return 2
  local api_host="api.deepl.com"
  [[ "${DEEPL_API_KEY}" == *":fx" ]] && api_host="api-free.deepl.com"
  local usage_json
  usage_json="$(curl -sS -m 10 --connect-timeout 5 \
    -H "Authorization: DeepL-Auth-Key ${DEEPL_API_KEY}" \
    "https://${api_host}/v2/usage" </dev/null 2>/dev/null)" || return 2
  local char_count char_limit
  char_count="$(jq -r '.character_count // 0' <<<"$usage_json")"
  char_limit="$(jq -r '.character_limit // 0' <<<"$usage_json")"
  [[ "$char_limit" -eq 0 ]] && return 1
  # Exhausted if >= 95% used
  local pct
  pct="$(awk "BEGIN { printf \"%d\", ($char_count / $char_limit) * 100 }")"
  if [[ "$pct" -ge 95 ]]; then
    log "DEEPL_QUOTA: exhausted (${char_count}/${char_limit} = ${pct}%)"
    return 1
  fi
  debug "DEEPL_QUOTA: available (${char_count}/${char_limit} = ${pct}%)"
  return 0
}


run_upgrade_retries() {
  local state_db="$1"
  local retried=0 resolved=0 abandoned=0

  while IFS=$'\t' read -r nu_path nu_lang nu_forced _ _ nu_retries; do
    [[ -z "$nu_path" ]] && continue
    [[ ! -f "$nu_path" ]] && { resolve_needs_upgrade "$state_db" "$nu_path" "$nu_lang" "$nu_forced"; continue; }

    # Check if file already has a good sub for this lang now
    local nu_duration
    nu_duration="$(get_video_duration "$nu_path")"

    touch_upgrade_retry "$state_db" "$nu_path" "$nu_lang" "$nu_forced"

    # Dead-end sources cap early; provider-cycle sources get more attempts.
    local nu_source
    nu_source="$(sqm_db "$state_db" "SELECT source FROM needs_upgrade WHERE file_path='$(sql_escape "$nu_path")' AND lang='$(sql_escape "$nu_lang")' AND forced=$nu_forced;" 2>/dev/null || echo "external")"
    local effective_max
    if [[ "$nu_source" == "embedded_desync" || "$nu_source" == "missing" ]]; then
      effective_max="$UPGRADE_MAX_RETRY"
    else
      effective_max="$UPGRADE_PROVIDER_MAX_RETRY"
    fi

    if [[ "$nu_retries" -ge "$effective_max" ]]; then
      log "UPGRADE_ABANDONED: max retries ($effective_max) reached lang=$nu_lang source=$nu_source: $(basename "$nu_path")"
      sqm_db "$state_db" "UPDATE needs_upgrade SET source='accepted_fallback', resolved_ts=$(date +%s) WHERE file_path='$(sql_escape "$nu_path")' AND lang='$(sql_escape "$nu_lang")' AND forced=$nu_forced;" 2>/dev/null || true
      abandoned=$((abandoned + 1))
      continue
    fi

    # Stage 2: Discord alert at retry threshold
    if [[ "$nu_retries" -eq "$((UPGRADE_ALERT_RETRY - 1))" ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
      local _alert_fields
      _alert_fields="$(jq -nc \
        --arg file "$(basename "$nu_path")" \
        --arg lang "$nu_lang" \
        --arg source "$nu_source" \
        --arg retries "$((nu_retries + 1))/$effective_max" \
        '[
          {name: "📁 File",     value: $file,    inline: false},
          {name: "🌐 Language", value: $lang,    inline: true},
          {name: "📋 Source",   value: $source,  inline: true},
          {name: "🔄 Retries",  value: $retries, inline: true}
        ]')"
      notify_discord_embed "🔴 Subtitle Upgrade Stalled" \
        "May need manual intervention" \
        15158332 "Subtitle Quality Manager" "$_alert_fields" \
        || log "WARN: Discord dead-end alert failed (non-fatal)"
    fi

    # Try providers
    if [[ -n "$BAZARR_API_KEY" ]] && try_providers_for_lang "$nu_path" "$nu_lang" "$nu_duration"; then
      log "UPGRADE_RESOLVED: via providers lang=$nu_lang: $(basename "$nu_path")"
      resolve_needs_upgrade "$state_db" "$nu_path" "$nu_lang" "$nu_forced"
      resolved=$((resolved + 1))
      continue
    fi

    # Try translation
    if try_translate_inline "$nu_path" "$nu_lang"; then
      log "UPGRADE_RESOLVED: via translation lang=$nu_lang: $(basename "$nu_path")"
      resolve_needs_upgrade "$state_db" "$nu_path" "$nu_lang" "$nu_forced"
      resolved=$((resolved + 1))
      continue
    fi

    retried=$((retried + 1))
    log "UPGRADE_RETRY: still no replacement lang=$nu_lang retry=$((nu_retries + 1)): $(basename "$nu_path")"
  done < <(drain_upgrade_candidates "$state_db" 86400 30 500)

  [[ $((retried + resolved + abandoned)) -gt 0 ]] && log "upgrade-retries: retried=$retried resolved=$resolved abandoned=$abandoned"
}

cmd_auto_maintain() {
  load_streaming_candidates
  load_guard "auto-maintain" || return 0
  log "auto-maintain: path=$PATH_PREFIX_ROOT since=$SINCE_MINUTES keep_profile_langs=$KEEP_PROFILE_LANGS bloat_threshold=$BLOAT_THRESHOLD dry_run=$DRY_RUN"

  local state_db="$STATE_DIR/subtitle_quality_state.db"
  init_state_db "$state_db"
  init_watermark_patterns "$state_db"
  _CACHED_WATERMARK_PATTERNS="$(load_watermark_patterns "$state_db")"
  [[ -z "$_CACHED_WATERMARK_PATTERNS" ]] && _CACHED_WATERMARK_PATTERNS="$WATERMARK_PATTERNS"
  _DEEPL_QUOTA_CACHED=""  # Reset per-run cache

  # Run upgrade retries in full mode only (daily 1 AM)
  if [[ "$SINCE_MINUTES" -eq 0 ]]; then
    run_upgrade_retries "$state_db"
  fi

  # Drain pending work queue (files enqueued by import hook / dedupe)
  local -A pending_set=()
  local pending_count=0
  while IFS= read -r pf; do
    [[ -z "$pf" ]] && continue
    [[ -f "$pf" ]] || continue
    pending_set["$pf"]=1
    pending_count=$((pending_count + 1))
  done < <(drain_pending "$state_db")
  [[ "$pending_count" -gt 0 ]] && log "auto-maintain: drained $pending_count file(s) from pending queue"

  local total_files=0 muxed_files=0 muxed_tracks=0 stripped_files=0 stripped_tracks=0
  local skipped_converter=0 skipped_playback=0 skipped_streaming=0 warned=0 deepl_deferred=0 cleaned_nonprofile=0 extracted_nonprofile=0
  local exhausted_count=0 exhausted_summary=""
  local -a modified_dirs=()
  local -A bazarr_rescanned=()

  # Cleanup orphaned temp files from interrupted operations (older than 1 hour)
  local stale_count=0
  while IFS= read -r stale_tmp; do
    [[ -z "$stale_tmp" ]] && continue
    rm -f "$stale_tmp"
    stale_count=$((stale_count + 1))
  done < <(find "$PATH_PREFIX_ROOT" -type f \( -name "*.striptmp.*" -o -name ".*.striptmp.*" -o -name "*.bloattmp.*" -o -name ".*.bloattmp.*" -o -name "*.subtmp.*" -o -name ".*.subtmp.*" -o -name "*.collisiontmp.*" -o -name ".*.collisiontmp.*" \) -mmin +60 2>/dev/null)
  [[ "$stale_count" -gt 0 ]] && log "CLEANUP $stale_count orphaned temp file(s)"

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
      # Allow through if file is in pending queue (regardless of mtime)
      [[ "$mkv_age_ok" -eq 0 && -z "$recent_srt" && -z "${pending_set[$mkv_file]+x}" ]] && continue
    fi

    mkv_files+=("$mkv_file")
  done < <(find "$PATH_PREFIX_ROOT" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) ! -name "*tmp.*" 2>/dev/null | sort)

  # Add pending files that are outside PATH_PREFIX_ROOT (edge case) or weren't found by the scan
  local -A mkv_seen=()
  for f in "${mkv_files[@]}"; do mkv_seen["$f"]=1; done
  for pf in "${!pending_set[@]}"; do
    [[ -z "${mkv_seen[$pf]+x}" ]] && [[ -f "$pf" ]] && mkv_files+=("$pf")
  done

  log "auto-maintain: found ${#mkv_files[@]} candidate files (${pending_count} from pending queue)"

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
      local current_mtime stored_mtime stored_audit_ts
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      local stored_row
      stored_row="$(sqm_db "$state_db" -separator '|' "SELECT mtime, last_audit_ts FROM file_audits WHERE file_path='$(sql_escape "$mkv_file")';" 2>/dev/null || true)"
      stored_mtime="${stored_row%%|*}"
      stored_audit_ts="${stored_row##*|}"
      [[ -z "$stored_mtime" ]] && stored_mtime=0
      [[ -z "$stored_audit_ts" ]] && stored_audit_ts=0
      if [[ "$current_mtime" -eq "$stored_mtime" ]] && [[ "$stored_mtime" -gt 0 ]]; then
        # MKV unchanged — but check if any SRT next to it is newer than last audit
        local srt_max_mtime=0 _srt_mt
        while IFS= read -r _srt_f; do
          [[ -z "$_srt_f" ]] && continue
          _srt_mt="$(stat -c %Y "$_srt_f" 2>/dev/null || echo 0)"
          [[ "$_srt_mt" -gt "$srt_max_mtime" ]] && srt_max_mtime="$_srt_mt"
        done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)
        if [[ "$srt_max_mtime" -le "$stored_audit_ts" ]]; then
          debug "SKIP (unchanged): $basename"
          continue
        fi
        debug "RE-AUDIT (new SRT since last audit): $basename"
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

          # Profile tracks: never strip
          if lang_in_set "$p0_lang" "$am_profile_set"; then
            continue
          fi

          local p0_norm_lang
          p0_norm_lang="$(normalize_track_lang "$p0_lang")"

          # Build output filename: {stem}.{lang}[.forced].srt
          local p0_out_name="${name_stem}.${p0_norm_lang}"
          [[ "$p0_forced" -eq 1 ]] && p0_out_name+=".forced"
          p0_out_name+=".srt"
          local p0_out="${dir}/${p0_out_name}"
          local p0_detected_lang=""

          # Extract text-based subs to external SRT (bitmap codecs can't be extracted)
          if is_text_sub_codec "$p0_codec"; then
            if [[ ! -f "$p0_out" ]]; then
              if [[ "$DRY_RUN" -eq 0 ]]; then
                if ffmpeg -v quiet -i "$mkv_file" -map "0:${p0_idx}" -f srt "$p0_out" </dev/null 2>/dev/null && [[ -s "$p0_out" ]]; then
                  log "EXTRACTED non-profile idx=${p0_idx} lang=${p0_norm_lang} → ${p0_out_name}: $basename"
                  extracted_nonprofile=$((extracted_nonprofile + 1))

                  # Detect actual language for 'und' tracks and rename
                  if [[ "$p0_norm_lang" == "und" ]]; then
                    local detected_lang
                    if detected_lang="$(detect_srt_language "$p0_out" "${DEEPL_API_KEY:-}")"; then
                      detected_lang="$(normalize_track_lang "$detected_lang")"
                      p0_detected_lang="$detected_lang"
                      local renamed="${name_stem}.${detected_lang}"
                      [[ "$p0_forced" -eq 1 ]] && renamed+=".forced"
                      renamed+=".srt"
                      local renamed_path="${dir}/${renamed}"
                      if [[ ! -f "$renamed_path" ]]; then
                        mv "$p0_out" "$renamed_path"
                        log "DETECTED und → ${detected_lang} → renamed to ${renamed}: $basename"
                      else
                        debug "SKIP rename (${renamed} already exists): $basename"
                      fi
                    else
                      log "WARN: language detection failed for und track (kept as und): $basename"
                    fi
                  fi
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

          # Mark for stripping — but protect und tracks that turn out to be profile languages
          if [[ "$p0_norm_lang" == "und" && -n "${p0_detected_lang:-}" ]] && lang_in_set "$p0_detected_lang" "$am_profile_set"; then
            # und track detected as a profile language — do NOT strip
            log "PROTECT und→${p0_detected_lang} (profile language, keeping embedded): idx=${p0_idx}: $basename"
          elif [[ "$p0_norm_lang" == "und" && -z "${p0_detected_lang:-}" ]]; then
            # und track with failed detection — do NOT strip (safe fallback)
            log "PROTECT und (detection failed, keeping embedded): idx=${p0_idx}: $basename"
          else
            p0_strip_indices+=("$p0_idx")
          fi
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
            local p0_strip_tmp="${mkv_file%/*}/.${mkv_file##*/}"
            p0_strip_tmp="${p0_strip_tmp%.*}.bloattmp.${ext_p0}"
            if "${p0_strip_cmd[@]}" "$p0_strip_tmp" </dev/null 2>/dev/null && [[ -s "$p0_strip_tmp" ]] && validate_streams_match "$mkv_file" "$p0_strip_tmp" "phase0_strip"; then
              log_mkv_rewrite_audit "$mkv_file"
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

    # --- Phase 0.5: Consolidated 1-best-per-lang enforcement ---
    local -a eopl_strip_indices=()
    # shellcheck disable=SC2034
    declare -A eopl_kept_langs=()
    if enforce_one_per_lang "$mkv_file" "${duration%.*}" "$DRY_RUN" eopl_strip_indices eopl_kept_langs; then
      if [[ ${#eopl_strip_indices[@]} -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
        if strip_embedded_by_indices "$mkv_file" eopl_strip_indices; then
          stripped_tracks=$((stripped_tracks + ${#eopl_strip_indices[@]}))
          stripped_files=$((stripped_files + 1))
          file_modified=1
          log "PHASE_0.5: stripped ${#eopl_strip_indices[@]} loser embedded track(s): $basename"
        fi
      elif [[ ${#eopl_strip_indices[@]} -gt 0 ]]; then
        log "[DRY-RUN] PHASE_0.5: would strip ${#eopl_strip_indices[@]} loser embedded track(s): $basename"
        stripped_tracks=$((stripped_tracks + ${#eopl_strip_indices[@]}))
      fi
    fi

    # --- Phase 0.75: Sync drift validation for embedded profile-language tracks ---
    if [[ "$KEEP_PROFILE_LANGS" -eq 1 ]] && [[ -n "$am_profile_set" ]]; then
      local emb_json_p075
      emb_json_p075="$(get_embedded_subs "$mkv_file")"
      local emb_count_p075
      emb_count_p075="$(jq 'length' <<<"$emb_json_p075")"

      if [[ "$emb_count_p075" -ge 2 ]]; then
        # Determine drift anchor (first profile lang with text track)
        local drift_anchor_p075=""
        drift_anchor_p075="$(get_drift_anchor_lang "$emb_json_p075" "$am_profile_langs")"

        for ((i=0; i<emb_count_p075; i++)); do
          local p075_lang p075_codec
          p075_lang="$(jq -r ".[$i].tags.language // \"und\"" <<<"$emb_json_p075")"
          p075_codec="$(jq -r ".[$i].codec_name" <<<"$emb_json_p075")"
          is_text_sub_codec "$p075_codec" || continue
          local p075_norm
          p075_norm="$(normalize_track_lang "$p075_lang")"
          # Only check profile-language tracks, skip the anchor
          lang_in_set "$p075_norm" "$am_profile_set" || continue
          [[ "$p075_norm" == "$drift_anchor_p075" ]] && continue

          local drift_result drift_max drift_rate drift_ref
          drift_result="$(check_sync_drift "$mkv_file" "$p075_norm" "$duration" "$am_profile_langs")"
          read -r drift_max drift_rate drift_ref <<< "$drift_result"

          case "$drift_rate" in
            BAD)
              log "PHASE_0.75: embedded desync detected lang=$p075_norm drift=${drift_max}s ref=$drift_ref: $basename"
              if [[ "$DRY_RUN" -eq 0 ]]; then
                upsert_needs_upgrade "$state_db" "$mkv_file" "$p075_norm" 0 "BAD" 0 "embedded_desync"
              fi
              ;;
            WARN)
              log "PHASE_0.75: embedded drift warning lang=$p075_norm drift=${drift_max}s ref=$drift_ref: $basename"
              ;;
            GOOD)
              debug "PHASE_0.75: embedded sync OK lang=$p075_norm drift=${drift_max}s ref=$drift_ref: $basename"
              ;;
            SKIP)
              debug "PHASE_0.75: no reference for drift check lang=$p075_norm: $basename"
              ;;
          esac
        done
      fi
    fi

    # --- Phase 1: Audit & mux external SRTs ---
    # Build embedded language map for collision detection
    declare -A embedded_lang_idx_p1=()
    declare -A embedded_lang_codec_p1=()
    build_embedded_lang_map "$mkv_file" embedded_lang_idx_p1 embedded_lang_codec_p1
    local -a premux_strip_indices_p1=()

    local -a good_srts=() good_langs=()
    while IFS= read -r srt_file; do
      [[ -z "$srt_file" ]] && continue
      local srt_basename ext_lang
      srt_basename="$(basename "$srt_file")"
      ext_lang="$(extract_srt_lang "$srt_basename" "$name_stem")"
      [[ -z "$ext_lang" ]] && ext_lang="und"

      local analysis cues first_sec last_sec mojibake watermarks rating
      analysis="$(analyze_srt_file "$srt_file")"
      read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
      rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"

      # Normalize once for all branches
      local ext_lang_norm ext_forced_num=0
      ext_lang_norm="$(normalize_track_lang "$ext_lang")"
      [[ "$srt_basename" == *.forced.srt ]] && ext_forced_num=1

      # === content-language check (added 2026-04-27) ===
      # Reject SRTs whose actual content language doesn't match the lang tag.
      # Gated by SUB_CONTENT_LANG_CHECK env var (default on). On WRONG_LANG, force BAD verdict.
      if [[ "${SUB_CONTENT_LANG_CHECK:-1}" != "0" ]] && [[ "$ext_lang_norm" != "und" ]]; then
        local _lang_check
        _lang_check="$(check_external_srt_lang "$srt_file" "$ext_lang_norm" 2>/dev/null || true)"
        if [[ "$_lang_check" == WRONG_LANG* ]]; then
          log "WRONG_CONTENT_LANG tag=$ext_lang_norm $_lang_check: $srt_basename"
          rating="BAD"
        fi
      fi


      case "$rating" in
        GOOD)
          # Skip translation-source extractions (embedded original stays intact)
      # === prefer-embedded-when-clean (added 2026-04-27) ===
      # If a same-language embedded track exists AND its content is langdetect-OK,
      # skip the mux and drop the redundant external sidecar. Embedded wins by default.
      if [[ "${SUB_PREFER_EMBEDDED:-1}" != "0" ]] \
         && [[ -n "${embedded_lang_idx_p1[$ext_lang_norm]:-}" ]] \
         && [[ "$ext_lang_norm" != "und" ]]; then
        local _emb_track_id _emb_sub_idx _emb_check
        _emb_track_id="${embedded_lang_idx_p1[$ext_lang_norm]}"
        _emb_sub_idx="$(mkvmerge_track_id_to_sub_index "$mkv_file" "$_emb_track_id" 2>/dev/null || true)"
        if [[ -n "$_emb_sub_idx" ]]; then
          _emb_check="$(check_embedded_track_lang "$mkv_file" "$_emb_sub_idx" "$ext_lang_norm" 2>/dev/null || true)"
          if [[ "$_emb_check" == OK* ]]; then
            log "PREFER_EMBEDDED: clean embedded $ext_lang_norm exists, removing redundant external: $srt_basename"
            if [[ "$DRY_RUN" -eq 0 ]]; then
              rm -f "$srt_file"
            fi
            continue
          fi
        fi
      fi

          if [[ -f "${srt_file}.deepl-source" ]]; then
            debug "SKIP mux (translation source extraction): $srt_basename"
            continue
          fi
          # Check for translation marker (.deepl, .gemini, or .gtranslate grace period)
          local translation_marker=""
          if [[ -f "${srt_file}.deepl" ]]; then
            translation_marker="${srt_file}.deepl"
          elif [[ -f "${srt_file}.gemini" ]]; then
            translation_marker="${srt_file}.gemini"
          elif [[ -f "${srt_file}.gtranslate" ]]; then
            translation_marker="${srt_file}.gtranslate"
          fi
          if [[ -n "$translation_marker" ]]; then
            local marker_mtime srt_mtime marker_name
            marker_name="${translation_marker##*.}"
            marker_mtime="$(stat -c %Y "$translation_marker" 2>/dev/null || echo 0)"
            srt_mtime="$(stat -c %Y "$srt_file" 2>/dev/null || echo 0)"
            # If SRT was replaced after marker created, human sub found — delete marker and mux
            if [[ "$srt_mtime" -gt "$marker_mtime" ]]; then
              rm -f "$translation_marker"
              log "${marker_name} marker removed (SRT replaced by human sub): $srt_basename"
            else
              # Grace period: 7 days (604800 seconds)
              local now marker_age
              now="$(date +%s)"
              marker_age=$(( now - marker_mtime ))
              if [[ "$marker_age" -lt 604800 ]]; then
                local days_left=$(( (604800 - marker_age) / 86400 ))
                debug "SKIP mux (${marker_name} grace ${days_left}d remaining): $srt_basename"
                deepl_deferred=$((deepl_deferred + 1))
                continue
              else
                # Grace expired — mux the translation
                rm -f "$translation_marker"
                log "${marker_name} grace expired, muxing: $srt_basename"
              fi
            fi
          fi
          # Profile filter: skip non-profile languages (they're kept as DeepL sources, not muxed)
          if [[ -n "$am_profile_set" ]] && ! lang_in_set "$ext_lang_norm" "$am_profile_set"; then
            debug "SKIP non-profile external SRT: $srt_basename (lang=$ext_lang_norm, profile=$am_profile_langs)"
            continue
          fi

          # Pre-mux collision check: only strip embedded if external is actually better
          if [[ -n "${embedded_lang_idx_p1[$ext_lang_norm]:-}" ]]; then
            if check_embedded_collision "$mkv_file" "$ext_lang_norm" \
                 "${embedded_lang_idx_p1[$ext_lang_norm]}" "${embedded_lang_codec_p1[$ext_lang_norm]:-unknown}" \
                 "$rating" "$cues" "$duration" "$srt_basename"; then
              premux_strip_indices_p1+=("${embedded_lang_idx_p1[$ext_lang_norm]}")
              log "COLLISION lang=$ext_lang_norm: will replace embedded idx=${embedded_lang_idx_p1[$ext_lang_norm]} with external SRT: $srt_basename"
            fi
            unset "embedded_lang_idx_p1[$ext_lang_norm]"
          fi

          good_srts+=("$srt_file")
          good_langs+=("$ext_lang")
          ;;
        WARN)
          warned=$((warned + 1))
          # Mark WARN subs for upgrade — usable but subpar
          if [[ "$DRY_RUN" -eq 0 ]]; then
            local warn_score
            warn_score="$(subtitle_quality_score "$srt_file" "${duration%.*}" "$ext_forced_num")"
            upsert_needs_upgrade "$state_db" "$mkv_file" "$ext_lang_norm" "$ext_forced_num" "WARN" "$warn_score" "external"
            log "MARK_WARN: usable but subpar, marked for upgrade lang=$ext_lang_norm: $basename"
          fi
          ;;
        BAD)
          if [[ "$DRY_RUN" -eq 1 ]]; then
            log "[DRY-RUN] BAD external (would keep, needs manual replacement): $srt_basename"
          else
            log "BAD external (kept, needs manual replacement): $srt_basename"
          fi
          exhausted_count=$((exhausted_count + 1))
          exhausted_summary="${exhausted_summary}${basename} [${ext_lang_norm}] — bad sub, kept for review\n"
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
            map_args+=(-metadata:s:"${abs_idx}" "language=${lang}")
          else
            local metadata_idx=$((existing_sub_count + i))
            map_args+=(-metadata:s:s:"${metadata_idx}" "language=${lang}")
          fi
        done

        local ext="${mkv_file##*.}"
        local sub_codec="copy"
        [[ "${ext,,}" == "mp4" || "${ext,,}" == "m4v" ]] && sub_codec="mov_text"
        local tmp_out="${mkv_file%/*}/.${mkv_file##*/}"
        tmp_out="${tmp_out%.*}.subtmp.${ext}"
        if "${ffmpeg_cmd[@]}" "${map_args[@]}" -c:v copy -c:a copy -c:s "$sub_codec" "$tmp_out" </dev/null 2>/dev/null; then
          local new_sub_count expected
          new_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$tmp_out" 2>/dev/null | jq '.streams | length')"
          expected=$((existing_sub_count + ${#good_srts[@]}))

          if [[ "$new_sub_count" -eq "$expected" ]]; then
            if ! validate_streams_match "$mkv_file" "$tmp_out" "phase1_mux"; then
              rm -f "$tmp_out"
              continue
            fi
            log_mkv_rewrite_audit "$mkv_file"
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
              local p1_strip_tmp="${mkv_file%/*}/.${mkv_file##*/}"
              p1_strip_tmp="${p1_strip_tmp%.*}.collisiontmp.${p1_strip_ext}"
              if "${p1_strip_cmd[@]}" "$p1_strip_tmp" </dev/null 2>/dev/null && [[ -s "$p1_strip_tmp" ]] && validate_streams_match "$mkv_file" "$p1_strip_tmp" "phase1_collision_strip"; then
                log_mkv_rewrite_audit "$mkv_file"
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

    # --- Phase 1.75: Translate from non-profile sources before cleanup ---
    # For each profile language still missing, find best non-profile external SRT
    # as translation source and run DeepL synchronously (before Phase 1.5 deletes them)
    if [[ -n "$am_profile_set" ]]; then
      # Re-check which profile langs are still missing
      local emb_json_p175
      emb_json_p175="$(get_embedded_subs "$mkv_file")"
      declare -A emb_langs_p175=()
      while IFS= read -r el; do
        [[ -z "$el" ]] && continue
        el="$(normalize_track_lang "$el")"
        emb_langs_p175["$el"]=1
      done < <(jq -r '.[].tags.language // "und"' <<<"$emb_json_p175")

      declare -A ext_langs_p175=()
      while IFS= read -r remaining_srt; do
        [[ -z "$remaining_srt" ]] && continue
        local rb_p175 el_p175
        rb_p175="$(basename "$remaining_srt")"
        el_p175="$(extract_srt_lang "$rb_p175" "$name_stem")"
        [[ -z "$el_p175" ]] && el_p175="und"
        el_p175="$(normalize_track_lang "$el_p175")"
        ext_langs_p175["$el_p175"]=1
      done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)

      IFS=',' read -ra _profile_codes_p175 <<< "$am_profile_langs"
      for pc in "${_profile_codes_p175[@]}"; do
        local pc_norm_p175
        pc_norm_p175="$(normalize_track_lang "$pc")"
        # Skip if already have this lang (embedded or external)
        [[ -n "${emb_langs_p175[$pc_norm_p175]:-}" ]] && continue
        [[ -n "${ext_langs_p175[$pc_norm_p175]:-}" ]] && continue

        # Find best non-profile external SRT as translation source
        local best_np_srt="" best_np_score=-999999
        while IFS= read -r np_srt; do
          [[ -z "$np_srt" ]] && continue
          local np_base np_lang_p175
          np_base="$(basename "$np_srt")"
          np_lang_p175="$(extract_srt_lang "$np_base" "$name_stem")"
          [[ -z "$np_lang_p175" ]] && np_lang_p175="und"
          np_lang_p175="$(normalize_track_lang "$np_lang_p175")"
          # Only use non-profile SRTs as source
          lang_in_set "$np_lang_p175" "$am_profile_set" && continue
          local np_score
          np_score="$(subtitle_quality_score "$np_srt" "${duration%.*}" 0)"
          if [[ "$np_score" -gt "$best_np_score" ]]; then
            best_np_score="$np_score"
            best_np_srt="$np_srt"
          fi
        done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)

        if [[ -n "$best_np_srt" ]] && [[ "$DRY_RUN" -eq 0 ]]; then
          log "PHASE_1.75: translating from non-profile source $(basename "$best_np_srt") for missing lang=$pc_norm_p175: $basename"
          if try_translate_inline "$mkv_file" "$pc_norm_p175"; then
            log "PHASE_1.75: SUCCESS translated lang=$pc_norm_p175: $basename"
            file_modified=1
          fi
        elif [[ -n "$best_np_srt" ]]; then
          log "[DRY-RUN] PHASE_1.75: would translate from $(basename "$best_np_srt") for lang=$pc_norm_p175: $basename"
        fi
      done
    fi

    # --- Phase 1.5: Clean up non-profile external SRTs ---
    # Once all profile languages are satisfied (embedded or external), non-profile
    # SRTs are no longer needed as translation sources — safe to remove.
    if [[ -n "$am_profile_set" ]]; then
      # Check if ALL profile languages have subtitles (embedded or external)
      # Re-read embedded after potential mux
      local emb_json_p15
      emb_json_p15="$(get_embedded_subs "$mkv_file")"

      declare -A emb_langs_p15=()
      while IFS= read -r el; do
        [[ -z "$el" ]] && continue
        el="$(normalize_track_lang "$el")"
        emb_langs_p15["$el"]=1
      done < <(jq -r '.[].tags.language // "und"' <<<"$emb_json_p15")

      # Build set of remaining external SRT languages
      declare -A ext_langs_p15=()
      while IFS= read -r remaining_srt; do
        [[ -z "$remaining_srt" ]] && continue
        local rb el_p15
        rb="$(basename "$remaining_srt")"
        el_p15="$(extract_srt_lang "$rb" "$name_stem")"
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
          el_p15="$(extract_srt_lang "$rb" "$name_stem")"
          [[ -z "$el_p15" ]] && el_p15="und"
          local el_norm_p15
          el_norm_p15="$(normalize_track_lang "$el_p15")"
          if ! lang_in_set "$el_norm_p15" "$am_profile_set"; then
            if [[ "$DRY_RUN" -eq 0 ]]; then
              rm -f "$remaining_srt" "${remaining_srt}.deepl" "${remaining_srt}.gemini" "${remaining_srt}.gtranslate"
            fi
            log "CLEANUP non-profile external SRT: $rb (lang=$el_norm_p15, profile=$am_profile_langs)"
            cleaned_nonprofile=$((cleaned_nonprofile + 1))
            file_modified=1
          elif [[ -f "${remaining_srt}.deepl-source" ]]; then
            if [[ "$DRY_RUN" -eq 0 ]]; then
              rm -f "$remaining_srt" "${remaining_srt}.deepl-source"
            fi
            log "CLEANUP translation source: $rb (all profile langs satisfied)"
            cleaned_nonprofile=$((cleaned_nonprofile + 1))
            file_modified=1
          fi
        done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)
      else
        debug "SKIP cleanup (profile not fully satisfied): $basename (profile=$am_profile_langs)"
      fi
    fi

    # --- Phase 3: Provider cycle + translation for completely missing profile languages ---
    if [[ -n "$am_profile_set" ]]; then
      # Re-read current state — necessary because Phase 1/1.5 may have muxed/stripped tracks
      local emb_json_now
      emb_json_now="$(get_embedded_subs "$mkv_file")"

      declare -A emb_langs_now=()
      while IFS= read -r el; do
        [[ -z "$el" ]] && continue
        el="$(normalize_track_lang "$el")"
        emb_langs_now["$el"]=1
      done < <(jq -r '.[].tags.language // "und"' <<<"$emb_json_now")

      # Re-find external SRTs — necessary because Phase 1.5 may have deleted some (muxed them in)
      declare -A ext_langs_now=()
      while IFS= read -r remaining_srt; do
        [[ -z "$remaining_srt" ]] && continue
        local rb_now el_now
        rb_now="$(basename "$remaining_srt")"
        el_now="$(extract_srt_lang "$rb_now" "$name_stem")"
        [[ -z "$el_now" ]] && el_now="und"
        el_now="$(normalize_track_lang "$el_now")"
        ext_langs_now["$el_now"]=1
      done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)

      local pc_norm
      IFS=',' read -ra _profile_codes <<< "$am_profile_langs"
      for pc in "${_profile_codes[@]}"; do
        pc_norm="$(normalize_track_lang "$pc")"
        if [[ -z "${emb_langs_now[$pc_norm]:-}" ]] && [[ -z "${ext_langs_now[$pc_norm]:-}" ]]; then
          # This profile language is completely missing — try providers first
          if try_providers_for_lang "$mkv_file" "$pc_norm" "$duration"; then
            log "PROVIDER_CYCLE: found sub for missing profile lang=$pc_norm: $basename"
            continue
          fi
          # Providers failed — try translation from any available profile-language sub
          if try_translate_inline "$mkv_file" "$pc_norm"; then
            log "TRANSLATE: filled missing profile lang=$pc_norm via translation: $basename"
            continue
          fi
          log "EXHAUSTED: no provider or translation for missing profile lang=$pc_norm: $basename"
          exhausted_count=$((exhausted_count + 1))
          exhausted_summary="${exhausted_summary}${basename} [${pc_norm}] — no provider or translation found\n"
        fi
      done
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
          local strip_tmp="${mkv_file%/*}/.${mkv_file##*/}"
          strip_tmp="${strip_tmp%.*}.striptmp.${ext}"
          if "${strip_cmd[@]}" "$strip_tmp" </dev/null 2>/dev/null && [[ -s "$strip_tmp" ]] && validate_streams_match "$mkv_file" "$strip_tmp" "phase2_dedup_strip"; then
            log_mkv_rewrite_audit "$mkv_file"
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

      # Track series/movie dirs needing Bazarr rescan (fired after all files processed)
      if [[ -n "$BAZARR_API_KEY" ]]; then
        local rescan_key
        if is_tv_path "$mkv_file"; then
          rescan_key="$(echo "$mkv_file" | sed 's|/Season.*||' | sed 's|/$||')"
        else
          rescan_key="$(dirname "$mkv_file")"
        fi
        bazarr_rescanned["$rescan_key"]="$mkv_file"
      fi
    fi

    # Update state DB (full mode only)
    if [[ "$SINCE_MINUTES" -eq 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
      local current_mtime action_val="none"
      [[ "$muxed_files" -gt 0 ]] && [[ "$file_modified" -eq 1 ]] && action_val="muxed"
      [[ "$stripped_files" -gt 0 ]] && [[ "$file_modified" -eq 1 ]] && action_val="stripped"
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      sqm_db "$state_db" "INSERT OR REPLACE INTO file_audits (file_path, mtime, last_audit_ts, action_taken) VALUES ('$(sql_escape "$mkv_file")', $current_mtime, $(date +%s), '$action_val');" 2>/dev/null || true
    fi
  done

  # Deferred Bazarr scan-disk (after all files processed, so Bazarr sees final state)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ ${#bazarr_rescanned[@]} -gt 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    log "firing deferred Bazarr scan-disk for ${#bazarr_rescanned[@]} dir(s)"
    for rescan_key in "${!bazarr_rescanned[@]}"; do
      bazarr_rescan_for_file "${bazarr_rescanned[$rescan_key]}" "$BAZARR_DB" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed for $rescan_key"
    done
  fi

  log "auto-maintain done: files=$total_files muxed=$muxed_files($muxed_tracks tracks) stripped=$stripped_files($stripped_tracks tracks) extracted_nonprofile=$extracted_nonprofile cleaned_nonprofile=$cleaned_nonprofile warned=$warned exhausted=$exhausted_count skipped_converter=$skipped_converter skipped_playback=$skipped_playback skipped_streaming=$skipped_streaming"

  # Discord notification for files needing manual subtitle intervention
  if [[ "$exhausted_count" -gt 0 ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
    local _ex_fields _ex_list
    _ex_list="$(printf '%b' "$exhausted_summary" | head -20)"
    _ex_fields="$(jq -nc \
      --arg count "$exhausted_count" \
      --arg files "$_ex_list" \
      '[
        {name: "📋 Files needing subs", value: $files, inline: false},
        {name: "🔢 Count",              value: $count,  inline: true}
      ]')"
    notify_discord_embed "🔴 Subtitles Needed" \
      "Manual subtitle download required for **${exhausted_count}** file(s) — no provider or translation could be found" \
      15158332 "Subtitle Quality Manager" "$_ex_fields" \
      || log "WARN: Discord exhausted-subs alert failed (non-fatal)"
  fi

  # Discord notification (non-fatal, only when actions taken)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ $((muxed_files + stripped_files + extracted_nonprofile + cleaned_nonprofile)) -gt 0 ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
    local mode="quick"
    [[ "$SINCE_MINUTES" -eq 0 ]] && mode="full"

    # Build file list (basenames, capped at 15)
    local file_list="" file_count=${#modified_dirs[@]}
    local show_count=$file_count
    [[ "$show_count" -gt 15 ]] && show_count=15
    for ((fi=0; fi<show_count; fi++)); do
      file_list+="• \`$(basename "${modified_dirs[$fi]}")\`\n"
    done
    [[ "$file_count" -gt 15 ]] && file_list+="…and $((file_count - 15)) more\n"

    # Build fields JSON
    local _am_fields="["
    _am_fields+="{\"name\":\"📥 Muxed\",\"value\":\"${muxed_files} file(s), ${muxed_tracks} track(s)\",\"inline\":true},"
    _am_fields+="{\"name\":\"✂️ Stripped\",\"value\":\"${stripped_files} file(s), ${stripped_tracks} track(s)\",\"inline\":true},"
    _am_fields+="{\"name\":\"🔍 Scanned\",\"value\":\"${total_files}\",\"inline\":true}"
    [[ "$extracted_nonprofile" -gt 0 ]] && _am_fields+=",{\"name\":\"📤 Extracted\",\"value\":\"${extracted_nonprofile} track(s)\",\"inline\":true}"
    [[ "$cleaned_nonprofile" -gt 0 ]] && _am_fields+=",{\"name\":\"🧹 Cleaned\",\"value\":\"${cleaned_nonprofile} SRT(s)\",\"inline\":true}"
    [[ "$warned" -gt 0 ]] && _am_fields+=",{\"name\":\"⚠️ WARN Upgrade\",\"value\":\"${warned} SRT(s)\",\"inline\":true}"
    [[ "$deepl_deferred" -gt 0 ]] && _am_fields+=",{\"name\":\"⏳ DeepL Deferred\",\"value\":\"${deepl_deferred}\",\"inline\":true}"
    local _skips=""
    [[ "$skipped_converter" -gt 0 ]] && _skips+="🔄 converter: ${skipped_converter}  "
    [[ "$skipped_playback" -gt 0 ]] && _skips+="▶️ playback: ${skipped_playback}  "
    [[ "$skipped_streaming" -gt 0 ]] && _skips+="📺 streaming: ${skipped_streaming}"
    [[ -n "$_skips" ]] && _am_fields+=",{\"name\":\"⏭️ Skipped\",\"value\":\"${_skips}\",\"inline\":false}"
    if [[ -n "$file_list" ]]; then
      local _fl_rendered
      _fl_rendered="$(printf '%b' "$file_list")"
      _am_fields+=",$(jq -nc --arg v "$_fl_rendered" '{name: "📝 Modified Files", value: $v, inline: false}')"
    fi
    _am_fields+="]"

    local _am_desc
    _am_desc="$(printf '%b' "Scanned **${total_files}** files · **$((muxed_files + stripped_files))** modified")"

    local payload
    payload="$(jq -nc --arg title "📥 Subtitle Auto-Maintain ($mode)" \
      --arg desc "$_am_desc" \
      --argjson fields "$_am_fields" \
      --arg footer "Scanned $total_files · Mode: $mode" \
      --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      '{embeds: [{
        title: $title,
        description: $desc,
        color: 3066993,
        fields: $fields,
        footer: {text: $footer},
        timestamp: $ts
      }]}')"

    curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
      -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 \
      || log "WARN: Discord notification failed (non-fatal)"
  fi
}

cmd_compliance() {
  local state_db="$STATE_DIR/subtitle_quality_state.db"
  init_state_db "$state_db"

  local format="${COMPLIANCE_FORMAT:-text}"
  local verbose="${COMPLIANCE_VERBOSE:-0}"
  local total=0 compliant=0 missing=0 needs_upg=0 no_profile=0
  local -a results=()

  log "compliance: scanning path=$PATH_PREFIX_ROOT format=$format verbose=$verbose"

  while IFS= read -r mkv_file; do
    [[ -z "$mkv_file" ]] && continue
    total=$((total + 1))

    local basename dir name_stem
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    name_stem="${basename%.*}"

    # Resolve Bazarr profile
    local profile_langs=""
    profile_langs="$(resolve_bazarr_profile_langs "$mkv_file" "$BAZARR_DB")" || profile_langs=""
    if [[ -z "$profile_langs" ]]; then
      results+=("[NO_PROFILE] $basename")
      no_profile=$((no_profile + 1))
      continue
    fi
    # Get embedded subs
    local emb_json
    emb_json="$(get_embedded_subs "$mkv_file")"
    declare -A comp_emb_langs=()
    while IFS= read -r el; do
      [[ -z "$el" ]] && continue
      el="$(normalize_track_lang "$el")"
      comp_emb_langs["$el"]=1
    done < <(jq -r '.[].tags.language // "und"' <<<"$emb_json")

    # Get external SRTs
    declare -A comp_ext_langs=()
    while IFS= read -r srt; do
      [[ -z "$srt" ]] && continue
      local sb sl
      sb="$(basename "$srt")"
      sl="$(extract_srt_lang "$sb" "$name_stem")"
      [[ -z "$sl" ]] && sl="und"
      sl="$(normalize_track_lang "$sl")"
      comp_ext_langs["$sl"]=1
    done < <(find "$dir" -maxdepth 1 -name "${name_stem}.*.srt" -type f 2>/dev/null)

    # Check each profile language
    local file_status="OK" file_missing="" file_upgrade=""
    IFS=',' read -ra _pc <<< "$profile_langs"
    for pc in "${_pc[@]}"; do
      local pcn
      pcn="$(normalize_track_lang "$pc")"
      if [[ -n "${comp_emb_langs[$pcn]:-}" ]] || [[ -n "${comp_ext_langs[$pcn]:-}" ]]; then
        # Has sub — check if it's in needs_upgrade
        local nu_row
        nu_row="$(sqm_db "$state_db" "SELECT current_rating FROM needs_upgrade WHERE file_path='$(sql_escape "$mkv_file")' AND lang='$(sql_escape "$pcn")' AND resolved_ts IS NULL LIMIT 1;" 2>/dev/null || true)"
        if [[ -n "$nu_row" ]]; then
          file_status="UPGRADE"
          file_upgrade="${file_upgrade:+$file_upgrade,}$pcn"
        fi
      else
        file_status="MISSING"
        file_missing="${file_missing:+$file_missing,}$pcn"
      fi
    done

    case "$file_status" in
      OK)      compliant=$((compliant + 1)); [[ "$verbose" -eq 1 ]] && results+=("[OK] $basename") ;;
      MISSING) missing=$((missing + 1));     results+=("[MISSING:$file_missing] $basename") ;;
      UPGRADE) needs_upg=$((needs_upg + 1)); results+=("[UPGRADE:$file_upgrade] $basename") ;;
    esac

  done < <(find "$PATH_PREFIX_ROOT" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" \) ! -name "*tmp.*" 2>/dev/null | sort)

  # Output
  local rate=0
  [[ "$total" -gt 0 ]] && rate="$(awk "BEGIN { printf \"%.1f\", ($compliant / $total) * 100 }")"

  if [[ "$format" == "json" ]]; then
    jq -nc \
      --argjson total "$total" \
      --argjson compliant "$compliant" \
      --argjson missing "$missing" \
      --argjson needs_upgrade "$needs_upg" \
      --argjson no_profile "$no_profile" \
      --arg rate "$rate" \
      '{total: $total, compliant: $compliant, missing: $missing, needs_upgrade: $needs_upgrade, no_profile: $no_profile, compliance_rate: $rate}'
  else
    printf '\n=== Subtitle Compliance Report ===\n'
    printf 'Total files:     %d\n' "$total"
    printf 'Compliant:       %d\n' "$compliant"
    printf 'Missing:         %d\n' "$missing"
    printf 'Needs upgrade:   %d\n' "$needs_upg"
    printf 'No profile:      %d\n' "$no_profile"
    printf 'Compliance rate: %s%%\n\n' "$rate"

    if [[ ${#results[@]} -gt 0 ]]; then
      for r in "${results[@]}"; do
        printf '%s\n' "$r"
      done
    fi
  fi
}

cmd_enqueue() {
  local state_db="$STATE_DIR/subtitle_quality_state.db"
  local count=0
  # Remaining args after options were parsed are in ENQUEUE_FILES
  for f in "${ENQUEUE_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      enqueue_pending "$state_db" "$f" "manual"
      log "ENQUEUED: $f"
      count=$((count + 1))
    else
      log "WARN: file not found, skipping: $f"
    fi
  done
  log "enqueue done: $count file(s) added to pending queue"
}

update_watermark_hits() {
  local db="$1" srt_file="$2"
  local patterns
  patterns="$(load_watermark_patterns "$db")"
  [[ -z "$patterns" ]] && return 0
  local now
  now="$(date +%s)"
  while IFS= read -r pat; do
    [[ -z "$pat" ]] && continue
    if grep -qiE "$pat" "$srt_file" 2>/dev/null; then
      sqm_db "$db" "UPDATE watermark_patterns SET hit_count = hit_count + 1, last_hit = $now WHERE pattern = '$(sql_escape "$pat")';" 2>/dev/null || true
    fi
  done < <(printf '%s\n' "$patterns" | tr '|' '\n')
}

cmd_watermark() {
  local state_db="$STATE_DIR/subtitle_quality_state.db"
  init_state_db "$state_db"
  init_watermark_patterns "$state_db"

  local subcmd="${1:-list}"
  shift 2>/dev/null || true

  case "$subcmd" in
    list)
      printf '%-30s %-8s %-6s %-20s\n' "PATTERN" "SOURCE" "HITS" "LAST_HIT" >&2
      printf '%s\n' "-------------------------------------------------------------------" >&2
      while IFS=$'\t' read -r pat src hits last; do
        [[ -z "$pat" ]] && continue
        local last_str="--"
        [[ "$last" -gt 0 ]] && last_str="$(date -d "@$last" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$last")"
        printf '%-30s %-8s %-6s %-20s\n' "$pat" "$src" "$hits" "$last_str" >&2
      done < <(sqm_db "$state_db" -separator $'\t' "SELECT pattern, source, hit_count, last_hit FROM watermark_patterns ORDER BY source, pattern;" 2>/dev/null)
      ;;
    add)
      local pattern="${1:-}"
      [[ -z "$pattern" ]] && { echo "Usage: watermark add \"pattern\"" >&2; return 1; }
      local now
      now="$(date +%s)"
      sqm_db "$state_db" "INSERT OR IGNORE INTO watermark_patterns(pattern, source, added_ts) VALUES ('$(sql_escape "$pattern")', 'user', $now);" 2>/dev/null
      log "WATERMARK_ADD: pattern='$pattern' source=user"
      ;;
    remove)
      local pattern="${1:-}"
      [[ -z "$pattern" ]] && { echo "Usage: watermark remove \"pattern\"" >&2; return 1; }
      local src
      src="$(sqm_db "$state_db" "SELECT source FROM watermark_patterns WHERE pattern='$(sql_escape "$pattern")';" 2>/dev/null || true)"
      if [[ "$src" == "builtin" ]]; then
        echo "Cannot remove builtin pattern: $pattern" >&2
        return 1
      fi
      sqm_db "$state_db" "DELETE FROM watermark_patterns WHERE pattern='$(sql_escape "$pattern")' AND source != 'builtin';" 2>/dev/null
      log "WATERMARK_REMOVE: pattern='$pattern'"
      ;;
    test)
      local srt_file="${1:-}"
      [[ -z "$srt_file" || ! -f "$srt_file" ]] && { echo "Usage: watermark test file.srt" >&2; return 1; }
      local patterns
      patterns="$(load_watermark_patterns "$state_db")"
      if [[ -z "$patterns" ]]; then
        echo "No patterns loaded." >&2
        return 1
      fi
      local -a pat_list=()
      while IFS= read -r p; do [[ -n "$p" ]] && pat_list+=("$p"); done \
        < <(printf '%s\n' "$patterns" | tr '|' '\n')
      echo "Testing $(basename "$srt_file") against ${#pat_list[@]} patterns:" >&2
      for pat in "${pat_list[@]}"; do
        if grep -qiE "$pat" "$srt_file" 2>/dev/null; then
          local count
          count="$(grep -ciE "$pat" "$srt_file" 2>/dev/null)" || count=0
          printf '  MATCH: %-30s (%d hits)\n' "$pat" "$count" >&2
        fi
      done
      ;;
    *)
      echo "Unknown watermark subcommand: $subcmd" >&2
      echo "Usage: watermark {list|add|remove|test} [args]" >&2
      return 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then

COMMAND="${1:-}"
[[ -z "$COMMAND" ]] && { usage; exit 1; }
shift

case "$COMMAND" in
  audit|mux|strip|auto-maintain|enqueue|compliance|watermark) ;;
  --help|-h) usage; exit 0 ;;
  *) echo "Unknown command: $COMMAND" >&2; usage; exit 1 ;;
esac

ENQUEUE_FILES=()
WM_ARGS=()
if [[ "$COMMAND" == "watermark" ]]; then
  # watermark passes remaining args directly to cmd_watermark; only parse --state-dir
  WM_ARGS=("$@")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
      *) shift ;;
    esac
  done
elif [[ "$COMMAND" == "enqueue" ]]; then
  # enqueue takes positional file paths + optional --state-dir
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
      --help|-h)   usage; exit 0 ;;
      *)           ENQUEUE_FILES+=("$1"); shift ;;
    esac
  done
  [[ ${#ENQUEUE_FILES[@]} -eq 0 ]] && { echo "enqueue requires at least one file path." >&2; exit 1; }
else
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
      --format)      COMPLIANCE_FORMAT="${2:-text}"; shift 2 ;;
      --verbose)     COMPLIANCE_VERBOSE=1; shift ;;
      --help|-h)    usage; exit 0 ;;
      *)            echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
  done
fi

if [[ -z "$PATH_PREFIX" ]] && [[ "$COMMAND" != "auto-maintain" ]] && [[ "$COMMAND" != "enqueue" ]] && [[ "$COMMAND" != "compliance" ]] && [[ "$COMMAND" != "watermark" ]]; then
  echo "--path is required." >&2; exit 1
fi

if [[ "$COMMAND" == "auto-maintain" ]] && [[ -z "$PATH_PREFIX_ROOT" ]]; then
  echo "--path-prefix is required for auto-maintain." >&2; exit 1
fi

if [[ "$COMMAND" == "compliance" ]] && [[ -z "$PATH_PREFIX_ROOT" ]]; then
  echo "--path-prefix is required for compliance." >&2; exit 1
fi

if [[ "$COMMAND" == "strip" ]] && [[ -z "$TRACK_TARGET" ]] && [[ -z "$KEEP_ONLY" ]]; then
  echo "--track or --keep-only is required for strip command." >&2; exit 1
fi

if [[ "$COMMAND" == "strip" ]] && [[ -n "$TRACK_TARGET" ]] && [[ -n "$KEEP_ONLY" ]]; then
  echo "--track and --keep-only are mutually exclusive." >&2; exit 1
fi

BAZARR_API_KEY="${BAZARR_API_KEY:-$(getenv_fallback BAZARR_API_KEY BAZARR_KEY)}"

case "$COMMAND" in
  audit)        cmd_audit ;;
  mux)          cmd_mux ;;
  strip)        cmd_strip ;;
  auto-maintain) cmd_auto_maintain ;;
  enqueue)      cmd_enqueue ;;
  compliance)   cmd_compliance ;;
  watermark)    cmd_watermark "${WM_ARGS[@]}" ;;
esac

fi
