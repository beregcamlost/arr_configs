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
# SAFETY (added 2026-06-02 after a disk-full incident):
#   - DISK GUARD: never starts a remux unless free space >= source size + --min-free-gb;
#     stops the whole run on low disk so an orphaned temp can never fill the volume.
#   - STARTUP SWEEP: removes stale *.deemb.tmp.* / *.extract.tmp / *.deemb.bak.* left by a
#     prior run that was hard-killed (SIGKILL skips the inline cleanup).
#   - SIGNAL TRAP: on INT/TERM removes the in-flight temp and restores the original if a
#     swap was mid-flight.
#   - VALIDATE-THEN-DELETE-BACKUP: each original is kept as a .deemb.bak sidecar, swapped,
#     the NEW file is re-verified in place, and the backup is deleted only if it is healthy
#     (otherwise the original is rolled back).
#
# DRY-RUN by DEFAULT: probes + COUNTS only, performs ZERO mutations. Use --execute to act.
# Batched + throttled (load guard + sleep) for the shared 2-vCPU box.
#
#   ./batch_extract_embedded.sh [--dry-run|--execute] [--limit N] [--sleep S]
#                               [--load-max N] [--media-root DIR] [--min-free-gb N]
#
# *** GATED: do NOT mass-run without approval. First real run: --execute --limit 1, supervised. ***

# Reuse production helpers + config. Its main() is guarded by BASH_SOURCE==$0, so sourcing
# only defines functions / config vars (extract_profile_subtitle_sidecars, expand_lang_codes_inline,
# lang_in_set_inline, sql_quote, log, BAZARR_DB, ...).
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./library_codec_manager.sh
source "$_SELF_DIR/library_codec_manager.sh"
set +e +u +o pipefail   # the batch must survive per-file errors

# Load EMBY_URL / EMBY_API_KEY (playback-skip) + BAZARR_* (rescan) if not already in env.
[[ -f /config/berenstuff/.env ]] && { set -a; . /config/berenstuff/.env 2>/dev/null; set +a; }

MEDIA_ROOT="${MEDIA_ROOT:-/APPBOX_DATA/storage/media}"
DRY_RUN=1
LIMIT=0
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"
LOAD_MAX="${LOAD_MAX:-200}"   # host load on the shared box is ~70 baseline; our cgroup caps real impact
MIG_LOG="${MIG_LOG:-/APPBOX_DATA/storage/.transcode-state/de-embed-migration.log}"
BACKUP_DIR="${BACKUP_DIR:-}"   # if set, originals are ALSO copied here before strip (legacy/test)
MIN_FREE_GB="${MIN_FREE_GB:-20}"   # stop the run if free space would drop below this (+ src size)

usage_mig() {
  sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --min-free-gb) MIN_FREE_GB="$2"; shift 2 ;;
    --bazarr-db)  BAZARR_DB="$2"; shift 2 ;;
    -h|--help)    usage_mig 0 ;;
    *) echo "Unknown option: $1" >&2; usage_mig 1 ;;
  esac
done

mkdir -p "$(dirname "$MIG_LOG")" 2>/dev/null || true
LOG_PATH="$MIG_LOG"   # extract_profile_subtitle_sidecars writes ffmpeg stderr here
[[ -f "$BAZARR_DB" ]] || { echo "Bazarr DB not found: $BAZARR_DB" >&2; exit 1; }

mlog() { printf '%s [%s] %s\n' "$(date '+%F %T')" "$1" "${*:2}" | tee -a "$MIG_LOG"; }

# Bytes available on the filesystem holding $1.
free_bytes() { df -P "$1" 2>/dev/null | awk 'NR==2{print $4 * 1024}'; }

# In-flight artifacts, tracked so the signal trap can roll back a half-done file.
CURRENT_TMP=""; CURRENT_BAK=""; CURRENT_SRC=""
cleanup_on_signal() {
  [[ -n "$CURRENT_TMP" && -f "$CURRENT_TMP" ]] && rm -f "$CURRENT_TMP" 2>/dev/null
  # original was moved aside but the swap never completed -> put it back
  if [[ -n "$CURRENT_BAK" && -f "$CURRENT_BAK" && -n "$CURRENT_SRC" && ! -s "$CURRENT_SRC" ]]; then
    mv -f "$CURRENT_BAK" "$CURRENT_SRC" 2>/dev/null
  fi
}
trap cleanup_on_signal INT TERM EXIT

# Remove temp/backup artifacts orphaned by a prior interrupted run (EXECUTE only).
# A SIGKILL (disk-full/OOM/host kill) skips the inline rm, so a *.deemb.tmp.* can be a
# full-size copy left next to the media. Backups are only swept when the live file exists.
sweep_stale_temps() {
  local n=0 freed=0 sz p live
  while IFS= read -r -d '' p; do
    sz="$(stat -c '%s' "$p" 2>/dev/null || echo 0)"
    rm -f "$p" && { n=$((n + 1)); freed=$((freed + sz)); }
  done < <(find "$MEDIA_ROOT" -type f \( -name '*.deemb.tmp.*' -o -name '*.extract.tmp' \) -print0 2>/dev/null)
  while IFS= read -r -d '' p; do
    live="${p%.deemb.bak.*}.${p##*.}"   # BASE.deemb.bak.EXT -> BASE.EXT
    [[ -s "$live" ]] || continue
    sz="$(stat -c '%s' "$p" 2>/dev/null || echo 0)"
    rm -f "$p" && { n=$((n + 1)); freed=$((freed + sz)); }
  done < <(find "$MEDIA_ROOT" -type f -name '*.deemb.bak.*' -print0 2>/dev/null)
  [[ "$n" -gt 0 ]] && mlog info "startup sweep: removed $n stale temp/backup file(s) ($((freed / 1024 / 1024))MB)"
  return 0
}

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

# True if the file is currently being played in Emby (mirrors lib_subtitle_common.sh).
# Such files are skipped this pass; a later pass catches them once playback ends.
is_being_played() {
  local file_path="$1" emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 1
  local n
  n="$(curl -fsS --max-time 5 "${emby_url}/Sessions?api_key=${emby_key}" 2>/dev/null \
    | jq --arg p "$file_path" '[.[] | select(.NowPlayingItem.Path == $p)] | length' 2>/dev/null || echo 0)"
  [[ "${n:-0}" -gt 0 ]]
}

# A healthy media file must probe and still carry both a video and an audio stream.
file_plays_ok() {
  local f="$1" v a
  v="$(ffprobe -v error -select_streams v -show_entries stream=index -of csv=p=0 "$f" 2>/dev/null | head -1)"
  a="$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$f" 2>/dev/null | head -1)"
  [[ -n "$v" && -n "$a" ]]
}

# Remux: drop embedded TEXT subs, keep video/audio + image-based subs. (--execute only)
# Aborts (leaving the original UNTOUCHED) on remux failure, size anomaly, duration drift, or
# if profile-lang text subs would remain. On swap, keeps the original as a .deemb.bak sidecar,
# re-verifies the new file in place, and deletes the backup only if it is healthy (else rolls back).
strip_text_subs_keep_image() {
  local src="$1" pset="$2" ext tmp bak osz nsz sdur ddur diff leftover
  ext="${src##*.}"; tmp="${src%.*}.deemb.tmp.${ext}"; bak="${src%.*}.deemb.bak.${ext}"
  local -a img=()
  while IFS= read -r i; do [[ -n "$i" ]] && img+=( -map "0:$i" ); done < <(
    ffprobe -v error -select_streams s -show_entries stream=index,codec_name -of csv=p=0 "$src" 2>/dev/null \
      | awk -F',' '$2=="hdmv_pgs_subtitle"||$2=="dvd_subtitle"||$2=="pgssub"||$2=="dvdsub"{print $1}')
  CURRENT_SRC="$src"; CURRENT_TMP="$tmp"
  if ! ffmpeg -hide_banner -nostdin -y -v error -i "$src" -map 0 -map -0:s "${img[@]}" -c copy "$tmp" </dev/null 2>>"$MIG_LOG"; then
    rm -f "$tmp"; CURRENT_TMP=""; CURRENT_SRC=""; return 1
  fi
  osz="$(stat -c '%s' "$src" 2>/dev/null || echo 0)"
  nsz="$(stat -c '%s' "$tmp" 2>/dev/null || echo 0)"
  # remux drops only sub streams; size must be close (>=90%) and non-zero.
  if [[ "$nsz" -lt $((osz * 9 / 10)) || "$nsz" -eq 0 ]]; then
    rm -f "$tmp"; CURRENT_TMP=""; CURRENT_SRC=""; mlog warn "strip aborted (size $osz->$nsz): $(basename "$src")"; return 1
  fi
  # stream-copy must preserve duration; abort on >2s drift.
  sdur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$src" 2>/dev/null | cut -d. -f1)"
  ddur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$tmp" 2>/dev/null | cut -d. -f1)"
  if [[ "$sdur" =~ ^[0-9]+$ && "$ddur" =~ ^[0-9]+$ ]]; then
    diff=$(( sdur - ddur )); diff=${diff#-}
    if [[ "$diff" -gt 2 ]]; then
      rm -f "$tmp"; CURRENT_TMP=""; CURRENT_SRC=""; mlog warn "strip aborted (dur ${sdur}s->${ddur}s): $(basename "$src")"; return 1
    fi
  fi
  # the stripped temp must no longer carry any profile-lang TEXT sub (the whole point).
  if [[ -n "$pset" ]]; then
    leftover="$(list_extractable_text_subs "$tmp" "$pset" | head -1)"
    if [[ -n "$leftover" ]]; then
      rm -f "$tmp"; CURRENT_TMP=""; CURRENT_SRC=""; mlog warn "strip aborted (text sub remains: $leftover): $(basename "$src")"; return 1
    fi
  fi
  # optional centralized backup (legacy --backup-dir).
  if [[ -n "$BACKUP_DIR" ]]; then
    mkdir -p "$BACKUP_DIR" 2>/dev/null
    cp -n "$src" "$BACKUP_DIR/$(basename "$src")" 2>/dev/null || true
  fi
  # validate-then-delete-backup: keep the original as a sidecar, swap, re-verify in place.
  if ! mv -f "$src" "$bak"; then rm -f "$tmp"; CURRENT_TMP=""; CURRENT_SRC=""; return 1; fi
  CURRENT_BAK="$bak"
  if ! mv -f "$tmp" "$src"; then
    mv -f "$bak" "$src" 2>/dev/null
    CURRENT_TMP=""; CURRENT_BAK=""; CURRENT_SRC=""
    mlog warn "strip aborted (swap failed, restored): $(basename "$src")"; return 1
  fi
  CURRENT_TMP=""
  if file_plays_ok "$src"; then
    rm -f "$bak"; CURRENT_BAK=""; CURRENT_SRC=""
    return 0
  fi
  # new file failed verification -> roll back to the original.
  mv -f "$bak" "$src" 2>/dev/null
  CURRENT_BAK=""; CURRENT_SRC=""
  mlog warn "strip aborted (post-swap verify failed, restored): $(basename "$src")"; return 1
}

# ---- main ----
mlog info "START de-embed mode=$([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo EXECUTE) root=$MEDIA_ROOT limit=$LIMIT sleep=$SLEEP_BETWEEN load_max=$LOAD_MAX min_free=${MIN_FREE_GB}GB"
[[ "$DRY_RUN" -eq 0 ]] && sweep_stale_temps

total_files=0 scanned=0 with_profile=0 with_text=0 extract_tracks=0 stripped=0 skipped_playback=0 skipped_lowdisk=0
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
  # Skip files being watched in Emby right now — never interrupt a viewer; a later pass gets them.
  if is_being_played "$f"; then
    skipped_playback=$((skipped_playback + 1))
    mlog debug "skip (playback in Emby): $(basename "$f")"
    continue
  fi
  # DISK GUARD: a remux writes a full-size temp next to the source. Never start one unless we
  # keep >= MIN_FREE_GB free afterwards, so a hard-kill can never fill the volume.
  srcsz="$(stat -c '%s' "$f" 2>/dev/null || echo 0)"
  freeb="$(free_bytes "$MEDIA_ROOT")"
  if [[ -n "$freeb" && "$freeb" -lt $(( srcsz + MIN_FREE_GB * 1024 * 1024 * 1024 )) ]]; then
    skipped_lowdisk=$((skipped_lowdisk + 1))
    mlog error "LOW DISK: free=$((freeb / 1024 / 1024 / 1024))GB < need=$(( srcsz / 1024 / 1024 / 1024 + MIN_FREE_GB ))GB — stopping run"
    break
  fi
  extract_profile_subtitle_sidecars "$f" "$pset"
  if strip_text_subs_keep_image "$f" "$pset"; then
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
[[ "$DRY_RUN" -eq 0 ]] && echo "skipped (being watched in Emby):   $skipped_playback   <- a later pass will catch these"
[[ "$DRY_RUN" -eq 0 ]] && echo "skipped (low disk, run stopped):   $skipped_lowdisk"
echo "--- breakdown by lang:type ---"
for k in $(printf '%s\n' "${!by_bucket[@]}" | sort); do
  printf '  %-12s %d\n' "$k" "${by_bucket[$k]}"
done
mlog info "DONE scanned=$scanned with_text=$with_text tracks=$extract_tracks stripped=$stripped skipped_playback=$skipped_playback skipped_lowdisk=$skipped_lowdisk"
