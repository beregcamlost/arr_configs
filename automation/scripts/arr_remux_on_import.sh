#!/usr/bin/env bash
# arr_remux_on_import.sh
# Triggered by Sonarr/Radarr on import. Stream-copy remuxes MKV -> MP4 with +faststart
# when filename matches a pattern in remux_patterns.txt. Stream copy, no transcode,
# no quality loss. Designed to fix MKV-container stutter on Xiaomi 55" TVs.
set -euo pipefail

LOCK_FILE="/tmp/arr_remux_on_import.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATTERNS_FILE="$SCRIPT_DIR/remux_patterns.txt"
LOG_DIR="/config/berenstuff/automation/logs"
LOG="$LOG_DIR/remux.log"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG"; }

# --- 1. Get file path from Sonarr/Radarr env vars ---
FILE="${sonarr_episodefile_path:-${radarr_moviefile_path:-}}"
[ -z "$FILE" ] && { log "EXIT: no file path env var set"; exit 0; }
[[ "$FILE" != *.mkv ]] && { log "SKIP(not-mkv): $FILE"; exit 0; }
[ ! -f "$FILE" ] && { log "SKIP(missing): $FILE"; exit 0; }

# --- 2. Release group filter ---
if [ ! -f "$PATTERNS_FILE" ]; then
  log "WARN: patterns file missing at $PATTERNS_FILE — skipping all"
  exit 0
fi
if ! grep -iF -f "$PATTERNS_FILE" <<< "$(basename "$FILE")" > /dev/null; then
  log "SKIP(no-pattern-match): $FILE"
  exit 0
fi

# --- 3. Safety gates ---
SUBS=$(ffprobe -v error -select_streams s -show_entries stream=codec_name -of csv=p=0 "$FILE" 2>/dev/null || true)
if echo "$SUBS" | grep -qE 'hdmv_pgs|dvd_subtitle|dvdsub|pgssub|ass|ssa'; then
  log "SKIP(unsafe-subs: $SUBS): $FILE"
  exit 0
fi

ATTACH=$(ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "$FILE" 2>/dev/null | grep -c attachment || true)
if [ "$ATTACH" -gt 0 ]; then
  log "SKIP(attachments=$ATTACH): $FILE"
  exit 0
fi

VCODEC=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$FILE" 2>/dev/null || echo "")
if [ "$VCODEC" = "av1" ]; then
  log "SKIP(av1): $FILE"
  exit 0
fi

# --- 3.5 Extract embedded text subs to external sidecars (sidecar = standard) ---
# Sidecars load instantly in Emby Web; embedded text subs force a slow on-the-fly
# ffmpeg extraction at playback. Image/styled subs were already excluded by the gate
# above, so every subtitle stream here is text and extracts cleanly to .srt.
STEM="${FILE%.mkv}"
SUB_COUNT=$(ffprobe -v error -select_streams s -show_entries stream=index -of csv=p=0 "$FILE" 2>/dev/null | grep -c . || true)
if [ "${SUB_COUNT:-0}" -gt 0 ]; then
  si=0
  while [ "$si" -lt "$SUB_COUNT" ]; do
    SLANG=$(ffprobe -v error -select_streams "s:$si" -show_entries stream_tags=language -of csv=p=0 "$FILE" 2>/dev/null || true)
    [ -z "$SLANG" ] && SLANG="und"
    SFORCED=$(ffprobe -v error -select_streams "s:$si" -show_entries stream_disposition=forced -of csv=p=0 "$FILE" 2>/dev/null || echo 0)
    if [ "$SFORCED" = "1" ]; then SIDE="${STEM}.${SLANG}.forced.srt"; else SIDE="${STEM}.${SLANG}.srt"; fi
    if [ -f "$SIDE" ]; then
      log "KEEP existing sidecar (no clobber): $(basename "$SIDE")"
    elif ffmpeg -hide_banner -loglevel error -y -i "$FILE" -map "0:s:$si" -c:s srt "$SIDE" </dev/null 2>>"$LOG" && [ -s "$SIDE" ]; then
      log "EXTRACTED sub s:$si ($SLANG forced=$SFORCED) -> $(basename "$SIDE")"
    else
      rm -f "$SIDE"; log "WARN: extract sub s:$si failed (non-fatal)"
    fi
    si=$((si + 1))
  done
fi

# --- 4. Remux (video+audio only; subs are now sidecars, never embedded) ---
MP4="${FILE%.mkv}.mp4"
BAK="${FILE%.mkv}.remux-pre.bak.mkv"

log "START: $FILE -> $MP4"
mv "$FILE" "$BAK"

# Trap to restore on any failure between here and the end
RESTORE_BAK_ON_ERR=1
trap '[ "$RESTORE_BAK_ON_ERR" = "1" ] && { rm -f "$MP4"; [ -f "$BAK" ] && mv "$BAK" "$FILE"; log "RESTORE: rolled back due to error"; }' ERR

if ! ffmpeg -hide_banner -loglevel warning -i "$BAK" \
    -map 0:v -map 0:a \
    -c:v copy -c:a copy \
    -movflags +faststart \
    -y "$MP4" 2>>"$LOG"; then
  log "ERROR(ffmpeg-failed): $FILE"
  rm -f "$MP4"
  mv "$BAK" "$FILE"
  RESTORE_BAK_ON_ERR=0
  exit 1
fi

# --- 5. Verify size sanity (±10%) ---
MP4_SZ=$(stat -c%s "$MP4" 2>/dev/null || echo 0)
BAK_SZ=$(stat -c%s "$BAK" 2>/dev/null || echo 1)
DIFF=$(( (MP4_SZ - BAK_SZ) * 100 / BAK_SZ ))
ABS_DIFF=${DIFF#-}
if [ "$ABS_DIFF" -gt 10 ]; then
  log "ERROR(size-mismatch: BAK=$BAK_SZ MP4=$MP4_SZ diff=${DIFF}%): $FILE"
  rm -f "$MP4"
  mv "$BAK" "$FILE"
  RESTORE_BAK_ON_ERR=0
  exit 1
fi

RESTORE_BAK_ON_ERR=0
log "OK: $MP4 size=$(numfmt --to=iec "$MP4_SZ" 2>/dev/null || echo $MP4_SZ)"

# --- 6. Trigger Sonarr/Radarr rescan so they pick up the .mp4 ---
SONARR_KEY=$(grep -oP "(?<=<ApiKey>)[^<]+" /config/.config/Sonarr/config.xml 2>/dev/null || true)
RADARR_KEY=$(grep -oP "(?<=<ApiKey>)[^<]+" /config/.config/Radarr/config.xml 2>/dev/null || grep -oP "(?<=<ApiKey>)[^<]+" /config/radarr/config.xml 2>/dev/null || true)

if [ -n "${sonarr_series_id:-}" ] && [ -n "$SONARR_KEY" ]; then
  curl -sS -X POST "http://localhost:8989/sonarr/api/v3/command" \
    -H "X-Api-Key: $SONARR_KEY" -H "Content-Type: application/json" \
    -d "{\"name\":\"RescanSeries\",\"seriesId\":${sonarr_series_id}}" >/dev/null 2>&1 || true
  log "RESCAN(sonarr): series $sonarr_series_id"
fi
if [ -n "${radarr_movie_id:-}" ] && [ -n "$RADARR_KEY" ]; then
  curl -sS -X POST "http://localhost:7878/radarr/api/v3/command" \
    -H "X-Api-Key: $RADARR_KEY" -H "Content-Type: application/json" \
    -d "{\"name\":\"RescanMovie\",\"movieId\":${radarr_movie_id}}" >/dev/null 2>&1 || true
  log "RESCAN(radarr): movie $radarr_movie_id"
fi

# Trigger Emby refresh via PUBLIC URL (localhost:8096 not reachable from container)
EMBY_KEY="${EMBY_API_KEY:-}"
EMBY_URL_LOCAL="${EMBY_URL:-}"
if [ -z "$EMBY_KEY" ] || [ -z "$EMBY_URL_LOCAL" ]; then
  _env_file="/config/berenstuff/.env"
  [ -f "$_env_file" ] && source "$_env_file" 2>/dev/null || true
  EMBY_KEY="${EMBY_API_KEY:-}"
  EMBY_URL_LOCAL="${EMBY_URL:-}"
fi
if [ -n "$EMBY_KEY" ] && [ -n "$EMBY_URL_LOCAL" ]; then
  curl -sS -X POST "${EMBY_URL_LOCAL}/Library/Refresh?api_key=${EMBY_KEY}" \
    --max-time 30 >/dev/null 2>&1 || true
  log "RESCAN(emby): library refresh triggered"
fi
log "DONE: $FILE"
exit 0
