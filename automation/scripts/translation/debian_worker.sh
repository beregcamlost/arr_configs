#!/usr/bin/env bash
# translation/debian_worker.sh
# 24/7 subtitle translation worker using debian's CPU Ollama pool.
# Runs every 5 min via cron on mubuntu. Picks up Spanish-missing files
# not currently being translated by the WSL/3090 worker.
set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly LOG_PREFIX="[debian-worker]"
readonly LOG_FILE="/config/berenstuff/automation/logs/debian_worker.log"
readonly ENV_FILE="/config/berenstuff/.env"
readonly BAZARR_DB="/opt/bazarr/data/db/bazarr.db"
readonly DEBIAN_OLLAMA_URL="http://172.20.77.2:11434"
readonly LOCK_DIR="/tmp/sub-translate-locks"
readonly PYTHONPATH_DIR="/config/berenstuff/automation/scripts"
readonly LIMIT="${DEBIAN_WORKER_LIMIT:-5}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log() {
  printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" | tee -a "$LOG_FILE" >&2
}
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  local exit_code=$?
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Health-check debian Ollama
# ---------------------------------------------------------------------------
check_ollama() {
  if ! curl -fsS --max-time 5 "${DEBIAN_OLLAMA_URL}/api/tags" > /dev/null 2>&1; then
    log "ABORT: debian Ollama unreachable at ${DEBIAN_OLLAMA_URL} — skipping run"
    exit 0
  fi
  log "Health-check OK: debian Ollama reachable at ${DEBIAN_OLLAMA_URL}"
}

# ---------------------------------------------------------------------------
# Query Bazarr DB for candidates needing Spanish translation.
# Returns newline-separated MKV paths.
# Criteria:
#   - missing_subtitles contains 'es'
#   - path exists on disk
#   - path ends in .mkv (translator requires MKV; mp4/other skipped)
#   - has a sidecar .en.srt OR audio_language contains English
#     (so the translator has something to work from)
# ---------------------------------------------------------------------------
find_candidates() {
  local limit="$1"

  # We query both episodes and movies. SQLite UNION to get combined list.
  # Using the live DB (not snapshot) so we see real-time state.
  local sql
  sql="PRAGMA busy_timeout=10000;
SELECT path FROM (
  SELECT path, audio_language, missing_subtitles
  FROM table_episodes
  WHERE missing_subtitles LIKE '%es%'
    AND path IS NOT NULL AND path != ''
  UNION ALL
  SELECT path, audio_language, missing_subtitles
  FROM table_movies
  WHERE missing_subtitles LIKE '%es%'
    AND path IS NOT NULL AND path != ''
) combined
ORDER BY RANDOM()
LIMIT ${limit};"

  sqlite3 "$BAZARR_DB" "$sql" 2>/dev/null | grep -v '^[0-9]*$' || true
}

# ---------------------------------------------------------------------------
# Per-file lock hash
# ---------------------------------------------------------------------------
file_lock_path() {
  local mkv_file="$1"
  local hash
  hash="$(printf '%s' "$mkv_file" | sha1sum | cut -d' ' -f1)"
  printf '%s/%s.lock' "$LOCK_DIR" "$hash"
}

# ---------------------------------------------------------------------------
# Translate one file using debian Ollama.
# Returns 0 on success, 1 on failure/skip.
# ---------------------------------------------------------------------------
translate_file() {
  local mkv_file="$1"

  # File must exist on disk
  if [[ ! -f "$mkv_file" ]]; then
    log "SKIP: file not found on disk: $mkv_file"
    return 1
  fi

  # File must be .mkv (translator requires MKV container for embedded track fallback)
  local ext="${mkv_file##*.}"
  if [[ "${ext,,}" != "mkv" ]]; then
    log "SKIP: not an MKV (ext=${ext}): $(basename "$mkv_file")"
    return 1
  fi

  # Check we have a source to translate from: .en.srt sidecar OR English audio_language
  local dir stem
  dir="$(dirname "$mkv_file")"
  stem="$(basename "${mkv_file%.*}")"
  local has_source=0

  # Sidecar .en.srt check
  if ls "${dir}/${stem}".en*.srt > /dev/null 2>&1; then
    has_source=1
  fi
  # Embedded English audio track check (translator can extract embedded subs too)
  if [[ "$has_source" -eq 0 ]]; then
    if ffprobe -v quiet -show_streams -print_format json "$mkv_file" 2>/dev/null \
        | python3 -c "
import sys, json
s = json.load(sys.stdin)['streams']
eng = any(
    x.get('codec_type') in ('audio','subtitle') and
    x.get('tags',{}).get('language','') in ('eng','en')
    for x in s
)
sys.exit(0 if eng else 1)
" 2>/dev/null; then
      has_source=1
    fi
  fi

  if [[ "$has_source" -eq 0 ]]; then
    log "SKIP: no English source (no .en.srt, no English audio/sub track): $(basename "$mkv_file")"
    return 1
  fi

  # Per-file flock — skip if another worker holds it
  local lock_path
  lock_path="$(file_lock_path "$mkv_file")"
  exec 9>"$lock_path"
  if ! flock -n 9; then
    log "SKIP: per-file lock held by another worker: $(basename "$mkv_file")"
    exec 9>&-
    return 0
  fi

  log "START: translating $(basename "$mkv_file")"

  local exit_code=0
  PYTHONPATH="$PYTHONPATH_DIR" \
  OLLAMA_BASE_URL="$DEBIAN_OLLAMA_URL" \
    python3 -m translation.translator translate --file "$mkv_file" \
    </dev/null 2>&1 | while IFS= read -r line; do
      log "  $line"
    done || exit_code=$?

  # Release the lock explicitly (also released on fd close at function exit)
  flock -u 9
  exec 9>&-

  # Check output
  local es_srt="${dir}/${stem}.es.srt"
  local marker="${es_srt}.ollama"
  if [[ -f "$es_srt" ]]; then
    log "SUCCESS: created ${es_srt}"
    [[ -f "$marker" ]] && log "SUCCESS: marker ${marker} present"
    return 0
  else
    log "FAIL: no .es.srt produced for $(basename "$mkv_file") (exit_code=${exit_code})"
    return 1
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  # Ensure log dir exists
  mkdir -p "$(dirname "$LOG_FILE")"
  mkdir -p "$LOCK_DIR"

  # Worker-level flock: only one instance of this worker at a time,
  # whether launched by cron or manually.
  exec 7>/tmp/debian_worker.lock
  if ! flock -n 7; then
    # Log without timestamp to stdout only — don't spam the log file
    printf '[debian-worker] already running, skipping\n' >&2
    exit 0
  fi

  log "=== debian-worker starting (limit=${LIMIT}) ==="

  # Source environment
  # shellcheck source=/dev/null
  [[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

  # Override Ollama URL to point at debian (never written to .env)
  export OLLAMA_BASE_URL="$DEBIAN_OLLAMA_URL"

  # Health-check
  check_ollama

  # Find candidates
  local candidates=()
  while IFS= read -r path; do
    [[ -n "$path" ]] && candidates+=("$path")
  done < <(find_candidates "$LIMIT")

  if [[ "${#candidates[@]}" -eq 0 ]]; then
    log "No candidates found — queue empty or all locked"
    log "=== debian-worker done ==="
    exit 0
  fi

  log "Found ${#candidates[@]} candidate(s)"

  local ok=0 fail=0
  for mkv_file in "${candidates[@]}"; do
    if translate_file "$mkv_file"; then
      (( ok++ )) || true
    else
      (( fail++ )) || true
    fi
  done

  log "=== debian-worker done: ok=${ok} fail=${fail} ==="
}

main "$@"
