#!/usr/bin/env bash
# batch_extract_embedded.sh — Library de-embed migration (sidecar subtitle standard).
#
# For each media file that has embedded TEXT subtitles in a Bazarr-profile language:
#   1. extract the best text sub per (profile-lang, type) to a .<lang>[.forced|.sdh].srt
#      sidecar  — reuses library_codec_manager.sh::extract_profile_subtitle_sidecars
#      (tested, no-clobber, atomic). Image-based subs (PGS/VOBSUB) are left in the container.
#   2. strip the embedded TEXT subs from the container (keep image subs).
#   3. trigger a Bazarr rescan + a throttled Emby refresh.
#
# DRY-RUN by DEFAULT: probes + COUNTS only, performs ZERO mutations. Use --execute to act.
# Batched + throttled (load guard + sleep) for the shared 2-vCPU box.
#
#   ./batch_extract_embedded.sh [--dry-run|--execute] [--limit N] [--sleep S]
#                               [--load-max N] [--media-root DIR]
#
# *** GATED: do NOT mass-run without approval. First real run: --execute --limit 1, supervised. ***

# Reuse production helpers + config. Its main() is guarded by BASH_SOURCE==$0, so sourcing
# only defines functions / config vars (extract_profile_subtitle_sidecars, expand_lang_codes_inline,
# lang_in_set_inline, sql_quote, log, BAZARR_DB, ...).
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./library_codec_manager.sh
source "$_SELF_DIR/library_codec_manager.sh"
set +e +u +o pipefail   # the batch must survive per-file errors

MEDIA_ROOT="${MEDIA_ROOT:-/APPBOX_DATA/storage/media}"
DRY_RUN=1
LIMIT=0
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"
LOAD_MAX="${LOAD_MAX:-200}"   # host load on the shared box is ~70 baseline; our cgroup caps real impact
MIG_LOG="${MIG_LOG:-/APPBOX_DATA/storage/.transcode-state/de-embed-migration.log}"

usage_mig() {
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=1; shift ;;
    --execute)    DRY_RUN=0; shift ;;
    --limit)      LIMIT="$2"; shift 2 ;;
    --sleep)      SLEEP_BETWEEN="$2"; shift 2 ;;
    --load-max)   LOAD_MAX="$2"; shift 2 ;;
    --media-root) MEDIA_ROOT="$2"; shift 2 ;;
    --bazarr-db)  BAZARR_DB="$2"; shift 2 ;;
    -h|--help)    usage_mig 0 ;;
    *) echo "Unknown option: $1" >&2; usage_mig 1 ;;
  esac
done

mkdir -p "$(dirname "$MIG_LOG")" 2>/dev/null || true
LOG_PATH="$MIG_LOG"   # extract_profile_subtitle_sidecars writes ffmpeg stderr here
[[ -f "$BAZARR_DB" ]] || { echo "Bazarr DB not found: $BAZARR_DB" >&2; exit 1; }

mlog() { printf '%s [%s] %s\n' "$(date '+%F %T')" "$1" "${*:2}" | tee -a "$MIG_LOG"; }

load_ok() {
  local l1; l1="$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)"
  [[ "$l1" -lt "$LOAD_MAX" ]]
}

# Bazarr profile languages for a file, by PATH. Echoes comma-separated 2-letter codes (or empty).
resolve_profile_langs_by_path() {
  local path="$1" pid="" items="" q
  q="$(sql_quote "$path")"
  case "$path" in
    */tv/*|*/tvanimated/*)
      pid="$(sqlite3 -cmd '.timeout 5000' "$BAZARR_DB" \
        "SELECT s.profileId FROM table_episodes e JOIN table_shows s ON s.sonarrSeriesId=e.sonarrSeriesId WHERE e.path='$q' LIMIT 1;" 2>/dev/null)" ;;
    */movies/*|*/moviesanimated/*)
      pid="$(sqlite3 -cmd '.timeout 5000' "$BAZARR_DB" \
        "SELECT profileId FROM table_movies WHERE path='$q' LIMIT 1;" 2>/dev/null)" ;;
  esac
  [[ -z "$pid" || "$pid" == "None" ]] && return 0
  items="$(sqlite3 -cmd '.timeout 5000' "$BAZARR_DB" \
    "SELECT items FROM table_languages_profiles WHERE profileId=$pid LIMIT 1;" 2>/dev/null)"
  [[ -z "$items" ]] && return 0
  echo "$items" | jq -r '.[].language' 2>/dev/null | paste -sd, -
}

# Echo "lang:type" (type in normal|forced|sdh) for each embedded TEXT sub whose lang is in the set.
list_extractable_text_subs() {
  local src="$1" profile_set="$2"
  ffprobe -v error -select_streams s \
    -show_entries stream=index,codec_name:stream_tags=language:stream_disposition=forced,hearing_impaired \
    -of json "$src" 2>/dev/null \
    | jq -r '.streams[]? | [((.codec_name//"")|ascii_downcase),((.tags.language//"und")|ascii_downcase),((.disposition.forced//0)|tostring),((.disposition.hearing_impaired//0)|tostring)] | @tsv' 2>/dev/null \
    | while IFS=$'\t' read -r codec lang forced hi; do
        case " subrip ass ssa mov_text webvtt text " in *" $codec "*) : ;; *) continue ;; esac
        [[ "$lang" == "und" ]] && continue
        lang_in_set_inline "$lang" "$profile_set" || continue
        local t="normal"; [[ "$forced" == "1" ]] && t="forced"; [[ "$hi" == "1" ]] && t="sdh"
        echo "${lang}:${t}"
      done
}

# Remux: drop embedded TEXT subs, keep video/audio + image-based subs. (--execute only)
strip_text_subs_keep_image() {
  local src="$1" ext tmp osz nsz
  ext="${src##*.}"; tmp="${src%.*}.deemb.tmp.${ext}"
  local -a img=()
  while IFS= read -r i; do [[ -n "$i" ]] && img+=( -map "0:$i" ); done < <(
    ffprobe -v error -select_streams s -show_entries stream=index,codec_name -of csv=p=0 "$src" 2>/dev/null \
      | awk -F',' '$2=="hdmv_pgs_subtitle"||$2=="dvd_subtitle"||$2=="pgssub"||$2=="dvdsub"{print $1}')
  if ! ffmpeg -hide_banner -nostdin -y -v error -i "$src" -map 0 -map -0:s "${img[@]}" -c copy "$tmp" </dev/null 2>>"$MIG_LOG"; then
    rm -f "$tmp"; return 1
  fi
  osz="$(stat -c '%s' "$src" 2>/dev/null || echo 0)"
  nsz="$(stat -c '%s' "$tmp" 2>/dev/null || echo 0)"
  # remux drops only sub streams; size must be close (>=90%) and non-zero.
  if [[ "$nsz" -lt $((osz * 9 / 10)) || "$nsz" -eq 0 ]]; then
    rm -f "$tmp"; mlog warn "strip aborted (size $osz->$nsz): $(basename "$src")"; return 1
  fi
  mv -f "$tmp" "$src"
}

# ---- main ----
mlog info "START de-embed mode=$([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo EXECUTE) root=$MEDIA_ROOT limit=$LIMIT sleep=$SLEEP_BETWEEN load_max=$LOAD_MAX"

total_files=0 scanned=0 with_profile=0 with_text=0 extract_tracks=0 stripped=0
declare -A by_bucket=()

mapfile -t ALL < <(find "$MEDIA_ROOT" -type f \( -name '*.mkv' -o -name '*.mp4' -o -name '*.m4v' \) 2>/dev/null | sort)
total_files=${#ALL[@]}
mlog info "media files found: $total_files"

for f in "${ALL[@]}"; do
  [[ "$LIMIT" -gt 0 && "$scanned" -ge "$LIMIT" ]] && break
  [[ -f "$f" ]] || continue
  scanned=$((scanned + 1))

  if ! load_ok; then
    mlog warn "load >= $LOAD_MAX — waiting 30s (scanned=$scanned)"; sleep 30
    load_ok || { mlog warn "still high — stopping early"; break; }
  fi

  langs="$(resolve_profile_langs_by_path "$f")"
  [[ -z "$langs" ]] && continue
  with_profile=$((with_profile + 1))
  pset="$(expand_lang_codes_inline "$langs")"

  mapfile -t hits < <(list_extractable_text_subs "$f" "$pset")
  [[ "${#hits[@]}" -eq 0 ]] && continue
  with_text=$((with_text + 1))
  extract_tracks=$((extract_tracks + ${#hits[@]}))
  for h in "${hits[@]}"; do by_bucket["$h"]=$(( ${by_bucket["$h"]:-0} + 1 )); done

  if [[ "$DRY_RUN" -eq 1 ]]; then
    continue   # DRY-RUN: counts only, no mutation
  fi

  # ---- EXECUTE path (guarded; only runs with --execute) ----
  extract_profile_subtitle_sidecars "$f" "$pset"
  if strip_text_subs_keep_image "$f"; then
    stripped=$((stripped + 1))
    notify_emby_refresh 2>/dev/null || true
  fi
  sleep "$SLEEP_BETWEEN"
done

echo
echo "===== de-embed migration summary ($([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo EXECUTE)) ====="
echo "media files total:                 $total_files"
echo "scanned this run:                  $scanned"
echo "with a Bazarr profile:             $with_profile"
echo "with profile-lang embedded TEXT:   $with_text   <- files that would get sidecars"
echo "extractable text tracks:           $extract_tracks"
[[ "$DRY_RUN" -eq 0 ]] && echo "files stripped:                    $stripped"
echo "--- breakdown by lang:type ---"
for k in $(printf '%s\n' "${!by_bucket[@]}" | sort); do
  printf '  %-12s %d\n' "$k" "${by_bucket[$k]}"
done
mlog info "DONE scanned=$scanned with_text=$with_text tracks=$extract_tracks stripped=$stripped"
