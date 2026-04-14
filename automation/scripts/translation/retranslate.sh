#!/usr/bin/env bash
# retranslate.sh — re-run ES translation for batch or full mode
#   --mode batch  : sweep all missing ES on That '70s Show + Evil (strip embedded first)
#   --mode full   : clean specific bad Evil eps + full sweep of both shows
# Survives session close via nohup. Sends Discord embed on completion.
set -euo pipefail

source /config/berenstuff/.env
source /config/berenstuff/automation/scripts/subtitles/lib_subtitle_common.sh

# ── Constants ────────────────────────────────────────────────────────────────
STATE_DB="/APPBOX_DATA/storage/.translation-state/translation_state.db"
MAX_RETRIES=5
BACKOFF_BASE=60  # seconds

COLOR_GREEN=3066993
COLOR_RED=15158332

T70S_DIR="/APPBOX_DATA/storage/media/tv/That '70s Show"
EVIL_DIR="/APPBOX_DATA/storage/media/tv/Evil"

# Episodes of Evil known to have bad ES translations (retranslate_full only)
EVIL_BAD_EPS="S01E01 S01E05 S01E07 S01E13 S02E01 S02E02 S02E03 S02E04 S02E07 S02E10 S02E13 S03E02 S03E06 S03E10 S04E03 S04E06"

# ── Helpers ──────────────────────────────────────────────────────────────────

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

# translate_file MKV
# Clears cooldown state, calls translator, retries on quota/rate errors.
translate_file() {
  local mkv="$1"
  local bn attempt output wait
  bn="$(basename "$mkv")"
  attempt=0

  while [[ $attempt -lt $MAX_RETRIES ]]; do
    sqlite3 "$STATE_DB" \
      "DELETE FROM translation_log WHERE media_path = '$(echo "$mkv" | sed "s/'/''/g")';" \
      </dev/null 2>/dev/null || true

    output="$(cd /config/berenstuff && PYTHONPATH=automation/scripts \
      python3 -m translation.translator translate --file "$mkv" 2>&1)" || true

    if echo "$output" | grep -q "Done: 1 translated"; then
      log "OK: $bn"
      return 0
    elif echo "$output" | grep -q "All profile langs present"; then
      log "SKIP (already has ES): $bn"
      return 0
    elif echo "$output" | grep -q "high demand\|timed out\|quota\|429\|500\|503\|504"; then
      attempt=$((attempt + 1))
      wait=$((BACKOFF_BASE * attempt))
      log "RETRY $attempt/$MAX_RETRIES (backoff ${wait}s): $bn"
      sleep "$wait"
    elif echo "$output" | grep -q "No source SRT"; then
      log "NO_SOURCE: $bn — need EN srt extracted first"
      return 1
    else
      log "FAIL: $bn — $(echo "$output" | grep -E 'ERROR|Done:|Traceback' | tail -1)"
      return 1
    fi
  done
  log "EXHAUSTED: $bn after $MAX_RETRIES retries"
  return 1
}

# extract_en_subs SHOW_DIR
# Extracts the first English subtitle track from each MKV/MP4 that lacks an .en.srt sidecar.
extract_en_subs() {
  local show_dir="$1"
  local extracted=0 mkv stem srt en_idx
  for mkv in "$show_dir"/Season*/*.mkv "$show_dir"/Season*/*.mp4; do
    [[ -f "$mkv" ]] || continue
    [[ "$mkv" == *"tmp."* ]] && continue
    stem="${mkv%.*}"
    srt="${stem}.en.srt"
    [[ -f "$srt" ]] && continue
    en_idx="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv" \
      </dev/null 2>/dev/null \
      | jq -r '.streams[]
          | select(
              (.tags.language=="eng" or .tags.language=="en")
              and (.codec_name=="subrip" or .codec_name=="mov_text"
                   or .codec_name=="ass" or .codec_name=="ssa")
            )
          | .index' \
      | head -1)"
    [[ -z "$en_idx" ]] && continue
    ffmpeg -v quiet -i "$mkv" -map "0:${en_idx}" -f srt "$srt" </dev/null 2>/dev/null \
      && extracted=$((extracted + 1))
  done
  log "Extracted $extracted EN srt files from $(basename "$show_dir")"
}

# strip_es_embedded SHOW_DIR
# Removes all embedded Spanish subtitle tracks from every MKV under the show dir.
strip_es_embedded() {
  local show_dir="$1"
  local stripped=0 mkv spa_tid tmp
  for mkv in "$show_dir"/Season*/*.mkv; do
    [[ -f "$mkv" ]] || continue
    [[ "$mkv" == *".striptmp."* ]] && continue
    spa_tid="$(mkvmerge -J "$mkv" </dev/null 2>/dev/null \
      | jq -r '.tracks[]
          | select(.type=="subtitles"
              and (.properties.language=="spa" or .properties.language=="es"))
          | .id')"
    [[ -z "$spa_tid" ]] && continue
    tmp="${mkv%.mkv}.striptmp.mkv"
    if mkvmerge -q -o "$tmp" --subtitle-tracks "!${spa_tid}" "$mkv" </dev/null 2>/dev/null; then
      mv "$tmp" "$mkv"
      stripped=$((stripped + 1))
    else
      rm -f "$tmp"
    fi
  done
  log "Stripped $stripped ES embedded subs from $(basename "$show_dir")"
}

# discord_color OK_COUNT FAIL_COUNT → prints color int (always exits 0)
discord_color() { [[ "$2" -gt 0 ]] && echo "$COLOR_RED" || { echo "$COLOR_GREEN"; }; }

# ── Mode: batch ───────────────────────────────────────────────────────────────
run_batch() {
  LOG="/config/berenstuff/automation/logs/retranslate_batch.log"
  log "========== BATCH RE-TRANSLATION START =========="

  log "=== THAT '70s SHOW ==="
  extract_en_subs "$T70S_DIR"
  local t70s_ok=0 t70s_fail=0 stem
  for mkv in "$T70S_DIR"/Season*/*.mkv; do
    [[ -f "$mkv" ]] || continue
    [[ "$mkv" == *".striptmp."* ]] && continue
    stem="${mkv%.mkv}"
    [[ -f "${stem}.es.srt" ]] && continue
    if translate_file "$mkv"; then
      t70s_ok=$((t70s_ok + 1))
    else
      t70s_fail=$((t70s_fail + 1))
    fi
  done
  log "That '70s Show done: $t70s_ok translated, $t70s_fail failed"
  notify_discord_embed "That '70s Show — ES Translation" \
    "Translated: $t70s_ok | Failed: $t70s_fail" \
    "$(discord_color "$t70s_ok" "$t70s_fail")" \
    "Subtitle Re-Translator"

  log "=== EVIL ==="
  strip_es_embedded "$EVIL_DIR"
  extract_en_subs "$EVIL_DIR"
  local evil_ok=0 evil_fail=0
  for mkv in "$EVIL_DIR"/Season*/*.mkv; do
    [[ -f "$mkv" ]] || continue
    [[ "$mkv" == *".striptmp."* ]] && continue
    stem="${mkv%.mkv}"
    [[ -f "${stem}.es.srt" ]] && continue
    if translate_file "$mkv"; then
      evil_ok=$((evil_ok + 1))
    else
      evil_fail=$((evil_fail + 1))
    fi
  done
  log "Evil done: $evil_ok translated, $evil_fail failed"
  notify_discord_embed "Evil — ES Translation" \
    "Translated: $evil_ok | Failed: $evil_fail" \
    "$(discord_color "$evil_ok" "$evil_fail")" \
    "Subtitle Re-Translator"

  log "========== BATCH RE-TRANSLATION COMPLETE =========="
  notify_discord_embed "Batch Re-Translation Complete" \
    "That '70s Show: $t70s_ok ok / $t70s_fail fail\nEvil: $evil_ok ok / $evil_fail fail" \
    "$COLOR_GREEN" "Subtitle Re-Translator"
}

# ── Mode: full ────────────────────────────────────────────────────────────────
run_full() {
  LOG="/config/berenstuff/automation/logs/retranslate_full.log"
  log "========== FULL RE-TRANSLATION START =========="

  # --- Evil: remove bad external ES + strip embedded ES for known-bad episodes ---
  log "=== EVIL — Cleaning BAD ES episodes ==="
  local ep season stem spa_tid tmp mkv
  for ep in $EVIL_BAD_EPS; do
    season="${ep:1:2}"
    for mkv in "$EVIL_DIR/Season $((10#$season))"/*"$ep"*.mkv; do
      [[ -f "$mkv" ]] || continue
      stem="${mkv%.mkv}"
      [[ -f "${stem}.es.srt" ]] && rm -f "${stem}.es.srt" \
        && log "Removed: $(basename "${stem}.es.srt")"
      spa_tid="$(mkvmerge -J "$mkv" </dev/null 2>/dev/null \
        | jq -r '.tracks[]
            | select(.type=="subtitles"
                and (.properties.language=="spa" or .properties.language=="es"))
            | .id')" || true
      if [[ -n "$spa_tid" ]]; then
        tmp="${mkv%.mkv}.striptmp.mkv"
        if mkvmerge -q -o "$tmp" --subtitle-tracks "!${spa_tid}" "$mkv" </dev/null 2>/dev/null; then
          mv "$tmp" "$mkv"
          log "Stripped ES from: $(basename "$mkv")"
        else
          rm -f "$tmp"
        fi
      fi
    done
  done

  log "=== EVIL — Extracting EN subs ==="
  extract_en_subs "$EVIL_DIR"

  log "=== EVIL — Translating ==="
  local evil_ok=0 evil_fail=0
  for ep in $EVIL_BAD_EPS; do
    season="${ep:1:2}"
    for mkv in "$EVIL_DIR/Season $((10#$season))"/*"$ep"*.mkv; do
      [[ -f "$mkv" ]] || continue
      if translate_file "$mkv"; then
        evil_ok=$((evil_ok + 1))
      else
        evil_fail=$((evil_fail + 1))
      fi
    done
  done
  local bad_ep_count
  bad_ep_count="$(echo "$EVIL_BAD_EPS" | wc -w)"
  log "Evil done: $evil_ok translated, $evil_fail failed"
  notify_discord_embed "Evil — ES Re-Translation" \
    "Translated: $evil_ok / $bad_ep_count BAD | Failed: $evil_fail" \
    "$(discord_color "$evil_ok" "$evil_fail")" \
    "Subtitle Re-Translator"

  # --- That '70s Show: extract EN, translate all missing ES ---
  log "=== THAT '70s SHOW — Extracting EN subs ==="
  extract_en_subs "$T70S_DIR"

  log "=== THAT '70s SHOW — Translating ==="
  local t70s_ok=0 t70s_fail=0 t70s_skip=0 media
  for media in "$T70S_DIR"/Season*/*.mkv "$T70S_DIR"/Season*/*.mp4; do
    [[ -f "$media" ]] || continue
    [[ "$media" == *"tmp."* ]] && continue
    stem="${media%.*}"
    if [[ -f "${stem}.es.srt" ]]; then
      t70s_skip=$((t70s_skip + 1))
      continue
    fi
    if translate_file "$media"; then
      t70s_ok=$((t70s_ok + 1))
    else
      t70s_fail=$((t70s_fail + 1))
    fi
  done
  log "That '70s Show done: $t70s_ok translated, $t70s_fail failed, $t70s_skip skipped"
  notify_discord_embed "That '70s Show — ES Translation" \
    "Translated: $t70s_ok | Failed: $t70s_fail | Skipped: $t70s_skip" \
    "$(discord_color "$t70s_ok" "$t70s_fail")" \
    "Subtitle Re-Translator"

  log "========== FULL RE-TRANSLATION COMPLETE =========="
  notify_discord_embed "Batch Re-Translation Complete" \
    "Evil: $evil_ok ok / $evil_fail fail\nThat '70s Show: $t70s_ok ok / $t70s_fail fail / $t70s_skip skip" \
    "$COLOR_GREEN" "Subtitle Re-Translator"
}

# ── Entry point ───────────────────────────────────────────────────────────────

MODE="${1:-}"
# Normalise "--mode full" (two args) into a single token
[[ "$MODE" == "--mode" || "$MODE" == "-m" ]] && MODE="${MODE}=${2:-}"

case "$MODE" in
  --mode=batch|-m=batch) run_batch ;;
  --mode=full|-m=full)   run_full  ;;
  *)
    echo "Usage: $(basename "$0") --mode batch|full" >&2
    echo "  batch  Sweep all missing ES on both shows (strip embedded Evil ES first)" >&2
    echo "  full   Clean specific bad Evil eps + full sweep of both shows" >&2
    exit 1
    ;;
esac
