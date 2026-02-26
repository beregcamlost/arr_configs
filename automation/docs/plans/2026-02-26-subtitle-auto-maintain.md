# Subtitle Auto-Maintain Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automate muxing GOOD external SRTs into MKVs and stripping BAD embedded tracks, with incremental state tracking, Emby integration, and safety checks against converter/playback conflicts.

**Architecture:** New `auto-maintain` subcommand in `subtitle_quality_manager.sh` with quick mode (`--since N`) and full mode (incremental state DB). Shared `emby_refresh_item()` and `is_file_being_played()` helpers in `lib_subtitle_common.sh`. All scripts that modify media files trigger Emby per-item refresh.

**Tech Stack:** Bash, SQLite, ffmpeg/ffprobe, Emby REST API, Bazarr API

---

### Task 1: Add Emby helpers to shared library

**Files:**
- Modify: `automation/scripts/subtitles/lib_subtitle_common.sh` (append after line 649)

**Step 1: Add `is_file_being_played()` function**

Append to end of `lib_subtitle_common.sh`:

```bash
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
```

**Step 2: Add `emby_refresh_item()` function**

Append directly after `is_file_being_played()`:

```bash
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
```

**Step 3: Syntax check**

Run: `bash -n automation/scripts/subtitles/lib_subtitle_common.sh`
Expected: No output (clean)

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/lib_subtitle_common.sh
git commit -m "feat(lib): add emby_refresh_item() and is_file_being_played() helpers"
```

---

### Task 2: Add `auto-maintain` CLI parsing and state DB

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh` (CLI section lines 7-86, command dispatch lines 553-557)

**Step 1: Add new variables for auto-maintain**

After line 17 (`LOG_LEVEL="info"`), add:

```bash
PATH_PREFIX_ROOT=""
SINCE_MINUTES=0
STATE_DIR="/APPBOX_DATA/storage/.subtitle-quality-state"
EMBY_URL="${EMBY_URL:-}"
EMBY_API_KEY="${EMBY_API_KEY:-}"
```

**Step 2: Add `auto-maintain` to command case**

Change line 58 from:
```bash
  audit|mux|strip) ;;
```
to:
```bash
  audit|mux|strip|auto-maintain) ;;
```

**Step 3: Add new CLI flags to the while loop**

In the `while [[ $# -gt 0 ]]` loop (lines 63-77), add cases before the `*` catch-all:

```bash
    --path-prefix) PATH_PREFIX_ROOT="${2:-}"; shift 2 ;;
    --since)       SINCE_MINUTES="${2:-0}"; shift 2 ;;
    --emby-url)    EMBY_URL="${2:-}"; shift 2 ;;
    --emby-api-key) EMBY_API_KEY="${2:-}"; shift 2 ;;
```

**Step 4: Update validation block**

After line 81 (`--path is required`), add:

```bash
if [[ "$COMMAND" == "auto-maintain" ]] && [[ -z "$PATH_PREFIX_ROOT" ]]; then
  echo "--path-prefix is required for auto-maintain." >&2; exit 1
fi
```

**Step 5: Add state DB initialization function**

After the `debug()` function (line 97), add:

```bash
# ---------------------------------------------------------------------------
# State DB for incremental auditing
# ---------------------------------------------------------------------------

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
```

**Step 6: Add `auto-maintain` to command dispatch**

Change the final case block from:
```bash
case "$COMMAND" in
  audit) cmd_audit ;;
  mux)   cmd_mux ;;
  strip) cmd_strip ;;
esac
```
to:
```bash
case "$COMMAND" in
  audit)          cmd_audit ;;
  mux)            cmd_mux ;;
  strip)          cmd_strip ;;
  auto-maintain)  cmd_auto_maintain ;;
esac
```

**Step 7: Syntax check**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: No output (clean — `cmd_auto_maintain` doesn't exist yet but won't be called during syntax check)

Wait — bash -n WILL fail on undefined function in case dispatch. Add a stub:

Before the case dispatch, add:

```bash
cmd_auto_maintain() {
  log "auto-maintain: not yet implemented"
}
```

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: No output (clean)

**Step 8: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): add auto-maintain CLI parsing and state DB schema"
```

---

### Task 3: Implement `cmd_auto_maintain()` — quick mode

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh` (replace stub `cmd_auto_maintain`)

**Step 1: Replace the stub with the full function**

The function handles both quick mode (`--since N`) and full mode. This task implements the quick mode path. The function should:

1. Iterate all media dirs under `--path-prefix` (tv, movies, tvanimated, moviesanimated)
2. Find MKV files (filtered by `--since` if set — only files with SRTs modified recently)
3. For each MKV with external SRTs:
   a. Check `is_file_being_converted()` — skip if converter running
   b. Check `is_file_being_played()` — skip if active playback
   c. Audit external SRTs (fast: `analyze_srt_file` + `score_subtitle`)
   d. If any GOOD SRTs exist: mux them into MKV (reuse mux logic from `cmd_mux`)
   e. After mux: check embedded tracks for BAD ratings with GOOD replacement → strip
   f. After any modification: call `emby_refresh_item()` and Bazarr scan-disk
4. Discord summary

```bash
cmd_auto_maintain() {
  log "auto-maintain: path=$PATH_PREFIX_ROOT since=$SINCE_MINUTES dry_run=$DRY_RUN"

  local state_db="$STATE_DIR/subtitle_quality_state.db"
  [[ "$SINCE_MINUTES" -eq 0 ]] && init_state_db "$state_db"

  local total_files=0 muxed_files=0 muxed_tracks=0 stripped_files=0 stripped_tracks=0
  local skipped_converter=0 skipped_playback=0 warned=0

  # Find MKV files across all media dirs
  local -a mkv_files=()
  while IFS= read -r mkv_file; do
    [[ -z "$mkv_file" ]] && continue

    # In quick mode (--since), only process MKVs that have recently modified SRTs
    if [[ "$SINCE_MINUTES" -gt 0 ]]; then
      local stem dir
      stem="$(basename "${mkv_file%.mkv}")"
      dir="$(dirname "$mkv_file")"
      local recent_srt
      recent_srt="$(find "$dir" -maxdepth 1 -name "${stem}.*.srt" -type f -mmin "-${SINCE_MINUTES}" 2>/dev/null | head -1)"
      [[ -z "$recent_srt" ]] && continue
    fi

    mkv_files+=("$mkv_file")
  done < <(find "$PATH_PREFIX_ROOT" -type f -name "*.mkv" 2>/dev/null | sort)

  log "auto-maintain: found ${#mkv_files[@]} candidate files"

  for mkv_file in "${mkv_files[@]}"; do
    local basename dir name_stem duration
    basename="$(basename "$mkv_file")"
    dir="$(dirname "$mkv_file")"
    name_stem="${basename%.mkv}"

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

    duration="$(get_video_duration "$mkv_file")"

    # --- Phase 1: Audit & mux external SRTs ---
    local -a good_srts=() good_langs=() warn_srts=()
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
          good_srts+=("$srt_file")
          good_langs+=("$ext_lang")
          ;;
        WARN)
          warn_srts+=("$srt_file")
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
        muxed_tracks=$((muxed_tracks + ${#good_srts[@]}))
        muxed_files=$((muxed_files + 1))
      else
        local -a ffmpeg_cmd=(ffmpeg -y -v quiet -i "$mkv_file")
        local -a map_args=(-map 0)
        local existing_sub_count
        existing_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$mkv_file" 2>/dev/null | jq '.streams | length')"

        for ((i=0; i<${#good_srts[@]}; i++)); do
          ffmpeg_cmd+=(-i "${good_srts[$i]}")
          map_args+=(-map "$((i + 1)):0")
          local metadata_idx=$((existing_sub_count + i))
          map_args+=(-metadata:s:s:${metadata_idx} "language=${good_langs[$i]}")
        done

        local tmp_out="${mkv_file}.subtmp.mkv"
        if "${ffmpeg_cmd[@]}" "${map_args[@]}" -c copy "$tmp_out" </dev/null 2>/dev/null; then
          local new_sub_count expected
          new_sub_count="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$tmp_out" 2>/dev/null | jq '.streams | length')"
          expected=$((existing_sub_count + ${#good_srts[@]}))

          if [[ "$new_sub_count" -eq "$expected" ]]; then
            mv "$tmp_out" "$mkv_file"
            for sf in "${good_srts[@]}"; do rm -f "$sf"; done
            muxed_tracks=$((muxed_tracks + ${#good_srts[@]}))
            muxed_files=$((muxed_files + 1))
            log "MUXED ${#good_srts[@]} sub(s) into: $basename"
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

    # --- Phase 2: Auto-strip BAD embedded tracks ---
    # Only strip if we just muxed a GOOD replacement in the same language
    if [[ "$muxed_files" -gt 0 || "$SINCE_MINUTES" -eq 0 ]]; then
      local embedded_json emb_count
      embedded_json="$(get_embedded_subs "$mkv_file")"
      emb_count="$(jq 'length' <<<"$embedded_json")"

      local -a strip_indices=()
      for ((i=0; i<emb_count; i++)); do
        local stream_idx lang codec_name title
        stream_idx="$(jq -r ".[$i].index" <<<"$embedded_json")"
        lang="$(jq -r ".[$i].tags.language" <<<"$embedded_json")"
        title="$(jq -r ".[$i].tags.title" <<<"$embedded_json")"
        codec_name="$(jq -r ".[$i].codec_name" <<<"$embedded_json")"

        # Extract to temp file for scoring
        local tmpfile="/tmp/sub_auto_${$}_${stream_idx}.srt"
        if ! ffmpeg -v quiet -i "$mkv_file" -map "0:${stream_idx}" -f srt "$tmpfile" </dev/null 2>/dev/null; then
          rm -f "$tmpfile"
          continue
        fi

        local analysis cues first_sec last_sec mojibake watermarks emb_rating
        analysis="$(analyze_srt_file "$tmpfile")"
        read -r cues first_sec last_sec mojibake watermarks <<<"$analysis"
        # Also check title for watermarks
        if echo "$title" | grep -qiE "$WATERMARK_PATTERNS" 2>/dev/null; then
          watermarks=1
        fi
        emb_rating="$(score_subtitle "$cues" "$first_sec" "$last_sec" "$duration" "$mojibake" "$watermarks")"
        rm -f "$tmpfile"

        if [[ "$emb_rating" == "BAD" ]]; then
          # Check if a GOOD replacement exists in same language (normalize en/eng)
          local norm_lang="${lang}"
          [[ "$norm_lang" == "eng" ]] && norm_lang="en"
          [[ "$norm_lang" == "spa" ]] && norm_lang="es"
          local has_good=0
          for ((j=0; j<emb_count; j++)); do
            [[ "$j" -eq "$i" ]] && continue
            local other_lang
            other_lang="$(jq -r ".[$j].tags.language" <<<"$embedded_json")"
            [[ "$other_lang" == "eng" ]] && other_lang="en"
            [[ "$other_lang" == "spa" ]] && other_lang="es"
            if [[ "$other_lang" == "$norm_lang" ]]; then
              has_good=1
              break
            fi
          done
          if [[ "$has_good" -eq 1 ]]; then
            strip_indices+=("$stream_idx")
            log "AUTO-STRIP BAD embedded idx=$stream_idx lang=$lang: $basename"
          fi
        fi
      done

      if [[ ${#strip_indices[@]} -gt 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
        local -a strip_cmd=(ffmpeg -y -v quiet -i "$mkv_file" -map 0)
        for idx in "${strip_indices[@]}"; do
          strip_cmd+=(-map "-0:${idx}")
        done
        strip_cmd+=(-c copy)
        local strip_tmp="${mkv_file}.striptmp.mkv"
        if "${strip_cmd[@]}" "$strip_tmp" </dev/null 2>/dev/null && [[ -s "$strip_tmp" ]]; then
          mv "$strip_tmp" "$mkv_file"
          stripped_tracks=$((stripped_tracks + ${#strip_indices[@]}))
          stripped_files=$((stripped_files + 1))
          log "STRIPPED ${#strip_indices[@]} BAD track(s) from: $basename"
        else
          rm -f "$strip_tmp"
          log "FAIL strip: $basename"
        fi
      elif [[ ${#strip_indices[@]} -gt 0 ]]; then
        log "[DRY-RUN] Would strip ${#strip_indices[@]} BAD track(s) from: $basename"
        stripped_tracks=$((stripped_tracks + ${#strip_indices[@]}))
        stripped_files=$((stripped_files + 1))
      fi
    fi

    # --- Phase 3: Emby + Bazarr refresh ---
    if [[ "$DRY_RUN" -eq 0 ]] && [[ "$muxed_files" -gt 0 || "$stripped_files" -gt 0 ]]; then
      emby_refresh_item "$mkv_file" || log "WARN: Emby refresh failed (non-fatal)"
    fi

    # Update state DB (full mode only)
    if [[ "$SINCE_MINUTES" -eq 0 ]] && [[ "$DRY_RUN" -eq 0 ]]; then
      local current_mtime
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      sqlite3 "$state_db" "INSERT OR REPLACE INTO file_audits (file_path, mtime, last_audit_ts, action_taken) VALUES ('$(sql_escape "$mkv_file")', $current_mtime, $(date +%s), 'processed');" 2>/dev/null || true
    fi
  done

  # Bazarr rescan (batch by series/movie, non-fatal)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ "$muxed_files" -gt 0 || "$stripped_files" -gt 0 ]] && [[ -n "$BAZARR_API_KEY" ]]; then
    # Deduplicate rescans by parent directory
    local -A rescanned=()
    for mkv_file in "${mkv_files[@]}"; do
      local show_dir
      show_dir="$(echo "$mkv_file" | sed 's|/Season.*||' | sed 's|/$||')"
      [[ -n "${rescanned[$show_dir]:-}" ]] && continue
      rescanned["$show_dir"]=1

      if [[ "$show_dir" == *"/tv/"* ]] || [[ "$show_dir" == *"/tvanimated/"* ]]; then
        local sonarr_id
        sonarr_id="$(sqlite3 "$BAZARR_DB" "SELECT sonarrSeriesId FROM table_shows WHERE path LIKE '%$(sql_escape "$(basename "$show_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
        [[ -n "$sonarr_id" ]] && { bazarr_scan_disk_series "$sonarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed"; }
      elif [[ "$show_dir" == *"/movies/"* ]] || [[ "$show_dir" == *"/moviesanimated/"* ]]; then
        local radarr_id
        radarr_id="$(sqlite3 "$BAZARR_DB" "SELECT radarrId FROM table_movies WHERE path LIKE '%$(sql_escape "$(basename "$show_dir")")%' LIMIT 1;" 2>/dev/null || echo "")"
        [[ -n "$radarr_id" ]] && { bazarr_scan_disk_movie "$radarr_id" "$BAZARR_URL" "$BAZARR_API_KEY" || log "WARN: Bazarr rescan failed"; }
      fi
    done
  fi

  log "auto-maintain done: files=$total_files muxed=$muxed_files($muxed_tracks tracks) stripped=$stripped_files($stripped_tracks tracks) warned=$warned skipped_converter=$skipped_converter skipped_playback=$skipped_playback"

  # Discord notification (non-fatal)
  if [[ "$DRY_RUN" -eq 0 ]] && [[ $((muxed_files + stripped_files)) -gt 0 ]]; then
    local mode="quick"
    [[ "$SINCE_MINUTES" -eq 0 ]] && mode="full"
    notify_discord_embed "Subtitle Auto-Maintain ($mode)" \
      "$(printf "Muxed: %d file(s), %d track(s)\nStripped: %d file(s), %d track(s)\nWARN (manual review): %d\nSkipped (converter): %d\nSkipped (playback): %d" \
        "$muxed_files" "$muxed_tracks" "$stripped_files" "$stripped_tracks" "$warned" "$skipped_converter" "$skipped_playback")" \
      3066993 || log "WARN: Discord notification failed (non-fatal)"
  fi
}
```

**Step 2: Syntax check**

Run: `bash -n automation/scripts/subtitles/subtitle_quality_manager.sh`
Expected: No output (clean)

**Step 3: Dry-run test on Evil Season 1 (already clean — should find nothing)**

Run: `source .env && bash automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media/tv/Evil --since 60 --dry-run`
Expected: `found 0 candidate files` (no recent SRTs since Evil is already muxed)

**Step 4: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): implement auto-maintain with quick+full mode, Emby integration"
```

---

### Task 4: Add incremental state DB filtering to full mode

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh` (inside `cmd_auto_maintain`)

**Step 1: Add state DB check in the main loop**

In `cmd_auto_maintain()`, after the `is_file_being_played` check and before getting `duration`, add incremental filtering for full mode:

```bash
    # Full mode: skip files that haven't changed since last audit
    if [[ "$SINCE_MINUTES" -eq 0 ]]; then
      local current_mtime stored_mtime
      current_mtime="$(stat -c %Y "$mkv_file" 2>/dev/null || echo 0)"
      stored_mtime="$(sqlite3 "$state_db" "SELECT mtime FROM file_audits WHERE file_path='$(sql_escape "$mkv_file")';" 2>/dev/null || echo 0)"
      if [[ "$current_mtime" -eq "$stored_mtime" ]] && [[ "$stored_mtime" -gt 0 ]]; then
        debug "SKIP (unchanged): $basename"
        continue
      fi
    fi
```

**Step 2: Test full mode dry-run on small dir**

Run: `source .env && bash automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media/moviesanimated --dry-run`
Expected: Lists candidate files to mux (moviesanimated has 12 MKVs with external SRTs)

**Step 3: Commit**

```bash
git add automation/scripts/subtitles/subtitle_quality_manager.sh
git commit -m "feat(sub-quality): add incremental state DB filtering for full mode"
```

---

### Task 5: Integrate Emby refresh into existing scripts

**Files:**
- Modify: `automation/scripts/subtitles/arr_profile_extract_on_import.sh` (near line 201)
- Modify: `automation/scripts/subtitles/library_subtitle_dedupe.sh` (near line 555)
- Modify: `automation/scripts/transcode/library_codec_manager.sh` (after file swap in convert)

**Step 1: Add Emby refresh to import hook**

In `arr_profile_extract_on_import.sh`, find the line near the end that logs completion stats (after extract_target calls, before the closing of main). Add:

```bash
# Trigger Emby refresh for the imported file
if [[ -n "${EMBY_URL:-}" && -n "${EMBY_API_KEY:-}" ]]; then
  emby_refresh_item "$MEDIA_PATH" || log "WARN: Emby refresh failed (non-fatal)"
fi
```

**Step 2: Add Emby refresh to subtitle dedupe**

In `library_subtitle_dedupe.sh`, after the Bazarr rescan block (around line 555) and before the Discord notification, add:

```bash
# Emby refresh for changed files
if [[ "$DRY_RUN" -eq 0 && "$changed" -gt 0 && -n "${EMBY_URL:-}" && -n "${EMBY_API_KEY:-}" ]]; then
  for _path in "${changed_paths[@]}"; do
    emby_refresh_item "$_path" || log "WARN: Emby refresh failed for $(basename "$_path") (non-fatal)"
  done
fi
```

Note: This requires tracking changed file paths in the main loop. Add `local -a changed_paths=()` before the main loop, and `changed_paths+=("$vid")` where `changed` is incremented.

**Step 3: Add Emby refresh to codec converter**

In `library_codec_manager.sh`, find the convert subcommand section where the temp file is swapped with the original (look for `mv` of the converted output). After the successful swap, add:

```bash
# Refresh Emby metadata after codec conversion
emby_refresh_item "$original_path" || log "WARN: Emby refresh failed (non-fatal)"
```

Note: The codec manager sources its own lib. It needs to source `lib_subtitle_common.sh` OR we can add `emby_refresh_item` as a standalone function. Since the codec manager is in `automation/scripts/transcode/`, the simplest approach is to add a minimal Emby refresh function inline or source the subtitle lib. Check the codec manager's existing source statements first.

**Step 4: Syntax check all modified files**

Run:
```bash
bash -n automation/scripts/subtitles/arr_profile_extract_on_import.sh
bash -n automation/scripts/subtitles/library_subtitle_dedupe.sh
bash -n automation/scripts/transcode/library_codec_manager.sh
```

**Step 5: Sync compat copies**

```bash
cp automation/scripts/subtitles/subtitle_quality_manager.sh scripts/subtitle_quality_manager.sh
cp automation/scripts/subtitles/arr_profile_extract_on_import.sh scripts/arr_profile_extract_on_import.sh
# Dedupe and codec manager may also have compat copies — check
```

**Step 6: Commit**

```bash
git add -A
git commit -m "feat: add Emby per-item refresh to import hook, dedupe, and codec converter"
```

---

### Task 6: Add cron entries and E2E test

**Files:**
- Modify: `automation/configs/crontab.env-sourced`
- Test: Live run on small media directory

**Step 1: Add cron entries to tracked crontab**

Add these two entries to `automation/configs/crontab.env-sourced`:

```cron
# Subtitle quality auto-maintain: quick scan every 10 min (recently changed files)
*/10 * * * * /usr/bin/flock -n /tmp/library_subtitle_dedupe.lock /bin/bash -c 'source /config/berenstuff/.env && /bin/bash /config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --since 15 --path-prefix /APPBOX_DATA/storage/media --state-dir /APPBOX_DATA/storage/.subtitle-quality-state' >> /config/berenstuff/automation/logs/subtitle_quality_manager.log 2>&1
# Subtitle quality auto-maintain: full incremental scan daily 1 AM
0 1 * * * /usr/bin/flock -n /tmp/library_subtitle_dedupe.lock /bin/bash -c 'source /config/berenstuff/.env && /bin/bash /config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media --state-dir /APPBOX_DATA/storage/.subtitle-quality-state' >> /config/berenstuff/automation/logs/subtitle_quality_manager.log 2>&1
```

**Step 2: Install crontab**

Run: `crontab automation/configs/crontab.env-sourced`
Verify: `crontab -l | grep subtitle_quality`

**Step 3: E2E test — quick mode on moviesanimated**

Run: `source .env && bash automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --since 99999 --path-prefix /APPBOX_DATA/storage/media/moviesanimated --dry-run`

Expected output should show:
- Files found with external SRTs
- DRY-RUN mux decisions for GOOD SRTs
- Skip messages for WARN/BAD SRTs

**Step 4: E2E test — live quick mode on 1-2 files**

Pick a single movie directory with external SRTs and test live mux:

```bash
source .env && bash automation/scripts/subtitles/subtitle_quality_manager.sh auto-maintain --since 99999 --path-prefix "/APPBOX_DATA/storage/media/moviesanimated/[PICK ONE]"
```

Verify:
- External SRTs deleted
- MKV now has embedded subtitles matching the external langs
- Emby refresh triggered
- Bazarr scan-disk triggered

**Step 5: Commit crontab**

```bash
git add automation/configs/crontab.env-sourced
git commit -m "feat: add subtitle auto-maintain cron entries (quick */10 + full daily 1 AM)"
```

---

### Task 7: Update usage text and sync compat copies

**Files:**
- Modify: `automation/scripts/subtitles/subtitle_quality_manager.sh` (usage function)
- Copy: `scripts/subtitle_quality_manager.sh`

**Step 1: Update usage() to document auto-maintain**

Add auto-maintain section to the usage text:

```
Auto-maintain options:
  --path-prefix DIR     Root media directory to scan recursively (required)
  --since N             Only scan files with SRTs modified in last N minutes (quick mode)
  --state-dir DIR       State DB directory for incremental tracking (default: /APPBOX_DATA/storage/.subtitle-quality-state)
  --emby-url URL        Emby server URL (default: from EMBY_URL env)
  --emby-api-key KEY    Emby API key (default: from EMBY_API_KEY env)

Examples:
  subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media --since 15 --dry-run
  subtitle_quality_manager.sh auto-maintain --path-prefix /APPBOX_DATA/storage/media --state-dir /path/to/state
```

**Step 2: Sync all compat copies**

```bash
cp automation/scripts/subtitles/subtitle_quality_manager.sh scripts/subtitle_quality_manager.sh
bash -n scripts/subtitle_quality_manager.sh
```

**Step 3: Final syntax check all modified files**

```bash
bash -n automation/scripts/subtitles/subtitle_quality_manager.sh
bash -n automation/scripts/subtitles/lib_subtitle_common.sh
bash -n automation/scripts/subtitles/arr_profile_extract_on_import.sh
bash -n automation/scripts/subtitles/library_subtitle_dedupe.sh
bash -n scripts/subtitle_quality_manager.sh
```

**Step 4: Commit and push**

```bash
git add -A
git commit -m "feat(sub-quality): update usage, sync compat copies, final validation"
git push
```

---

### Task 8: Update memory

**Files:**
- Modify: `/config/.claude/projects/-config/memory/MEMORY.md`

Update the "Subtitle Auto-Maintain" section from "pending implementation" to completed, add cron schedule entries, note any implementation details discovered during development.
