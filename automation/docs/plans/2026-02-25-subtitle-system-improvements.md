# Subtitle System Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement 7 improvements to the subtitle automation system: unified webhook hook, DRY state functions, hardened SQL escaping, API retry logic, batch JSON conversion, dedupe Discord notifications, and recovery `--since` flag.

**Architecture:** Bottom-up approach — shared library improvements first (tasks 1-3), then consumers updated to use them (tasks 4-7). Each task is independently testable via `bash -n` syntax checks and dry-run modes.

**Tech Stack:** Bash (set -euo pipefail), SQLite3 CLI, curl, jq, Python3 (minimal), ffmpeg/ffprobe

**Key paths:**
- Canonical scripts: `automation/scripts/subtitles/`
- Compat copies: `scripts/`
- Shared lib: `automation/scripts/subtitles/lib_subtitle_common.sh`
- Recovery: `automation/scripts/subtitles/bazarr_subtitle_recovery.sh`
- Dedupe: `automation/scripts/subtitles/library_subtitle_dedupe.sh`
- Batch extractor: `automation/scripts/subtitles/batch_extract_embedded.sh`
- Sonarr hook (to delete): `automation/scripts/subtitles/sonarr_profile_extract_on_import.sh`
- Radarr hook (to delete): `automation/scripts/subtitles/radarr_profile_extract_on_import.sh`

**Compat sync rule:** After editing any canonical script in `automation/scripts/`, always sync the compat copy in `scripts/` and run `bash -n` on it.

---

### Task 1: Add `sql_escape()`, `curl_with_retry()`, and `notify_discord_embed()` to shared library

**Files:**
- Modify: `automation/scripts/subtitles/lib_subtitle_common.sh` (append after line 49, before `file_size_bytes`)

**Step 1: Add `sql_escape()` function**

Add after the `getenv_fallback()` function (after line 49):

```bash
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
```

**Step 2: Add `curl_with_retry()` function**

Add after `sql_escape()`:

```bash
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
```

**Step 3: Add `notify_discord_embed()` function**

Add after `curl_with_retry()`:

```bash
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
```

**Step 4: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/lib_subtitle_common.sh
cp automation/scripts/subtitles/lib_subtitle_common.sh scripts/lib_subtitle_common.sh
bash -n scripts/lib_subtitle_common.sh
```

**Step 5: Commit**

```bash
git add automation/scripts/subtitles/lib_subtitle_common.sh scripts/lib_subtitle_common.sh
git commit -m "feat(subtitles): add sql_escape, curl_with_retry, notify_discord_embed to shared lib"
```

---

### Task 2: Unified Webhook Hook

**Files:**
- Create: `automation/scripts/subtitles/arr_profile_extract_on_import.sh`
- Create: `scripts/arr_profile_extract_on_import.sh` (compat copy)
- Delete: `automation/scripts/subtitles/sonarr_profile_extract_on_import.sh`
- Delete: `automation/scripts/subtitles/radarr_profile_extract_on_import.sh`
- Delete: `scripts/sonarr_profile_extract_on_import.sh`
- Delete: `scripts/radarr_profile_extract_on_import.sh`

**Step 1: Create the unified hook**

Write `automation/scripts/subtitles/arr_profile_extract_on_import.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/arr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

WRITES=0
SKIPS=0
PRUNES=0

source "$(dirname "$0")/lib_subtitle_common.sh"

# ---------------------------------------------------------------------------
# Auto-detect Sonarr vs Radarr from env vars
# ---------------------------------------------------------------------------
ARR_TYPE=""
EVENT_TYPE=""
MEDIA_PATH=""
MEDIA_ID=""
PROFILE_ID=""

sonarr_event="$(getenv_fallback SONARR_EVENTTYPE sonarr_eventtype)"
radarr_event="$(getenv_fallback RADARR_EVENTTYPE radarr_eventtype)"

if [[ -n "$sonarr_event" ]]; then
  ARR_TYPE="sonarr"
  EVENT_TYPE="$sonarr_event"
  MEDIA_PATH="$(getenv_fallback SONARR_EPISODEFILE_PATH sonarr_episodefile_path)"
  MEDIA_ID="$(getenv_fallback SONARR_SERIES_ID sonarr_series_id)"
elif [[ -n "$radarr_event" ]]; then
  ARR_TYPE="radarr"
  EVENT_TYPE="$radarr_event"
  MEDIA_PATH="$(getenv_fallback RADARR_MOVIEFILE_PATH radarr_moviefile_path)"
  MEDIA_ID="$(getenv_fallback RADARR_MOVIE_ID radarr_movie_id)"
else
  echo "ERROR: No Sonarr or Radarr event type detected." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Discord notification (adapts to arr type)
# ---------------------------------------------------------------------------
notify_discord() {
  local status="$1" details="$2"
  local file_name color emoji label id_label

  [[ -n "${DISCORD_WEBHOOK_URL:-}" ]] || return 0

  file_name="$(basename "${MEDIA_PATH:-unknown}")"

  case "$ARR_TYPE" in
    sonarr) label="Sonarr"; id_label="Series ID" ;;
    radarr) label="Radarr"; id_label="Movie ID" ;;
  esac

  case "$status" in
    SUCCESS) color=3066993;  emoji="✅" ;;
    SKIP)    color=15844367; emoji="⏭️" ;;
    *)       color=3447003;  emoji="ℹ️" ;;
  esac

  local payload
  payload="$(jq -nc \
    --arg title "$emoji Subtitle Extract — $label" \
    --arg desc "$details" \
    --argjson color "$color" \
    --arg event "${EVENT_TYPE:-unknown}" \
    --arg media_id "${MEDIA_ID:-unknown}" \
    --arg profile_id "${PROFILE_ID:-unknown}" \
    --arg file_name "$file_name" \
    --arg id_label "$id_label" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: [
        {name: $id_label, value: $media_id, inline: true},
        {name: "Profile", value: $profile_id, inline: true},
        {name: "File", value: ("`" + $file_name + "`")}
      ],
      footer: {text: ("Event: " + $event)},
      timestamp: $ts
    }]}')"

  curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Resolve profile ID (works for both Sonarr series and Radarr movies)
# ---------------------------------------------------------------------------
resolve_profile_id() {
  local media_id="$1" media_path="$2"
  local esc_path profile_id default_profile attempt
  esc_path="$(sql_escape "$media_path")"

  for attempt in $(seq 1 10); do
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE sonarrSeriesId=$media_id LIMIT 1;")"
      if [[ -z "$profile_id" && -n "$media_path" ]]; then
        profile_id="$(sqlite3 "$DB" "SELECT s.profileId FROM table_episodes e JOIN table_shows s ON s.sonarrSeriesId=e.sonarrSeriesId WHERE e.path='$esc_path' LIMIT 1;")"
      fi
    else
      profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE radarrId=$media_id LIMIT 1;")"
      if [[ -z "$profile_id" && -n "$media_path" ]]; then
        profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE path='$esc_path' LIMIT 1;")"
      fi
    fi
    if [[ -n "$profile_id" ]]; then
      printf '%s' "$profile_id"
      return 0
    fi
    sleep 2
  done

  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  else
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  fi
  if [[ -z "$default_profile" ]]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_languages_profiles ORDER BY profileId LIMIT 1;")"
  fi
  printf '%s' "$default_profile"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "EVENT=$EVENT_TYPE arr=$ARR_TYPE media_id=$MEDIA_ID path=$MEDIA_PATH"

  if [[ -z "$MEDIA_PATH" || ! -f "$MEDIA_PATH" ]]; then
    log "Skip: no media file path"
    notify_discord "SKIP" "Reason: no media file path"
    exit 0
  fi

  # Resolve media ID from DB if not provided
  if [[ -z "$MEDIA_ID" ]]; then
    local esc_path
    esc_path="$(sql_escape "$MEDIA_PATH")"
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      MEDIA_ID="$(sqlite3 "$DB" "SELECT sonarrSeriesId FROM table_episodes WHERE path='$esc_path' LIMIT 1;")"
    else
      MEDIA_ID="$(sqlite3 "$DB" "SELECT radarrId FROM table_movies WHERE path='$esc_path' LIMIT 1;")"
    fi
  fi

  if [[ -z "$MEDIA_ID" ]]; then
    log "Skip: media id not found"
    notify_discord "SKIP" "Reason: media id not found"
    exit 0
  fi

  PROFILE_ID="$(resolve_profile_id "$MEDIA_ID" "$MEDIA_PATH")"

  if [[ -z "$PROFILE_ID" ]]; then
    log "Skip: profile not found for $ARR_TYPE id=$MEDIA_ID"
    notify_discord "SKIP" "Reason: profile not found"
    exit 0
  fi

  # Check profile matches media
  local profile_check
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_shows WHERE sonarrSeriesId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  else
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_movies WHERE radarrId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  fi
  if [[ "$profile_check" != "1" ]]; then
    log "Fallback profile applied for $ARR_TYPE id=$MEDIA_ID: profile=$PROFILE_ID"
  fi

  local items
  items="$(sqlite3 "$DB" "SELECT items FROM table_languages_profiles WHERE profileId=$PROFILE_ID LIMIT 1;")"

  if [[ -z "$items" ]]; then
    log "Skip: empty profile items"
    notify_discord "SKIP" "Reason: empty profile items"
    exit 0
  fi

  while IFS='|' read -r code forced; do
    [[ -z "$code" ]] && continue
    code="${code,,}"
    forced="${forced,,}"
    if [[ "$forced" != "true" && "$forced" != "false" ]]; then
      forced="false"
    fi
    log "Applying extraction for language=$code forced=$forced profile=$PROFILE_ID"
    extract_target "$MEDIA_PATH" "$code" "$forced"
  done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

  if [[ "$WRITES" -gt 0 ]]; then
    notify_discord "SUCCESS" "**$WRITES** extracted · **$SKIPS** skipped · **$PRUNES** pruned"
  else
    notify_discord "INFO" "No new extractions · **$SKIPS** skipped · **$PRUNES** pruned"
  fi

  log "Done"
}

main "$@"
```

**Step 2: Delete old hooks and sync compat**

```bash
rm -f automation/scripts/subtitles/sonarr_profile_extract_on_import.sh
rm -f automation/scripts/subtitles/radarr_profile_extract_on_import.sh
rm -f scripts/sonarr_profile_extract_on_import.sh
rm -f scripts/radarr_profile_extract_on_import.sh
chmod +x automation/scripts/subtitles/arr_profile_extract_on_import.sh
cp automation/scripts/subtitles/arr_profile_extract_on_import.sh scripts/arr_profile_extract_on_import.sh
```

**Step 3: Syntax check**

```bash
bash -n automation/scripts/subtitles/arr_profile_extract_on_import.sh
bash -n scripts/arr_profile_extract_on_import.sh
```

**Step 4: Commit**

```bash
git add -A automation/scripts/subtitles/sonarr_profile_extract_on_import.sh \
  automation/scripts/subtitles/radarr_profile_extract_on_import.sh \
  scripts/sonarr_profile_extract_on_import.sh \
  scripts/radarr_profile_extract_on_import.sh \
  automation/scripts/subtitles/arr_profile_extract_on_import.sh \
  scripts/arr_profile_extract_on_import.sh
git commit -m "feat(subtitles): unify sonarr/radarr hooks into single arr_profile_extract_on_import.sh"
```

**Post-deploy note:** User must update Sonarr Connect and Radarr Connect webhook paths to point to `scripts/arr_profile_extract_on_import.sh`.

---

### Task 3: DRY Recovery State DB Functions

**Files:**
- Modify: `automation/scripts/subtitles/bazarr_subtitle_recovery.sh`

This task replaces 7 functions (`state_get_ts`, `state_get_bazarr_attempts`, `state_get_arr_attempts`, `state_get_regrab_attempts`, `state_inc_bazarr_attempts`, `state_inc_arr_attempts`, `state_inc_regrab_attempts`) with 2 generic functions (`state_get_col`, `state_inc_col`).

**Step 1: Replace the 7 state functions (lines 249-372)**

Delete everything from `state_get_ts()` (line 249) through the closing `}` of `state_inc_regrab_attempts()` (line 372). Replace with:

```bash
state_get_col() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5" col="$6"
  sqlite3 "$STATE_DB" "
    SELECT COALESCE($col,0) FROM recovery_state
    WHERE media_type='$media_type' AND media_id=$media_id AND lang_code='$lang' AND forced=$forced AND hi=$hi
    LIMIT 1;
  "
}

state_inc_col() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5" col="$6"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[DRY-RUN] state_inc_col $media_type $media_id $lang forced=$forced hi=$hi col=$col"
    return 0
  fi
  sqlite3 "$STATE_DB" "
    INSERT INTO recovery_state
      (media_type, media_id, lang_code, forced, hi, $col, updated_at)
    VALUES
      ('$media_type', $media_id, '$lang', $forced, $hi, 1, datetime('now'))
    ON CONFLICT(media_type, media_id, lang_code, forced, hi) DO UPDATE SET
      $col=COALESCE($col,0)+1,
      updated_at=datetime('now');
  "
}
```

Also keep `state_set()` and `state_reset_for_regrab()` and `state_reset_bazarr_attempts()` unchanged.

**Step 2: Update all call sites in `process_item()` function**

Replace every occurrence:
- `state_get_ts "$mt" "$id" "$lang" "$forced" "$hi" "col_name"` — already matches `state_get_col` signature, just rename
- `state_get_bazarr_attempts ...` → `state_get_col ... "bazarr_attempts"`
- `state_get_arr_attempts ...` → `state_get_col ... "arr_attempts"`
- `state_get_regrab_attempts ...` → `state_get_col ... "regrab_attempts"`
- `state_inc_bazarr_attempts ...` → `state_inc_col ... "bazarr_attempts"`
- `state_inc_arr_attempts ...` → `state_inc_col ... "arr_attempts"`
- `state_inc_regrab_attempts ...` → `state_inc_col ... "regrab_attempts"`

Specific call-site changes inside `process_item()`:

```
# Line ~824: state_get_ts → state_get_col (same sig, rename only)
last_baz="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_bazarr_try_ts")"
last_tr="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_translate_try_ts")"
last_arr="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_arr_try_ts")"
last_regrab="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_regrab_ts")"
baz_attempts="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "bazarr_attempts")"
arr_att="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "arr_attempts")"
regrab_att="$(state_get_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "regrab_attempts")"

# Line ~866: state_inc_bazarr_attempts → state_inc_col
state_inc_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "bazarr_attempts"

# Line ~969: state_inc_arr_attempts → state_inc_col
state_inc_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "arr_attempts"

# Line ~996: state_inc_regrab_attempts → state_inc_col
state_inc_col "$media_type" "$media_id" "$lang" "$forced" "$hi" "regrab_attempts"
```

**Step 3: Also use shared `sql_escape` from lib**

Remove the local `sql_escape()` definition from recovery if present — it now comes from `lib_subtitle_common.sh` via the shared lib.

Wait — recovery doesn't source `lib_subtitle_common.sh`. It's standalone. So we need to either: source the lib, or keep the local copy. Since recovery doesn't use any other lib functions, just replace its local inline escaping with the improved `sql_escape()` defined locally (copy the improved version from the lib).

Actually, let's just add `source "$(dirname "$0")/lib_subtitle_common.sh"` to recovery. It already has its own `log()` which overrides the lib's version. This gives it access to `sql_escape()` and `curl_with_retry()`.

Add after line 98 (`mkdir -p ...`), before the TMPDIR line:

```bash
source "$(dirname "$0")/lib_subtitle_common.sh"
```

Then remove the `jsonish_to_json` function and the inline `sql_escape` if defined locally. Recovery's `log()` already overrides the lib's log().

**Step 4: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/bazarr_subtitle_recovery.sh
cp automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
bash -n scripts/bazarr_subtitle_recovery.sh
```

**Step 5: Commit**

```bash
git add automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
git commit -m "refactor(subtitles): DRY up recovery state DB functions + source shared lib"
```

---

### Task 4: Integrate `curl_with_retry()` into Recovery Script

**Files:**
- Modify: `automation/scripts/subtitles/bazarr_subtitle_recovery.sh`

**Step 1: Update Bazarr download functions**

Replace `curl -sS` calls with `curl_with_retry` in these functions:
- `try_bazarr_download_episode()` (~line 435)
- `try_bazarr_download_movie()` (~line 453)
- `try_translate_to_lang()` (~line 472)
- `trigger_arr_search_episode()` (~line 492)
- `trigger_arr_search_movie()` (~line 506)

Pattern for each — change:
```bash
curl -sS -o "$TMPDIR_RECOVERY/bazarr_episode_dl.out" -w "%{http_code}" \
```
to:
```bash
curl_with_retry -sS -o "$TMPDIR_RECOVERY/bazarr_episode_dl.out" -w '%{http_code}' \
```

Apply the same replacement for all 5 functions. Note: `-w '%{http_code}'` single-quoted to match the `curl_with_retry` contract.

**Step 2: Ensure attempt counters only increment after retries exhausted**

This is already the case — `state_inc_col` is called after the curl call returns, and `curl_with_retry` only returns after all retries are done. No changes needed.

**Step 3: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/bazarr_subtitle_recovery.sh
cp automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
bash -n scripts/bazarr_subtitle_recovery.sh
```

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
git commit -m "feat(subtitles): add curl retry logic to recovery API calls"
```

---

### Task 5: Integrate `curl_with_retry()` into Dedupe and Batch Extractor

**Files:**
- Modify: `automation/scripts/subtitles/lib_subtitle_common.sh` (Bazarr rescan functions)
- Modify: `automation/scripts/subtitles/library_subtitle_dedupe.sh` (uses rescan via lib)
- Modify: `automation/scripts/subtitles/batch_extract_embedded.sh` (Discord notification curl)

**Step 1: Update Bazarr rescan functions in shared lib**

In `lib_subtitle_common.sh`, replace the `curl` calls in `bazarr_scan_disk_movie()` and `bazarr_scan_disk_series()` (lines ~545-562):

```bash
bazarr_scan_disk_movie() {
  local radarr_id="$1" bazarr_url="$2" api_key="$3"
  local http_code
  http_code="$(curl_with_retry -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${api_key}" \
    "${bazarr_url}/api/movies?radarrid=${radarr_id}&action=scan-disk")"
  log "BAZARR_RESCAN movie id=${radarr_id} http=${http_code}"
  [[ "$http_code" == "204" || "$http_code" == "200" ]]
}

bazarr_scan_disk_series() {
  local sonarr_id="$1" bazarr_url="$2" api_key="$3"
  local http_code
  http_code="$(curl_with_retry -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${api_key}" \
    "${bazarr_url}/api/series?seriesid=${sonarr_id}&action=scan-disk")"
  log "BAZARR_RESCAN series id=${sonarr_id} http=${http_code}"
  [[ "$http_code" == "204" || "$http_code" == "200" ]]
}
```

**Step 2: Update batch extractor Discord curl**

In `batch_extract_embedded.sh` `notify_discord()` function (~line 302), replace:
```bash
curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
```
with:
```bash
curl_with_retry -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
```

**Step 3: Syntax check and sync all compat copies**

```bash
bash -n automation/scripts/subtitles/lib_subtitle_common.sh
bash -n automation/scripts/subtitles/library_subtitle_dedupe.sh
bash -n automation/scripts/subtitles/batch_extract_embedded.sh
cp automation/scripts/subtitles/lib_subtitle_common.sh scripts/lib_subtitle_common.sh
cp automation/scripts/subtitles/batch_extract_embedded.sh scripts/batch_extract_embedded.sh
bash -n scripts/lib_subtitle_common.sh
bash -n scripts/batch_extract_embedded.sh
```

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/lib_subtitle_common.sh scripts/lib_subtitle_common.sh \
  automation/scripts/subtitles/batch_extract_embedded.sh scripts/batch_extract_embedded.sh
git commit -m "feat(subtitles): add curl retry to Bazarr rescan + batch extractor Discord"
```

---

### Task 6: Batch `jsonish_to_json` in Recovery

**Files:**
- Modify: `automation/scripts/subtitles/bazarr_subtitle_recovery.sh`

**Step 1: Add batch conversion function**

Add after the `source` line and DB init, before the `--report` section:

```bash
# ---------------------------------------------------------------------------
# Batch convert Python-repr strings to JSON (one Python call per media type)
# ---------------------------------------------------------------------------
batch_jsonish_to_json() {
  local input_file="$1" output_file="$2"
  python3 -c "
import sys, json, ast

for line in open(sys.argv[1], 'r'):
    line = line.rstrip('\n')
    if not line:
        continue
    parts = line.split('|', 2)
    if len(parts) < 3:
        continue
    # parts[0] = id fields (pipe-separated), parts handled by caller
    # Actually we receive: id_fields<TAB>missing_raw<TAB>subtitles_raw
    fields = line.split('\t')
    if len(fields) < 3:
        continue
    ids = fields[0]
    try:
        missing = json.dumps(ast.literal_eval(fields[1])) if fields[1] else '[]'
    except Exception:
        missing = '[]'
    try:
        subs = json.dumps(ast.literal_eval(fields[2])) if fields[2] else '[]'
    except Exception:
        subs = '[]'
    print(ids + '\t' + missing + '\t' + subs)
" "$input_file" > "$output_file"
}
```

**Step 2: Replace the episode main loop**

Change the episode processing section. Before the loop, dump and convert:

```bash
# Bulk export + convert episodes
EPISODE_RAW="$TMPDIR_RECOVERY/episodes_raw.tsv"
EPISODE_JSON="$TMPDIR_RECOVERY/episodes_json.tsv"
sqlite3 -separator $'\t' "$BAZARR_DB" "
  SELECT sonarrEpisodeId || '|' || sonarrSeriesId, missing_subtitles, subtitles
  FROM table_episodes
  WHERE missing_subtitles IS NOT NULL AND missing_subtitles <> '[]'
  ORDER BY sonarrEpisodeId;
" > "$EPISODE_RAW"
batch_jsonish_to_json "$EPISODE_RAW" "$EPISODE_JSON"

while IFS=$'\t' read -r id_fields missing_json subtitles_json; do
  [[ -n "$id_fields" ]] || continue
  IFS='|' read -r episode_id series_id <<< "$id_fields"
  scanned=$((scanned + 1))
  if [[ "$MAX_ITEMS" -gt 0 && "$handled" -ge "$MAX_ITEMS" ]]; then
    break
  fi
  handled=$((handled + 1))
  process_item "episode" "$episode_id" "$series_id" "$missing_json" "$subtitles_json"
done < "$EPISODE_JSON"
```

Do the same for movies:

```bash
# Bulk export + convert movies
MOVIE_RAW="$TMPDIR_RECOVERY/movies_raw.tsv"
MOVIE_JSON="$TMPDIR_RECOVERY/movies_json.tsv"
sqlite3 -separator $'\t' "$BAZARR_DB" "
  SELECT radarrId, missing_subtitles, subtitles
  FROM table_movies
  WHERE missing_subtitles IS NOT NULL AND missing_subtitles <> '[]'
  ORDER BY radarrId;
" > "$MOVIE_RAW"
batch_jsonish_to_json "$MOVIE_RAW" "$MOVIE_JSON"

while IFS=$'\t' read -r movie_id missing_json subtitles_json; do
  [[ -n "$movie_id" ]] || continue
  scanned=$((scanned + 1))
  if [[ "$MAX_ITEMS" -gt 0 && "$handled" -ge "$MAX_ITEMS" ]]; then
    break
  fi
  handled=$((handled + 1))
  process_item "movie" "$movie_id" "0" "$missing_json" "$subtitles_json"
done < "$MOVIE_JSON"
```

**Step 3: Update `process_item()` to accept pre-converted JSON**

Change `process_item()` so it no longer calls `jsonish_to_json` on `missing_raw` and `subtitles_raw`:

Replace lines ~809-811:
```bash
  missing_json="$(printf '%s' "$missing_raw" | jsonish_to_json 2>/dev/null || echo '[]')"
  subtitles_json="$(printf '%s' "$subtitles_raw" | jsonish_to_json 2>/dev/null || echo '[]')"
```
with:
```bash
  missing_json="$missing_raw"
  subtitles_json="$subtitles_raw"
```

Keep the mid-loop re-reads (after Bazarr download and translate attempts) — those still need `jsonish_to_json` inline since they query fresh data. Use the existing `jsonish_to_json` function for those (keep it defined, just not used in the hot path).

**Step 4: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/bazarr_subtitle_recovery.sh
cp automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
bash -n scripts/bazarr_subtitle_recovery.sh
```

**Step 5: Commit**

```bash
git add automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
git commit -m "perf(subtitles): batch jsonish_to_json — ~400 Python calls reduced to 2"
```

---

### Task 7: Discord Notifications for Dedupe

**Files:**
- Modify: `automation/scripts/subtitles/library_subtitle_dedupe.sh`

**Step 1: Add DISCORD_WEBHOOK_URL default and notification function**

Add after line 10 (`BAZARR_API_KEY` default):

```bash
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"
```

Add the `--discord-webhook` CLI flag in the arg parser and usage:

In usage(), add:
```
  --discord-webhook URL  Discord webhook URL for notifications (default: from env DISCORD_WEBHOOK_URL)
```

In the case block, add:
```bash
    --discord-webhook)
      DISCORD_WEBHOOK_URL="${2:-}"
      shift 2
      ;;
```

**Step 2: Add notification at end of script**

Add before the final `log "Done ..."` line (line 551):

```bash
# Discord notification (only when changes were made)
if [[ "$DRY_RUN" -eq 0 && "$changed" -gt 0 && -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  discord_body="**Scanned:** $scanned · **Processed:** $processed · **Changed:** $changed"$'\n'"**Converted:** $total_converted · **Stripped:** $total_stripped · **Removed:** $total_removed · **Renamed:** $total_renamed · **Rescans:** $rescan_count"
  notify_discord_embed "Subtitle Dedupe Complete" "$discord_body" 3066993
fi
```

This uses the `notify_discord_embed()` from the shared lib (already sourced).

**Step 3: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/library_subtitle_dedupe.sh
# library_subtitle_dedupe.sh doesn't have a compat copy in scripts/ — check:
ls scripts/library_subtitle_dedupe.sh 2>/dev/null && cp automation/scripts/subtitles/library_subtitle_dedupe.sh scripts/library_subtitle_dedupe.sh
```

Wait — per the initial exploration, there is no `scripts/library_subtitle_dedupe.sh` compat copy. The dedupe script is only in `automation/scripts/subtitles/`. No sync needed.

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/library_subtitle_dedupe.sh
git commit -m "feat(subtitles): add Discord notifications to dedupe script"
```

---

### Task 8: `--since` Flag for Recovery

**Files:**
- Modify: `automation/scripts/subtitles/bazarr_subtitle_recovery.sh`

**Step 1: Add SINCE_MINUTES variable and CLI flag**

Add after `REPORT_MODE=0` (line 23):

```bash
SINCE_MINUTES=0
```

In usage(), add:
```
  --since MINUTES       Only process items with stale or no state (default: 0 = all)
```

In the case block, add:
```bash
    --since) SINCE_MINUTES="${2:-}"; shift 2 ;;
```

Add `SINCE_MINUTES` to the numeric validation loop:
```bash
for n in "$MAX_ITEMS" ... "$SINCE_MINUTES"; do
```

**Step 2: Add filtering logic to main loops**

Add a helper function after `id_in_list()`:

```bash
should_skip_since() {
  local media_type="$1" media_id="$2"
  [[ "$SINCE_MINUTES" -gt 0 ]] || return 1  # --since not set, don't skip
  local cutoff_ts last_update_ts
  cutoff_ts="$(date -d "$SINCE_MINUTES minutes ago" +%s)"
  # Check if ANY lang for this media was updated recently
  last_update_ts="$(sqlite3 "$STATE_DB" "
    SELECT COALESCE(MAX(strftime('%s', updated_at)), 0)
    FROM recovery_state
    WHERE media_type='$media_type' AND media_id=$media_id;
  ")"
  [[ -n "$last_update_ts" && "$last_update_ts" -gt 0 && "$last_update_ts" -gt "$cutoff_ts" ]]
}
```

In each main loop (episodes and movies), add the skip check after `handled=$((handled + 1))`:

```bash
  if should_skip_since "$media_type" "$media_id"; then
    continue
  fi
```

For episodes, the media_type is "episode" and media_id is episode_id. For movies, media_type is "movie" and media_id is movie_id.

Actually, insert the check before incrementing handled, to avoid counting skipped items:

```bash
  scanned=$((scanned + 1))
  if [[ "$MAX_ITEMS" -gt 0 && "$handled" -ge "$MAX_ITEMS" ]]; then
    break
  fi
  if should_skip_since "episode" "$episode_id"; then
    continue
  fi
  handled=$((handled + 1))
```

Same for movies with `"movie" "$movie_id"`.

**Step 3: Update log line**

Change the start log to include since:
```bash
if [[ "$SINCE_MINUTES" -gt 0 ]]; then
  log "Start recovery max_items=$MAX_ITEMS since=${SINCE_MINUTES}m"
else
  log "Start recovery max_items=$MAX_ITEMS"
fi
```

**Step 4: Syntax check and sync compat**

```bash
bash -n automation/scripts/subtitles/bazarr_subtitle_recovery.sh
cp automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
bash -n scripts/bazarr_subtitle_recovery.sh
```

**Step 5: Commit**

```bash
git add automation/scripts/subtitles/bazarr_subtitle_recovery.sh scripts/bazarr_subtitle_recovery.sh
git commit -m "feat(subtitles): add --since flag to recovery for incremental runs"
```

---

### Task 9: Final Validation and Cleanup

**Step 1: Syntax check all modified scripts**

```bash
bash -n automation/scripts/subtitles/lib_subtitle_common.sh
bash -n automation/scripts/subtitles/arr_profile_extract_on_import.sh
bash -n automation/scripts/subtitles/bazarr_subtitle_recovery.sh
bash -n automation/scripts/subtitles/library_subtitle_dedupe.sh
bash -n automation/scripts/subtitles/batch_extract_embedded.sh
bash -n scripts/lib_subtitle_common.sh
bash -n scripts/arr_profile_extract_on_import.sh
bash -n scripts/bazarr_subtitle_recovery.sh
bash -n scripts/batch_extract_embedded.sh
```

**Step 2: Dry-run tests**

```bash
# Recovery dry-run
automation/scripts/subtitles/bazarr_subtitle_recovery.sh --dry-run --max-items 5
# Recovery with --since
automation/scripts/subtitles/bazarr_subtitle_recovery.sh --dry-run --since 30 --max-items 5
# Recovery report
automation/scripts/subtitles/bazarr_subtitle_recovery.sh --report
# Dedupe dry-run
automation/scripts/subtitles/library_subtitle_dedupe.sh --dry-run --max-files 5
```

**Step 3: Verify old hook scripts are deleted**

```bash
test ! -f automation/scripts/subtitles/sonarr_profile_extract_on_import.sh
test ! -f automation/scripts/subtitles/radarr_profile_extract_on_import.sh
test ! -f scripts/sonarr_profile_extract_on_import.sh
test ! -f scripts/radarr_profile_extract_on_import.sh
```

**Step 4: Update thin wrapper scripts if they exist**

Check if `extract_fr_embedded_for_profile5.sh` or `extract_zh_embedded_for_chinese_profiles.sh` reference the old hooks — they shouldn't, but verify:

```bash
grep -l "sonarr_profile_extract\|radarr_profile_extract" automation/scripts/subtitles/*.sh scripts/*.sh || echo "No references to old hooks found"
```

**Step 5: Final commit if any cleanup was needed**

```bash
# Only if changes were made
git status --porcelain | grep -q . && git add -A && git commit -m "chore(subtitles): final cleanup after improvements"
```

---

### Post-Implementation Checklist

- [ ] Update Sonarr Connect webhook path: `scripts/arr_profile_extract_on_import.sh`
- [ ] Update Radarr Connect webhook path: `scripts/arr_profile_extract_on_import.sh`
- [ ] Verify Discord notifications arrive from dedupe (wait for next `*/5` cron run with changes)
- [ ] Verify recovery `--since 30` works with cron (optional — can update cron later)
- [ ] Update MEMORY.md with changes made
