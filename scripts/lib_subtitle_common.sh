#!/usr/bin/env bash
# lib_subtitle_common.sh — Shared functions for subtitle extraction and deduplication.
#
# Sourced by:
#   arr_profile_extract_on_import.sh
#   library_subtitle_dedupe.sh
#   bazarr_subtitle_recovery.sh
#   subtitle_quality_manager.sh
#
# Do not execute directly.
#
# Expects the sourcing script to:
#   - set -euo pipefail
#   - define LOG (log file path) before sourcing, OR override log() after sourcing
#   - define WRITES, SKIPS, PRUNES counters (used by extract_target / prune_candidates_keep)
#
# The sourcing script may set DB before sourcing to override the default Bazarr DB path.

: "${DB:=/opt/bazarr/data/db/bazarr.db}"

# Subtitle extensions that can be converted to SRT (text-based formats)
CONVERTIBLE_SUB_EXTS="ass ssa vtt"
# All subtitle extensions we care about (convertible + srt)
ALL_TEXT_SUB_EXTS="srt ass ssa vtt"

# ---------------------------------------------------------------------------
# Logging — writes to $LOG if set, otherwise to stderr
# ---------------------------------------------------------------------------

log() {
  if [[ -n "${LOG:-}" ]]; then
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
  else
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2
  fi
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

getenv_fallback() {
  local a="$1"
  local b="$2"
  local v="${!a:-}"
  if [ -z "$v" ]; then
    v="${!b:-}"
  fi
  printf '%s' "$v"
}

# ---------------------------------------------------------------------------
# SQL escaping for sqlite3 CLI (not parameterized — data is from trusted DBs)
# ---------------------------------------------------------------------------

sql_escape() {
  local s="${1:-}"
  # Strip null bytes — sqlite3 CLI chokes on these
  s="${s//$'\x00'/}"
  # Escape single quotes for SQL string literals
  s="${s//\'/\'\'}"
  printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# Library path classification helpers
# ---------------------------------------------------------------------------

# Returns 0 (true) if the path is a TV series path
is_tv_path() {
  local p="$1"
  [[ "$p" == *"/tv/"* || "$p" == *"/tvanimated/"* ]]
}

# Returns 0 (true) if the path is a movie path
is_movie_path() {
  local p="$1"
  [[ "$p" == *"/movies/"* || "$p" == *"/moviesanimated/"* ]]
}

# ---------------------------------------------------------------------------
# Language code helpers
# ---------------------------------------------------------------------------

# Convert 2-letter ISO 639-1 to 3-letter ISO 639-2 (MP4/M4V require 3-letter)
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

# Build an expanded set of language codes for matching.
# Input: comma-separated codes (e.g. "eng,spa" or "en,es")
# Output: space-separated set with both 2-letter and 3-letter variants
expand_lang_codes() {
  local input="$1"
  local -A seen=()
  local result=""
  IFS=',' read -ra codes <<< "$input"
  for code in "${codes[@]}"; do
    code="${code,,}"  # lowercase
    code="${code// /}" # trim
    [[ -z "$code" ]] && continue
    [[ -n "${seen[$code]:-}" ]] && continue
    seen["$code"]=1
    result+="$code "
    case "$code" in
      en)  [[ -z "${seen[eng]:-}" ]] && { seen[eng]=1; result+="eng "; } ;;
      eng) [[ -z "${seen[en]:-}" ]]  && { seen[en]=1;  result+="en "; }  ;;
      es)  [[ -z "${seen[spa]:-}" ]] && { seen[spa]=1; result+="spa "; } ;;
      spa) [[ -z "${seen[es]:-}" ]]  && { seen[es]=1;  result+="es "; }  ;;
      fr)  [[ -z "${seen[fre]:-}" ]] && { seen[fre]=1; result+="fre "; } ;;
      fre|fra) [[ -z "${seen[fr]:-}" ]] && { seen[fr]=1; result+="fr "; }
               [[ "$code" == "fre" ]] && [[ -z "${seen[fra]:-}" ]] && { seen[fra]=1; result+="fra "; }
               [[ "$code" == "fra" ]] && [[ -z "${seen[fre]:-}" ]] && { seen[fre]=1; result+="fre "; } ;;
      pt)  [[ -z "${seen[por]:-}" ]] && { seen[por]=1; result+="por "; } ;;
      por) [[ -z "${seen[pt]:-}" ]]  && { seen[pt]=1;  result+="pt "; }  ;;
      de)  [[ -z "${seen[ger]:-}" ]] && { seen[ger]=1; result+="ger "; } ;;
      ger|deu) [[ -z "${seen[de]:-}" ]] && { seen[de]=1; result+="de "; }
               [[ "$code" == "ger" ]] && [[ -z "${seen[deu]:-}" ]] && { seen[deu]=1; result+="deu "; }
               [[ "$code" == "deu" ]] && [[ -z "${seen[ger]:-}" ]] && { seen[ger]=1; result+="ger "; } ;;
      it)  [[ -z "${seen[ita]:-}" ]] && { seen[ita]=1; result+="ita "; } ;;
      ita) [[ -z "${seen[it]:-}" ]]  && { seen[it]=1;  result+="it "; }  ;;
      zh)  [[ -z "${seen[zho]:-}" ]] && { seen[zho]=1; result+="zho chi "; seen[chi]=1; } ;;
      zho|chi) [[ -z "${seen[zh]:-}" ]] && { seen[zh]=1; result+="zh "; } ;;
      ja)  [[ -z "${seen[jpn]:-}" ]] && { seen[jpn]=1; result+="jpn "; } ;;
      jpn) [[ -z "${seen[ja]:-}" ]]  && { seen[ja]=1;  result+="ja "; }  ;;
      ko)  [[ -z "${seen[kor]:-}" ]] && { seen[kor]=1; result+="kor "; } ;;
      kor) [[ -z "${seen[ko]:-}" ]]  && { seen[ko]=1;  result+="ko "; }  ;;
    esac
  done
  echo "$result"
}

# Text-based subtitle codecs that can be extracted to SRT.
# Bitmap codecs (hdmv_pgs_subtitle, dvd_subtitle) cannot.
TEXT_SUB_CODECS="subrip srt ass ssa mov_text webvtt"

is_text_sub_codec() {
  [[ " $TEXT_SUB_CODECS " == *" $1 "* ]]
}

# Normalize any track language tag (2-letter or 3-letter ISO) to canonical 2-letter form.
# Pure bash, no subshell, no DB — safe in hot loops.
# Returns input unchanged for unknown codes (fail-open).
normalize_track_lang() {
  case "${1,,}" in
    eng|en)       printf 'en' ;;
    spa|es)       printf 'es' ;;
    fra|fre|fr)   printf 'fr' ;;
    deu|ger|de)   printf 'de' ;;
    ita|it)       printf 'it' ;;
    por|pt)       printf 'pt' ;;
    zho|chi|zh)   printf 'zh' ;;
    jpn|ja)       printf 'ja' ;;
    kor|ko)       printf 'ko' ;;
    ara|ar)       printf 'ar' ;;
    rus|ru)       printf 'ru' ;;
    nld|nl)       printf 'nl' ;;
    swe|sv)       printf 'sv' ;;
    dan|da)       printf 'da' ;;
    fin|fi)       printf 'fi' ;;
    nor|no)       printf 'no' ;;
    pol|pl)       printf 'pl' ;;
    ces|cs)       printf 'cs' ;;
    hun|hu)       printf 'hu' ;;
    ron|ro)       printf 'ro' ;;
    tur|tr)       printf 'tr' ;;
    tha|th)       printf 'th' ;;
    vie|vi)       printf 'vi' ;;
    ell|el)       printf 'el' ;;
    heb|he)       printf 'he' ;;
    hin|hi)       printf 'hi' ;;
    ind|id)       printf 'id' ;;
    ukr|uk)       printf 'uk' ;;
    bul|bg)       printf 'bg' ;;
    hrv|hr)       printf 'hr' ;;
    und)          printf 'und' ;;
    *)            printf '%s' "${1,,}" ;;
  esac
}

# Check if a language code is in an expanded set (space-separated)
lang_in_set() {
  local lang="$1" set="$2"
  [[ " $set " == *" ${lang,,} "* ]]
}

# Get unique audio track language(s) from a media file.
# Returns comma-separated language codes (e.g. "jpn" or "eng,jpn").
# Excludes "und" (undefined).
get_audio_languages() {
  local file="$1"
  ffprobe -v quiet -print_format json -show_streams -select_streams a "$file" 2>/dev/null \
    | jq -r '[.streams[].tags.language // "und"] | map(select(. == "und" | not)) | unique | join(",")' 2>/dev/null
}

# Resolve Bazarr language profile for a media file.
# Returns comma-separated language codes (e.g. "en,es") or empty string if not found.
# $1=file_path  $2=bazarr_db_path
resolve_bazarr_profile_langs() {
  local file_path="$1"
  local bazarr_db="${2:-$DB}"

  [[ ! -f "$bazarr_db" ]] && return 1

  local profile_id=""
  local esc_path
  esc_path="$(sql_escape "$file_path")"

  if is_tv_path "$file_path"; then
    profile_id="$(sqlite3 "$bazarr_db" "
      SELECT s.profileId FROM table_episodes e
      JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
      WHERE e.path = '$esc_path' LIMIT 1;
    " 2>/dev/null)"

    if [[ -z "$profile_id" ]]; then
      local series_dir
      series_dir="$(echo "$file_path" | sed 's|/Season.*||' | xargs basename 2>/dev/null)"
      if [[ -n "$series_dir" ]]; then
        profile_id="$(sqlite3 "$bazarr_db" "
          SELECT profileId FROM table_shows
          WHERE path LIKE '%$(sql_escape "$series_dir")%' LIMIT 1;
        " 2>/dev/null)"
      fi
    fi
  elif is_movie_path "$file_path"; then
    profile_id="$(sqlite3 "$bazarr_db" "
      SELECT profileId FROM table_movies WHERE path = '$esc_path' LIMIT 1;
    " 2>/dev/null)"

    if [[ -z "$profile_id" ]]; then
      local movie_dir
      movie_dir="$(basename "$(dirname "$file_path")")"
      if [[ -n "$movie_dir" ]]; then
        profile_id="$(sqlite3 "$bazarr_db" "
          SELECT profileId FROM table_movies
          WHERE path LIKE '%$(sql_escape "$movie_dir")%' LIMIT 1;
        " 2>/dev/null)"
      fi
    fi
  fi

  [[ -z "$profile_id" ]] && return 1

  local items
  items="$(sqlite3 "$bazarr_db" "
    SELECT items FROM table_languages_profiles WHERE profileId = $profile_id LIMIT 1;
  " 2>/dev/null)"

  [[ -z "$items" ]] && return 1

  local langs
  langs="$(printf '%s' "$items" | jq -r '.[].language' 2>/dev/null | sort -u | tr '\n' ',' | sed 's/,$//')"
  [[ -z "$langs" ]] && return 1

  printf '%s' "$langs"
}

# Trigger Bazarr rescan for the media file's parent series/movie.
# Automatically detects TV vs movie path and calls the appropriate API.
# $1=file_path  $2=bazarr_db  $3=bazarr_url  $4=bazarr_api_key
bazarr_rescan_for_file() {
  local file_path="$1" bazarr_db="$2" bazarr_url="$3" api_key="$4"
  [[ -z "$api_key" ]] && return 0

  if is_tv_path "$file_path"; then
    local show_dir sonarr_id
    show_dir="$(echo "$file_path" | sed 's|/Season.*||' | sed 's|/$||')"
    sonarr_id="$(sqlite3 "$bazarr_db" "SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(sql_escape "$(basename "$show_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
    [[ -n "$sonarr_id" ]] && bazarr_scan_disk_series "$sonarr_id" "$bazarr_url" "$api_key"
  elif is_movie_path "$file_path"; then
    local movie_dir radarr_id
    movie_dir="$(dirname "$file_path")"
    radarr_id="$(sqlite3 "$bazarr_db" "SELECT radarrId FROM table_movies WHERE path LIKE '%$(sql_escape "$(basename "$movie_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
    [[ -n "$radarr_id" ]] && bazarr_scan_disk_movie "$radarr_id" "$bazarr_url" "$api_key"
  fi
}

# ---------------------------------------------------------------------------
# HTTP helper with retry on transient failures
# ---------------------------------------------------------------------------

# curl_with_retry [curl_args...]
#
# Wraps curl with automatic retry on transient errors.
# Retries up to 3 times with 5s/15s backoff on:
#   - HTTP 500, 502, 503, 504
#   - curl exit 7 (connection refused), 28 (timeout)
# Does NOT retry 4xx (client errors).
# Returns the HTTP status code on stdout (last line).
# All other curl output goes to whatever -o specifies.
#
# IMPORTANT: Caller MUST include `-w '%{http_code}'` in curl args.
# Example:
#   http_code="$(curl_with_retry -sS -o /tmp/out.json -w '%{http_code}' -X GET "$url")"
curl_with_retry() {
  local max_attempts=3
  local -a delays=(5 15)
  local attempt=1 http_code=0 curl_exit=0

  while [[ "$attempt" -le "$max_attempts" ]]; do
    set +e
    http_code="$(curl "$@" 2>/dev/null)"
    curl_exit=$?
    set -e

    # Success: 2xx or 3xx or 4xx (client error — not transient)
    if [[ "$curl_exit" -eq 0 ]]; then
      case "$http_code" in
        [23]*|4*) printf '%s' "$http_code"; return 0 ;;
      esac
    fi

    # Last attempt — don't sleep, just return
    if [[ "$attempt" -ge "$max_attempts" ]]; then
      break
    fi

    # Transient: 5xx or connection errors
    local delay="${delays[$((attempt - 1))]:-15}"
    log "RETRY attempt=$((attempt + 1))/$max_attempts curl_exit=$curl_exit http=$http_code delay=${delay}s"
    sleep "$delay"
    attempt=$((attempt + 1))
  done

  printf '%s' "$http_code"
}

# ---------------------------------------------------------------------------
# Discord notification helper (generic embed)
# ---------------------------------------------------------------------------

# notify_discord_embed TITLE DESCRIPTION COLOR
# COLOR: 3066993=green, 15105570=orange, 15844367=yellow, 3447003=blue
notify_discord_embed() {
  local title="$1" desc="$2" color="${3:-3066993}"
  [[ -n "${DISCORD_WEBHOOK_URL:-}" ]] || return 0
  local payload
  payload="$(jq -nc \
    --arg title "$title" \
    --arg desc "$desc" \
    --argjson color "$color" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      timestamp: $ts
    }]}')"
  curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

file_size_bytes() {
  stat -c '%s' "$1" 2>/dev/null || echo 0
}

media_duration_seconds() {
  local file="$1"
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null | awk '{printf("%d\n",$1+0)}'
}

# ---------------------------------------------------------------------------
# Subtitle format conversion (non-SRT text -> SRT via ffmpeg)
# ---------------------------------------------------------------------------

# Convert a non-SRT text subtitle to SRT using ffmpeg.
# Returns 0 on success, 1 on failure.
# On success, the .srt file exists and caller should remove the original.
convert_subtitle_to_srt() {
  local src="$1"
  local ext="${src##*.}"
  local base="${src%.*}"
  local dest="${base}.srt"

  # Skip if already SRT
  [[ "${ext,,}" == "srt" ]] && return 1

  # Only convert text-based formats
  case "${ext,,}" in
    ass|ssa|vtt) ;;
    *) return 1 ;;
  esac

  # Don't overwrite existing SRT
  if [[ -f "$dest" ]]; then
    log "SKIP convert $src -> .srt already exists"
    return 1
  fi

  # Convert
  if ffmpeg -y -loglevel error -i "$src" "$dest" 2>/dev/null; then
    # Verify output is non-empty
    if [[ -s "$dest" ]]; then
      log "CONVERTED ${src##*/} -> ${dest##*/}"
      return 0
    else
      rm -f "$dest"
      log "WARN convert produced empty output: ${src##*/}"
      return 1
    fi
  else
    rm -f "$dest" 2>/dev/null
    log "WARN convert failed: ${src##*/}"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# SRT watermark stripping (YIFY/YTS/opensubtitles ads + <font> tags)
# ---------------------------------------------------------------------------

# Strip watermark cue blocks and <font> HTML tags from an SRT file.
# A cue block is removed if ALL its non-empty text lines (after HTML tag
# removal) match known watermark patterns.
# Remaining cues are renumbered sequentially.
#
# Returns: 0 = modified, 1 = no changes, 2 = file deleted (entirely watermarks)
strip_srt_watermarks() {
  local srt_file="$1"
  [[ -f "$srt_file" ]] || return 1

  local tmp
  tmp="$(mktemp --suffix=.srt)"

  awk '
    BEGIN {
      cue_num = 0; in_cue = 0; is_watermark = 1
      text_lines = 0; had_changes = 0; kept_cues = 0
      cue_idx = ""; cue_ts = ""; cue_text = ""
    }

    function is_wm_line(line,   clean) {
      # Strip all HTML tags for matching
      clean = line
      gsub(/<[^>]*>/, "", clean)
      # Trim whitespace
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", clean)
      if (clean == "") return 1  # empty after tag strip = watermark
      # Case-insensitive match
      clean_lower = tolower(clean)
      if (clean_lower ~ /yts\.(lt|mx|am|ag)/) return 1
      if (clean_lower ~ /yify/) return 1
      if (clean_lower ~ /opensubtitles\.org/) return 1
      if (clean_lower ~ /^downloaded from/) return 1
      if (clean_lower ~ /official.*movies.*site/) return 1
      if (clean_lower ~ /\[?[[:space:]]*yts/) return 1
      return 0
    }

    function strip_font_tags(line) {
      gsub(/<\/?font[^>]*>/, "", line)
      return line
    }

    function flush_cue() {
      if (cue_ts == "") return
      if (is_watermark && text_lines > 0) {
        # Entire cue is watermark — drop it
        had_changes = 1
        cue_ts = ""; cue_text = ""; text_lines = 0; is_watermark = 1
        return
      }
      # Strip <font> tags from kept cue text
      cleaned = strip_font_tags(cue_text)
      if (cleaned != cue_text) had_changes = 1
      kept_cues++
      printf "%d\n%s\n%s\n\n", kept_cues, cue_ts, cleaned
      cue_ts = ""; cue_text = ""; text_lines = 0; is_watermark = 1
    }

    # Cue index line (just a number)
    /^[0-9]+[[:space:]]*$/ && !in_cue {
      flush_cue()
      cue_idx = $0
      in_cue = 1
      next
    }

    # Timestamp line
    in_cue && /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9][[:space:]]+-->[[:space:]]+[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9]/ {
      cue_ts = $0
      in_cue = 0
      next
    }

    # Blank line = end of cue block
    /^[[:space:]]*$/ {
      if (cue_ts != "") {
        flush_cue()
      }
      in_cue = 0
      next
    }

    # Text line within a cue
    cue_ts != "" {
      text_lines++
      if (!is_wm_line($0)) is_watermark = 0
      if (cue_text == "")
        cue_text = $0
      else
        cue_text = cue_text "\n" $0
      next
    }

    # Non-cue line (BOM, etc) — skip
  END {
    flush_cue()
    if (!had_changes) exit 10
    if (kept_cues == 0) exit 20
  }
  ' "$srt_file" > "$tmp"

  local rc=$?
  if [[ $rc -eq 10 ]]; then
    # No changes needed
    rm -f "$tmp"
    return 1
  elif [[ $rc -eq 20 ]]; then
    # File was entirely watermarks — delete it
    rm -f "$tmp" "$srt_file"
    log "DELETED all-watermark subtitle: $srt_file"
    return 2
  fi

  # Modified — replace original atomically, preserving permissions
  local orig_mode
  orig_mode="$(stat -c '%a' "$srt_file" 2>/dev/null || echo 644)"
  mv -f "$tmp" "$srt_file"
  chmod "$orig_mode" "$srt_file"
  log "STRIPPED watermarks/font-tags: ${srt_file##*/}"
  return 0
}

# ---------------------------------------------------------------------------
# Subtitle quality scoring (SRT cue/coverage analysis)
# ---------------------------------------------------------------------------

subtitle_quality_score() {
  local sub_file="$1"
  local media_seconds="$2"
  local forced_num="$3"
  awk -v media="$media_seconds" -v forced="$forced_num" '
    function ts_to_s(ts, a, b, c, d) {
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

# ---------------------------------------------------------------------------
# Subtitle file candidate discovery and pruning
# ---------------------------------------------------------------------------

list_lang_candidates() {
  local file="$1"
  local code="$2"
  local forced_num="$3"
  local stem
  declare -A seen=()
  stem="${file%.*}"

  shopt -s nullglob
  if [ "$forced_num" -eq 1 ]; then
    local raw=(
      "${stem}.${code}.forced.srt"
      "${stem}."*"${code}.forced.srt"
      "${stem}.${code}."*"forced.srt"
      "${stem}."*"${code}."*"forced.srt"
    )
    local p
    for p in "${raw[@]}"; do
      [ -f "$p" ] || continue
      if [ -z "${seen[$p]:-}" ]; then
        seen["$p"]=1
        printf '%s\n' "$p"
      fi
    done
  else
    local raw=(
      "${stem}.${code}.srt"
      "${stem}."*"${code}.srt"
      "${stem}.${code}."*"srt"
      "${stem}."*"${code}."*"srt"
    )
    local p
    for p in "${raw[@]}"; do
      [ -f "$p" ] || continue
      if [ -z "${seen[$p]:-}" ]; then
        seen["$p"]=1
        printf '%s\n' "$p"
      fi
    done
  fi
  shopt -u nullglob
}

prune_candidates_keep() {
  local keep="$1"
  shift
  local f
  for f in "$@"; do
    [ "$f" = "$keep" ] && continue
    [ -f "$f" ] || continue
    rm -f -- "$f"
    PRUNES=$((PRUNES + 1))
    log "PRUNE duplicate subtitle: $f"
  done
}

# ---------------------------------------------------------------------------
# Stream-matching helpers (ffprobe JSON -> subtitle stream index)
# ---------------------------------------------------------------------------

escape_regex() {
  printf '%s' "$1" | sed 's/[][(){}.^$*+?|\\/]/\\&/g'
}

extra_title_regex() {
  case "$1" in
    en) printf '%s' '(^|[^a-z])(english|eng|ingles)([^a-z]|$)' ;;
    es) printf '%s' '(^|[^a-z])(spanish|espanol|castellano|latino)([^a-z]|$)' ;;
    fr) printf '%s' '(^|[^a-z])(french|francais)([^a-z]|$)' ;;
    pt) printf '%s' '(^|[^a-z])(portuguese|portugues|brazilian)([^a-z]|$)' ;;
    zt) printf '%s' '(繁體|traditional|hant|cht|big5)' ;;
    zh) printf '%s' '(chinese|mandarin|cantonese|中文|国语|國語|粤语|粵語|簡體|简体|simplified|hans|chs)' ;;
    *) printf '%s' '' ;;
  esac
}

get_stream_idx_standard() {
  local json="$1"
  local code="$2"
  local forced_num="$3"
  local code3=""
  local code3b=""
  local lname=""
  local name_rx=""
  local extra_rx=""
  local title_rx=""

  IFS='|' read -r code3 code3b lname <<<"$(sqlite3 -separator '|' "$DB" "
    SELECT lower(coalesce(code3,'')), lower(coalesce(code3b,'')), lower(coalesce(name,''))
    FROM table_settings_languages
    WHERE lower(code2)='${code}'
    LIMIT 1;
  ")"

  extra_rx="$(extra_title_regex "$code")"
  if [ -n "$lname" ]; then
    name_rx="$(escape_regex "$lname")"
  fi

  if [ -n "$name_rx" ] && [ -n "$extra_rx" ]; then
    title_rx="${name_rx}|${extra_rx}"
  elif [ -n "$name_rx" ]; then
    title_rx="$name_rx"
  else
    title_rx="$extra_rx"
  fi

  printf '%s' "$json" | jq -r \
    --arg code "$code" \
    --arg code3 "$code3" \
    --arg code3b "$code3b" \
    --arg titleRx "$title_rx" \
    --argjson forced "$forced_num" '
      [ .streams[]
        | (.tags.language // "" | ascii_downcase) as $lang
        | (.tags.title // "" | ascii_downcase) as $title
        | select(
            ($lang == $code)
            or ($code3 != "" and $lang == $code3)
            or ($code3b != "" and $lang == $code3b)
            or ($titleRx != "" and ($title | test($titleRx; "i")))
          )
        | select((.disposition.forced // 0) == $forced)
        | .index
      ] | first // empty
    '
}

get_stream_idx_zh() {
  local json="$1"
  local code="$2"
  local forced_num="$3"
  local lang_rx title_rx

  if [ "$code" = "zt" ]; then
    lang_rx='^(zht|cht)$'
    title_rx='(繁體|traditional|hant|cht|big5)'
  else
    lang_rx='^(zho|chi|zh|zhs|chs|zht|cht)$'
    title_rx='(chinese|mandarin|cantonese|中文|国语|國語|粤语|粵語|簡體|简体|繁體|traditional|simplified|hant|hans|cht|chs)'
  fi

  printf '%s' "$json" | jq -r \
    --arg langRx "$lang_rx" \
    --arg titleRx "$title_rx" \
    --argjson forced "$forced_num" '
      [ .streams[]
        | (.tags.language // "" | ascii_downcase) as $lang
        | (.tags.title // "" | ascii_downcase) as $title
        | select(($lang | test($langRx; "i")) or ($title | test($titleRx; "i")))
        | select((.disposition.forced // 0) == $forced)
        | .index
      ] | first // empty
    '
}

# ---------------------------------------------------------------------------
# Core extraction logic
# ---------------------------------------------------------------------------

extract_target() {
  local file="$1"
  local code="$2"
  local forced_bool="$3"
  local forced_num="0"
  local json idx out suffix tmp_out
  local -a existing=()
  local best_existing=""
  local best_existing_size=0
  local best_existing_score=0
  local extracted_size=0
  local extracted_score=0
  local media_seconds=0
  local f fsize fscore

  if [ "$forced_bool" = "true" ]; then
    forced_num="1"
  fi

  json="$(ffprobe -v error -print_format json -show_streams -select_streams s "$file" 2>/dev/null || true)"
  [ -z "$json" ] && return 0

  case "$code" in
    zh|zt)
      idx="$(get_stream_idx_zh "$json" "$code" "$forced_num" || true)"
      ;;
    *)
      idx="$(get_stream_idx_standard "$json" "$code" "$forced_num" || true)"
      ;;
  esac

  [ -z "$idx" ] && return 0

  suffix="$code"
  out="${file%.*}.${suffix}.srt"
  if [ "$forced_num" -eq 1 ]; then
    out="${file%.*}.${suffix}.forced.srt"
  fi

  mapfile -t existing < <(list_lang_candidates "$file" "$code" "$forced_num")
  media_seconds="$(media_duration_seconds "$file")"
  [ -z "$media_seconds" ] && media_seconds=0

  for f in "${existing[@]}"; do
    [ -f "$f" ] || continue
    fsize="$(file_size_bytes "$f")"
    fscore="$(subtitle_quality_score "$f" "$media_seconds" "$forced_num")"
    if [ -z "$best_existing" ] || [ "$fscore" -gt "$best_existing_score" ] || { [ "$fscore" -eq "$best_existing_score" ] && [ "$fsize" -gt "$best_existing_size" ]; }; then
      best_existing_score="$fscore"
      best_existing_size="$fsize"
      best_existing="$f"
    fi
  done

  tmp_out="$(mktemp --suffix=.srt)"
  if ! ffmpeg -nostdin -loglevel error -y -i "$file" -map "0:${idx}" -c:s srt "$tmp_out"; then
    rm -f "$tmp_out"
    return 0
  fi
  extracted_size="$(file_size_bytes "$tmp_out")"
  extracted_score="$(subtitle_quality_score "$tmp_out" "$media_seconds" "$forced_num")"

  if [ -z "$best_existing" ] || [ "$extracted_score" -gt "$best_existing_score" ] || { [ "$extracted_score" -eq "$best_existing_score" ] && [ "$extracted_size" -gt "$best_existing_size" ]; }; then
    mv -f "$tmp_out" "$out"
    WRITES=$((WRITES + 1))
    log "WROTE best subtitle: $out (score=$extracted_score size=$extracted_size, prev_best_score=$best_existing_score prev_best_size=$best_existing_size)"
    if [ "${#existing[@]}" -gt 0 ]; then
      prune_candidates_keep "$out" "${existing[@]}"
    fi
    return 0
  fi

  rm -f "$tmp_out"
  SKIPS=$((SKIPS + 1))
  log "SKIP extracted subtitle for code=$code forced=$forced_num (extracted_score=$extracted_score extracted_size=$extracted_size <= best_existing_score=$best_existing_score best_existing_size=$best_existing_size)"

  if [ "$best_existing" != "$out" ]; then
    mv -f "$best_existing" "$out"
    best_existing="$out"
    log "NORMALIZE keep best existing as canonical: $out"
  fi
  if [ "${#existing[@]}" -gt 0 ]; then
    prune_candidates_keep "$best_existing" "${existing[@]}"
  fi
}

# ---------------------------------------------------------------------------
# Strip ALL embedded subtitle streams from a media file
# ---------------------------------------------------------------------------

# Removes every embedded subtitle track, keeping video/audio/attachments intact.
# Works on any container (MKV, MP4, AVI, etc.) — preserves original format.
# No-op if file has zero embedded subs. Non-fatal on failure.
# $1=file_path
strip_all_embedded_subs() {
  local file="$1"
  [[ -f "$file" ]] || return 0

  local sub_count tmp_out ext
  sub_count="$(ffprobe -v error -select_streams s -show_entries stream=index \
    -of csv=p=0 "$file" </dev/null 2>/dev/null | wc -l)"
  [[ "$sub_count" -eq 0 ]] && return 0

  ext="${file##*.}"
  tmp_out="${file}.strip_tmp.${ext}"

  if ffmpeg -y -v quiet -i "$file" -map 0 -map -0:s -c copy "$tmp_out" </dev/null 2>/dev/null; then
    # Verify output is non-empty and reasonable size
    local orig_size new_size
    orig_size="$(stat -c '%s' "$file" 2>/dev/null || echo 0)"
    new_size="$(stat -c '%s' "$tmp_out" 2>/dev/null || echo 0)"
    if [[ "$new_size" -gt 0 && "$new_size" -le "$orig_size" ]]; then
      mv -f "$tmp_out" "$file"
      log "STRIP_EMBEDDED removed $sub_count subtitle streams from $(basename "$file")"
      return 0
    else
      rm -f "$tmp_out"
      log "WARN: strip produced suspicious output (orig=${orig_size} new=${new_size}), skipping"
      return 1
    fi
  else
    rm -f "$tmp_out" 2>/dev/null
    log "WARN: strip_all_embedded_subs ffmpeg failed for $(basename "$file")"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Bazarr scan-disk API helpers
# ---------------------------------------------------------------------------

# Trigger Bazarr to rescan subtitles on disk for a movie.
# $1=radarrId  $2=bazarr_url  $3=api_key
bazarr_scan_disk_movie() {
  local radarr_id="$1" bazarr_url="$2" api_key="$3"
  local http_code
  http_code="$(curl_with_retry -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${api_key}" \
    "${bazarr_url}/api/movies?radarrid=${radarr_id}&action=scan-disk")"
  log "BAZARR_RESCAN movie id=${radarr_id} http=${http_code}"
  [[ "$http_code" == "204" || "$http_code" == "200" ]]
}

# Trigger Bazarr to rescan subtitles on disk for a series.
# $1=sonarrSeriesId  $2=bazarr_url  $3=api_key
bazarr_scan_disk_series() {
  local sonarr_id="$1" bazarr_url="$2" api_key="$3"
  local http_code
  http_code="$(curl_with_retry -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${api_key}" \
    "${bazarr_url}/api/series?seriesid=${sonarr_id}&action=scan-disk")"
  log "BAZARR_RESCAN series id=${sonarr_id} http=${http_code}"
  [[ "$http_code" == "204" || "$http_code" == "200" ]]
}

# ---------------------------------------------------------------------------
# Emby integration helpers
# ---------------------------------------------------------------------------

# Returns 0 (true) if the file is currently being played in Emby
is_file_being_played() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 1
  local playing
  playing="$(curl -fsS "${emby_url}/Sessions?api_key=${emby_key}" 2>/dev/null \
    | jq --arg path "$file_path" '[.[] | select(.NowPlayingItem.Path == $path)] | length' 2>/dev/null || echo 0)"
  [[ "$playing" -gt 0 ]]
}

# Triggers Emby to rescan a specific media item by file path.
# Non-fatal: returns 0 even on failure (caller should use || log "WARN: ...")
emby_refresh_item() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 0

  # Find item ID by searching for the filename
  local search_name
  search_name="$(basename "${file_path%.*}" | sed 's/ - S[0-9]*E[0-9]*.*//' | sed 's/ ([0-9]*)$//')"
  local item_id
  item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(printf '%s' "$search_name" | jq -sRr @uri)&Recursive=true&Limit=10" 2>/dev/null \
    | jq -r --arg path "$file_path" '.Items[] | select(.Path == $path) | .Id' 2>/dev/null | head -1)"

  if [[ -z "$item_id" ]]; then
    # Fallback: try parent folder name search for movies
    local parent_name
    parent_name="$(basename "$(dirname "$file_path")")"
    item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(printf '%s' "$parent_name" | jq -sRr @uri)&Recursive=true&Limit=10" 2>/dev/null \
      | jq -r --arg path "$file_path" '.Items[] | select(.Path == $path) | .Id' 2>/dev/null | head -1)"
  fi

  if [[ -n "$item_id" ]]; then
    curl -fsS -X POST "${emby_url}/Items/${item_id}/Refresh?api_key=${emby_key}&Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default" >/dev/null 2>&1
    log "EMBY_REFRESH item=$item_id path=$(basename "$file_path")"
    return 0
  fi
  return 0  # Non-fatal even if item not found
}
