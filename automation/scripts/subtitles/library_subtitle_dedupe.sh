#!/usr/bin/env bash
set -euo pipefail

PATH_PREFIX="${PATH_PREFIX:-/APPBOX_DATA/storage/media}"
STATE_DIR="${STATE_DIR:-/APPBOX_DATA/storage/.subtitle-dedupe-state}"
DB_PATH="${DB_PATH:-$STATE_DIR/subtitle_dedupe.db}"
LOG_PATH="${LOG_PATH:-/config/berenstuff/automation/logs/library_subtitle_dedupe.log}"
BAZARR_DB="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"
MAX_FILES=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: library_subtitle_dedupe.sh [options]

Quality-dedupe external subtitles per media file and persist state in SQLite so
unchanged files are skipped on future runs.

Options:
  --path-prefix PATH   Media root to scan (default: /APPBOX_DATA/storage/media)
  --state-dir PATH     State directory for lock/db (default: /APPBOX_DATA/storage/.subtitle-dedupe-state)
  --db-path PATH       SQLite db path (default: <state-dir>/subtitle_dedupe.db)
  --log PATH           Log file path (default: /config/berenstuff/automation/logs/library_subtitle_dedupe.log)
  --bazarr-db PATH     Bazarr SQLite db path (default: /opt/bazarr/data/db/bazarr.db)
  --max-files N        Process at most N video files this run (default: 0 = all)
  --dry-run            Compute decisions but do not rename/remove files
  --help               Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path-prefix)
      PATH_PREFIX="${2:-}"
      shift 2
      ;;
    --state-dir)
      STATE_DIR="${2:-}"
      shift 2
      ;;
    --db-path)
      DB_PATH="${2:-}"
      shift 2
      ;;
    --log)
      LOG_PATH="${2:-}"
      shift 2
      ;;
    --bazarr-db)
      BAZARR_DB="${2:-}"
      shift 2
      ;;
    --max-files)
      MAX_FILES="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! [[ "$MAX_FILES" =~ ^[0-9]+$ ]]; then
  echo "--max-files must be an integer >= 0" >&2
  exit 1
fi

mkdir -p "$STATE_DIR" "$(dirname "$LOG_PATH")"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_PATH" >/dev/null
}

LOCK_FILE="$STATE_DIR/library_subtitle_dedupe.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another run is already active. Exiting."
  exit 0
fi

sqlite3 "$DB_PATH" '
CREATE TABLE IF NOT EXISTS media_state (
  media_path TEXT PRIMARY KEY,
  media_dir TEXT,
  media_size INTEGER,
  media_mtime INTEGER,
  sub_sig TEXT,
  last_status TEXT,
  last_scan_ts TEXT,
  renamed_count INTEGER DEFAULT 0,
  removed_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_state_dir ON media_state(media_dir);
'

sql_escape() {
  local s="${1:-}"
  s="${s//\'/\'\'}"
  printf '%s' "$s"
}

file_size_bytes() {
  stat -c '%s' "$1" 2>/dev/null || echo 0
}

media_duration_seconds() {
  local file="$1"
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null | awk '{printf("%d\n",$1+0)}'
}

subtitle_quality_score() {
  local sub_file="$1"
  local media_seconds="$2"
  local forced_num="$3"
  awk -v media="$media_seconds" -v forced="$forced_num" '
    function ts_to_s(ts, a) {
      gsub(",", ".", ts)
      split(ts, a, ":")
      if (length(a) != 3) return 0
      return (a[1] * 3600) + (a[2] * 60) + a[3]
    }
    BEGIN {
      cues=0; text_lines=0; text_chars=0; shown=0.0
    }
    /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9][[:space:]]+-->[[:space:]]+[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9]/ {
      split($0, p, /[[:space:]]+-->[[:space:]]+/)
      s = ts_to_s(p[1]); e = ts_to_s(p[2])
      if (e > s) shown += (e - s)
      cues++
      next
    }
    NF > 0 && $0 !~ /^[0-9]+$/ {
      text_lines++
      text_chars += length($0)
    }
    END {
      if (cues <= 0 || text_chars <= 0) {
        print 0
        exit
      }
      coverage = (media > 0) ? ((shown / media) * 100.0) : 0.0
      score = (cues * 250) + (text_lines * 120) + text_chars
      if (forced == 1) {
        ideal = 8.0
        if (coverage < 0.2) score -= 200000
        if (coverage > 60.0) score -= int((coverage - 60.0) * 5000)
      } else {
        ideal = 35.0
        if (coverage < 5.0) score -= int((5.0 - coverage) * 10000)
        if (coverage > 98.0) score -= int((coverage - 98.0) * 5000)
      }
      diff = coverage - ideal
      if (diff < 0) diff = -diff
      score -= int(diff * 3000)
      printf "%d\n", score
    }
  ' "$sub_file"
}

subtitle_group_key() {
  local sub="$1"
  local no_ext no_forced last lang forced
  no_ext="${sub%.srt}"
  forced=0
  if [[ "$no_ext" == *.forced ]]; then
    forced=1
    no_forced="${no_ext%.forced}"
  else
    no_forced="$no_ext"
  fi
  last="${no_forced##*.}"
  lang="und"
  if [[ "$last" =~ ^[a-z]{2,3}$ ]]; then
    lang="$(normalize_lang_code "$last")"
  fi
  printf '%s|%s' "$lang" "$forced"
}

canonical_subtitle_path() {
  local stem="$1"
  local key="$2"
  local lang forced
  lang="${key%%|*}"
  forced="${key##*|}"
  if [[ "$lang" == "und" ]]; then
    if [[ "$forced" == "1" ]]; then
      printf '%s.forced.srt' "$stem"
    else
      printf '%s.srt' "$stem"
    fi
  else
    if [[ "$forced" == "1" ]]; then
      printf '%s.%s.forced.srt' "$stem" "$lang"
    else
      printf '%s.%s.srt' "$stem" "$lang"
    fi
  fi
}

subtitle_signature() {
  local stem="$1"
  local files=()
  shopt -s nullglob
  files=("${stem}"*.srt)
  shopt -u nullglob

  if [[ "${#files[@]}" -eq 0 ]]; then
    printf 'none'
    return 0
  fi

  {
    local f
    for f in "${files[@]}"; do
      printf '%s|%s|%s\n' "$(basename "$f")" "$(stat -c '%s' "$f" 2>/dev/null || echo 0)" "$(stat -c '%Y' "$f" 2>/dev/null || echo 0)"
    done
  } | LC_ALL=C sort | sha1sum | awk '{print $1}'
}

dedupe_video_subtitles() {
  local media="$1"
  local stem media_seconds
  local renamed=0 removed=0
  local files=()
  local key f score size
  local profile_id allowed_json
  local -a allowed_keys=()
  declare -A allowed_map=()

  stem="${media%.*}"
  media_seconds="$(media_duration_seconds "$media")"
  [[ -z "$media_seconds" ]] && media_seconds=0

  shopt -s nullglob
  files=("${stem}"*.srt)
  shopt -u nullglob
  [[ "${#files[@]}" -eq 0 ]] && { printf '0|0|no_subtitles'; return 0; }

  profile_id="$(resolve_profile_id_for_media "$media")"
  if [[ -n "$profile_id" ]]; then
    allowed_json="$(profile_allowed_keys_json "$profile_id")"
    if [[ -n "$allowed_json" ]]; then
      mapfile -t allowed_keys < <(jq -r '.[]' <<<"$allowed_json" 2>/dev/null || true)
      for key in "${allowed_keys[@]}"; do
        allowed_map["$key"]=1
      done
    fi
  fi

  if [[ "${#allowed_map[@]}" -gt 0 ]]; then
    for f in "${files[@]}"; do
      [[ -f "$f" ]] || continue
      key="$(subtitle_group_key "$f")"
      if [[ -z "${allowed_map[$key]:-}" ]]; then
        if [[ "$DRY_RUN" -eq 0 ]]; then
          rm -f -- "$f"
        fi
        removed=$((removed + 1))
      fi
    done
    shopt -s nullglob
    files=("${stem}"*.srt)
    shopt -u nullglob
    [[ "${#files[@]}" -eq 0 ]] && { printf '%s|%s|profile_filtered' "$renamed" "$removed"; return 0; }
  fi

  declare -A best_file best_score best_size
  for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    key="$(subtitle_group_key "$f")"
    score="$(subtitle_quality_score "$f" "$media_seconds" "${key##*|}")"
    size="$(file_size_bytes "$f")"
    if [[ -z "${best_file[$key]:-}" || "$score" -gt "${best_score[$key]}" || ( "$score" -eq "${best_score[$key]}" && "$size" -gt "${best_size[$key]}" ) ]]; then
      best_file["$key"]="$f"
      best_score["$key"]="$score"
      best_size["$key"]="$size"
    fi
  done

  for key in "${!best_file[@]}"; do
    local keep canon
    keep="${best_file[$key]}"
    canon="$(canonical_subtitle_path "$stem" "$key")"

    if [[ "$keep" != "$canon" && -f "$keep" ]]; then
      if [[ "$DRY_RUN" -eq 0 ]]; then
        mv -f -- "$keep" "$canon"
      fi
      keep="$canon"
      renamed=$((renamed + 1))
    fi

    shopt -s nullglob
    files=("${stem}"*.srt)
    shopt -u nullglob
    for f in "${files[@]}"; do
      [[ -f "$f" ]] || continue
      [[ "$(subtitle_group_key "$f")" == "$key" ]] || continue
      [[ "$f" == "$keep" ]] && continue
      if [[ "$DRY_RUN" -eq 0 ]]; then
        rm -f -- "$f"
      fi
      removed=$((removed + 1))
    done
  done

  printf '%s|%s|done' "$renamed" "$removed"
}

normalize_lang_code() {
  local code="${1,,}"
  case "$code" in
    zho|chi|zhs|chs) printf 'zh' ;;
    zht|cht) printf 'zt' ;;
    *) printf '%s' "${LANG_NORM[$code]:-$code}" ;;
  esac
}

declare -A LANG_NORM
declare -A PROFILE_KEYS_CACHE

load_language_map() {
  if [[ ! -f "$BAZARR_DB" ]]; then
    return 0
  fi
  while IFS='|' read -r code2 code3 code3b; do
    [[ -n "$code2" ]] || continue
    code2="${code2,,}"
    code3="${code3,,}"
    code3b="${code3b,,}"
    LANG_NORM["$code2"]="$code2"
    [[ -n "$code3" ]] && LANG_NORM["$code3"]="$code2"
    [[ -n "$code3b" ]] && LANG_NORM["$code3b"]="$code2"
  done < <(
    sqlite3 -separator '|' "$BAZARR_DB" "
      SELECT lower(coalesce(code2,'')), lower(coalesce(code3,'')), lower(coalesce(code3b,''))
      FROM table_settings_languages;
    " 2>/dev/null || true
  )
  return 0
}

resolve_profile_id_for_media() {
  local media="$1"
  local esc out
  [[ -f "$BAZARR_DB" ]] || { printf ''; return 0; }
  esc="$(sql_escape "$media")"
  out="$(sqlite3 "$BAZARR_DB" "
    SELECT profileId FROM table_movies WHERE path='$esc' LIMIT 1;
  " 2>/dev/null | head -n1 || true)"
  if [[ -n "$out" ]]; then
    printf '%s' "$out"
    return 0
  fi
  out="$(sqlite3 "$BAZARR_DB" "
    SELECT s.profileId
    FROM table_episodes e
    JOIN table_shows s ON s.sonarrSeriesId=e.sonarrSeriesId
    WHERE e.path='$esc'
    LIMIT 1;
  " 2>/dev/null | head -n1 || true)"
  printf '%s' "$out"
}

profile_allowed_keys_json() {
  local profile_id="$1"
  local items_json
  [[ -n "$profile_id" ]] || { printf '[]'; return 0; }
  if [[ -n "${PROFILE_KEYS_CACHE[$profile_id]:-}" ]]; then
    printf '%s' "${PROFILE_KEYS_CACHE[$profile_id]}"
    return 0
  fi
  items_json="$(sqlite3 "$BAZARR_DB" "
    SELECT items
    FROM table_languages_profiles
    WHERE profileId=$profile_id
    LIMIT 1;
  " 2>/dev/null || true)"
  if [[ -z "$items_json" ]]; then
    PROFILE_KEYS_CACHE["$profile_id"]='[]'
    printf '[]'
    return 0
  fi
  PROFILE_KEYS_CACHE["$profile_id"]="$(jq -cr '
    [ .[]
      | (.language // "" | ascii_downcase) as $lang
      | (if ((.forced // "False" | ascii_downcase) == "true") then "1" else "0" end) as $forced
      | "\($lang)|\($forced)"
    ] | unique
  ' <<<"$items_json" 2>/dev/null || printf '[]')"
  printf '%s' "${PROFILE_KEYS_CACHE[$profile_id]}"
}

log "Start subtitle dedupe path_prefix=$PATH_PREFIX dry_run=$DRY_RUN max_files=$MAX_FILES"
load_language_map

scanned=0
processed=0
skipped_unchanged=0
changed=0
total_renamed=0
total_removed=0

while IFS= read -r -d '' media; do
  scanned=$((scanned + 1))
  if [[ "$MAX_FILES" -gt 0 && "$processed" -ge "$MAX_FILES" ]]; then
    break
  fi

  media_size="$(stat -c '%s' "$media" 2>/dev/null || echo 0)"
  media_mtime="$(stat -c '%Y' "$media" 2>/dev/null || echo 0)"
  media_dir="$(dirname "$media")"
  stem="${media%.*}"
  sig_before="$(subtitle_signature "$stem")"

  esc_media="$(sql_escape "$media")"
  row="$(sqlite3 "$DB_PATH" "SELECT media_size || '|' || media_mtime || '|' || sub_sig FROM media_state WHERE media_path='$esc_media' LIMIT 1;")"
  last_size="${row%%|*}"
  rest="${row#*|}"
  if [[ "$row" == "$rest" ]]; then
    last_size=""
    last_mtime=""
    last_sig=""
  else
    last_mtime="${rest%%|*}"
    last_sig="${rest#*|}"
  fi

  if [[ -n "$last_size" && "$last_size" == "$media_size" && "$last_mtime" == "$media_mtime" && "$last_sig" == "$sig_before" ]]; then
    skipped_unchanged=$((skipped_unchanged + 1))
    continue
  fi

  processed=$((processed + 1))
  IFS='|' read -r renamed removed status <<<"$(dedupe_video_subtitles "$media")"
  sig_after="$(subtitle_signature "$stem")"

  if [[ "$renamed" -gt 0 || "$removed" -gt 0 ]]; then
    changed=$((changed + 1))
    total_renamed=$((total_renamed + renamed))
    total_removed=$((total_removed + removed))
    log "CLEANED media=$(basename "$media") renamed=$renamed removed=$removed"
  fi

  if [[ "$DRY_RUN" -eq 0 ]]; then
    ts="$(date -u '+%Y-%m-%d %H:%M:%S')"
    esc_dir="$(sql_escape "$media_dir")"
    esc_sig="$(sql_escape "$sig_after")"
    esc_status="$(sql_escape "$status")"

    sqlite3 "$DB_PATH" "
      INSERT INTO media_state (
        media_path, media_dir, media_size, media_mtime, sub_sig,
        last_status, last_scan_ts, renamed_count, removed_count
      ) VALUES (
        '$esc_media', '$esc_dir', $media_size, $media_mtime, '$esc_sig',
        '$esc_status', '$ts', $renamed, $removed
      )
      ON CONFLICT(media_path) DO UPDATE SET
        media_dir=excluded.media_dir,
        media_size=excluded.media_size,
        media_mtime=excluded.media_mtime,
        sub_sig=excluded.sub_sig,
        last_status=excluded.last_status,
        last_scan_ts=excluded.last_scan_ts,
        renamed_count=excluded.renamed_count,
        removed_count=excluded.removed_count;
    "
  fi
done < <(find "$PATH_PREFIX" -type f \( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m4v' -o -iname '*.avi' \) -print0)

log "Done scanned=$scanned processed=$processed skipped_unchanged=$skipped_unchanged changed=$changed renamed=$total_renamed removed=$total_removed"
