#!/usr/bin/env bats
# test_compliance.bats — tests for enforce_one_per_lang, needs_upgrade DB helpers,
# and compliance command (subtitle compliance system)

setup() {
  load helpers/test_helper
  DB=/dev/null
  source "$SUBTITLES_DIR/lib_subtitle_common.sh"

  # Create temp dir for test state DBs and fixtures
  TEST_TMPDIR="$(mktemp -d)"
  TEST_STATE_DB="$TEST_TMPDIR/test_state.db"

  # Initialize the state DB (needs the manager's init_state_db)
  source "$SUBTITLES_DIR/subtitle_quality_manager.sh"
  init_state_db "$TEST_STATE_DB"
}

teardown() {
  rm -rf "$TEST_TMPDIR" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# needs_upgrade DB helpers — 6 tests
# ---------------------------------------------------------------------------

@test "upsert_needs_upgrade: inserts new entry" {
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "en" 0 "BAD" 1000 "external"
  local row
  row="$(sqlite3 "$TEST_STATE_DB" "SELECT lang, current_rating, current_score, source FROM needs_upgrade WHERE file_path='/media/tv/show/file.mkv' AND lang='en' AND forced=0;")"
  [[ "$row" == "en|BAD|1000|external" ]]
}

@test "upsert_needs_upgrade: updates existing entry (clears resolved_ts)" {
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "es" 0 "BAD" 500 "external"
  # Resolve it first
  resolve_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "es" 0
  # Upsert again — should clear resolved_ts
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "es" 0 "WARN" 800 "embedded"
  local resolved
  resolved="$(sqlite3 "$TEST_STATE_DB" "SELECT resolved_ts FROM needs_upgrade WHERE file_path='/media/tv/show/file.mkv' AND lang='es' AND forced=0;")"
  [[ -z "$resolved" || "$resolved" == "" ]]
  local rating
  rating="$(sqlite3 "$TEST_STATE_DB" "SELECT current_rating FROM needs_upgrade WHERE file_path='/media/tv/show/file.mkv' AND lang='es' AND forced=0;")"
  [[ "$rating" == "WARN" ]]
}

@test "resolve_needs_upgrade: sets resolved_ts on unresolved entry" {
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "fr" 0 "BAD" 200 "external"
  resolve_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/file.mkv" "fr" 0
  local resolved
  resolved="$(sqlite3 "$TEST_STATE_DB" "SELECT resolved_ts FROM needs_upgrade WHERE file_path='/media/tv/show/file.mkv' AND lang='fr' AND forced=0;")"
  [[ -n "$resolved" && "$resolved" -gt 0 ]]
}

@test "drain_upgrade_candidates: respects retry threshold" {
  # Insert entry with last_retry_ts = 0 (always eligible)
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/a.mkv" "en" 0 "BAD" 100 "external"
  # Insert entry with last_retry_ts = now (should be filtered out by 86400s threshold)
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/b.mkv" "en" 0 "BAD" 100 "external"
  touch_upgrade_retry "$TEST_STATE_DB" "/media/tv/show/b.mkv" "en" 0
  local result
  result="$(drain_upgrade_candidates "$TEST_STATE_DB" 86400 30 500)"
  # Only a.mkv should appear (b.mkv was just retried)
  [[ "$result" == *"/media/tv/show/a.mkv"* ]]
  [[ "$result" != *"/media/tv/show/b.mkv"* ]]
}

@test "drain_upgrade_candidates: excludes resolved entries" {
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/c.mkv" "en" 0 "BAD" 100 "external"
  resolve_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/c.mkv" "en" 0
  local result
  result="$(drain_upgrade_candidates "$TEST_STATE_DB" 86400 30 500)"
  [[ "$result" != *"/media/tv/show/c.mkv"* ]]
}

@test "drain_upgrade_candidates: excludes entries at max retries" {
  upsert_needs_upgrade "$TEST_STATE_DB" "/media/tv/show/d.mkv" "en" 0 "BAD" 100 "external"
  # Set retry_count to 30 (at max)
  sqlite3 "$TEST_STATE_DB" "UPDATE needs_upgrade SET retry_count=30 WHERE file_path='/media/tv/show/d.mkv';"
  local result
  result="$(drain_upgrade_candidates "$TEST_STATE_DB" 86400 30 500)"
  [[ "$result" != *"/media/tv/show/d.mkv"* ]]
}

# ---------------------------------------------------------------------------
# enforce_one_per_lang — 6 tests (unit-level, using mock ffprobe/ffmpeg)
# These test the grouping logic with pre-created SRT files
# ---------------------------------------------------------------------------

@test "enforce_one_per_lang: two externals same lang keeps higher score" {
  local media_dir="$TEST_TMPDIR/media"
  mkdir -p "$media_dir"
  # Create a dummy media file
  touch "$media_dir/test.mkv"

  # Create two SRTs for same language, different quality
  # Good SRT (many cues)
  local good_srt="$media_dir/test.en.srt"
  {
    for i in $(seq 1 500); do
      printf '%d\n%02d:%02d:%02d,000 --> %02d:%02d:%02d,500\nLine %d text content here\n\n' \
        "$i" $((i/3600)) $(((i%3600)/60)) $((i%60)) $((i/3600)) $(((i%3600)/60)) $((i%60+1)) "$i"
    done
  } > "$good_srt"

  # Bad SRT (few cues)
  local bad_srt="$media_dir/test.eng.srt"
  {
    for i in $(seq 1 5); do
      printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nShort line %d\n\n' "$i" "$i" $((i+1)) "$i"
    done
  } > "$bad_srt"

  # Mock ffprobe to return no embedded subs
  ffprobe() { echo '{"streams":[]}'; }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 0 strip_idx kept

  # good_srt should survive, bad_srt should be removed
  [[ -f "$good_srt" ]]
  [[ ! -f "$bad_srt" ]]

  unset -f ffprobe
}

@test "enforce_one_per_lang: forced and non-forced are independent groups" {
  local media_dir="$TEST_TMPDIR/media2"
  mkdir -p "$media_dir"
  touch "$media_dir/test.mkv"

  # Non-forced English SRT
  local en_srt="$media_dir/test.en.srt"
  {
    for i in $(seq 1 200); do
      printf '%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nNon-forced line %d\n\n' \
        "$i" $((i/60)) $((i%60)) $((i/60)) $((i%60+1)) "$i"
    done
  } > "$en_srt"

  # Forced English SRT
  local en_forced="$media_dir/test.en.forced.srt"
  {
    for i in $(seq 1 20); do
      printf '%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nForced line %d\n\n' \
        "$i" $((i/60)) $((i%60)) $((i/60)) $((i%60+1)) "$i"
    done
  } > "$en_forced"

  ffprobe() { echo '{"streams":[]}'; }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 0 strip_idx kept

  # Both should survive (different groups)
  [[ -f "$en_srt" ]]
  [[ -f "$en_forced" ]]

  unset -f ffprobe
}

@test "enforce_one_per_lang: single candidate is a no-op" {
  local media_dir="$TEST_TMPDIR/media3"
  mkdir -p "$media_dir"
  touch "$media_dir/test.mkv"

  local only_srt="$media_dir/test.en.srt"
  {
    for i in $(seq 1 100); do
      printf '%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nLine %d\n\n' \
        "$i" $((i/60)) $((i%60)) $((i/60)) $((i%60+1)) "$i"
    done
  } > "$only_srt"

  ffprobe() { echo '{"streams":[]}'; }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 0 strip_idx kept

  # File should still exist, no strips
  [[ -f "$only_srt" ]]
  [[ ${#strip_idx[@]} -eq 0 ]]

  unset -f ffprobe
}

@test "enforce_one_per_lang: dry-run does not delete losers" {
  local media_dir="$TEST_TMPDIR/media4"
  mkdir -p "$media_dir"
  touch "$media_dir/test.mkv"

  local srt1="$media_dir/test.en.srt"
  local srt2="$media_dir/test.eng.srt"
  {
    for i in $(seq 1 500); do
      printf '%d\n%02d:%02d:%02d,000 --> %02d:%02d:%02d,500\nGood line %d\n\n' \
        "$i" $((i/3600)) $(((i%3600)/60)) $((i%60)) $((i/3600)) $(((i%3600)/60)) $((i%60+1)) "$i"
    done
  } > "$srt1"
  {
    for i in $(seq 1 5); do
      printf '%d\n00:00:%02d,000 --> 00:00:%02d,500\nBad %d\n\n' "$i" "$i" $((i+1)) "$i"
    done
  } > "$srt2"

  ffprobe() { echo '{"streams":[]}'; }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 1 strip_idx kept

  # Both should still exist in dry-run
  [[ -f "$srt1" ]]
  [[ -f "$srt2" ]]

  unset -f ffprobe
}

@test "enforce_one_per_lang: populates kept_langs correctly" {
  local media_dir="$TEST_TMPDIR/media5"
  mkdir -p "$media_dir"
  touch "$media_dir/test.mkv"

  local en_srt="$media_dir/test.en.srt"
  {
    for i in $(seq 1 200); do
      printf '%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nLine %d\n\n' \
        "$i" $((i/60)) $((i%60)) $((i/60)) $((i%60+1)) "$i"
    done
  } > "$en_srt"

  local es_srt="$media_dir/test.es.srt"
  {
    for i in $(seq 1 200); do
      printf '%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nLinea %d\n\n' \
        "$i" $((i/60)) $((i%60)) $((i/60)) $((i%60+1)) "$i"
    done
  } > "$es_srt"

  ffprobe() { echo '{"streams":[]}'; }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 0 strip_idx kept

  # Both languages should be tracked as external
  [[ "${kept[en|0]}" == "external" ]]
  [[ "${kept[es|0]}" == "external" ]]

  unset -f ffprobe
}

@test "enforce_one_per_lang: bitmap sub loses to text sub" {
  local media_dir="$TEST_TMPDIR/media6"
  mkdir -p "$media_dir"
  touch "$media_dir/test.mkv"

  # External text sub — spread cues across full 3600s for good coverage
  local en_srt="$media_dir/test.en.srt"
  {
    for i in $(seq 1 500); do
      local t=$((i * 7))
      printf '%d\n%02d:%02d:%02d,000 --> %02d:%02d:%02d,500\nLine %d with enough text content here for scoring\n\n' \
        "$i" $((t/3600)) $(((t%3600)/60)) $((t%60)) $((t/3600)) $(((t%3600)/60)) $(((t%60)+1)) "$i"
    done
  } > "$en_srt"

  # Mock ffprobe: one bitmap embedded en sub
  ffprobe() {
    echo '{"streams":[{"index":2,"codec_name":"hdmv_pgs_subtitle","tags":{"language":"eng"},"disposition":{"forced":0}}]}'
  }
  export -f ffprobe

  local -a strip_idx=()
  declare -A kept=()
  enforce_one_per_lang "$media_dir/test.mkv" 3600 0 strip_idx kept

  # External text should win, bitmap embedded should be marked for strip
  [[ -f "$en_srt" ]]
  [[ ${#strip_idx[@]} -eq 1 ]]
  [[ "${strip_idx[0]}" == "2" ]]
  [[ "${kept[en|0]}" == "external" ]]

  unset -f ffprobe
}

# ---------------------------------------------------------------------------
# compliance command — 4 tests (functional-level via cmd_compliance)
# These require mocking external tools extensively
# ---------------------------------------------------------------------------

@test "compliance: reports NO_PROFILE for file without Bazarr profile" {
  local media_dir="$TEST_TMPDIR/compliance1"
  mkdir -p "$media_dir/movies/Test Movie (2024)"
  touch "$media_dir/movies/Test Movie (2024)/Test Movie (2024).mkv"

  # Override globals
  STATE_DIR="$TEST_TMPDIR"
  PATH_PREFIX_ROOT="$media_dir"
  BAZARR_DB="/nonexistent/bazarr.db"
  COMPLIANCE_FORMAT="text"
  COMPLIANCE_VERBOSE=1

  # Suppress ffprobe/get_embedded_subs calls
  get_embedded_subs() { echo '[]'; }
  export -f get_embedded_subs

  run cmd_compliance
  [[ "$output" == *"No profile"* ]] || [[ "$output" == *"NO_PROFILE"* ]] || [[ "$output" == *"no_profile"* ]]
}

@test "compliance: summary includes total count" {
  local media_dir="$TEST_TMPDIR/compliance2"
  mkdir -p "$media_dir"
  touch "$media_dir/test1.mkv"

  STATE_DIR="$TEST_TMPDIR"
  PATH_PREFIX_ROOT="$media_dir"
  BAZARR_DB="/nonexistent/bazarr.db"
  COMPLIANCE_FORMAT="text"
  COMPLIANCE_VERBOSE=0

  get_embedded_subs() { echo '[]'; }
  export -f get_embedded_subs

  run cmd_compliance
  [[ "$output" == *"Total files:"* ]]
  [[ "$output" == *"Compliance rate:"* ]]
}

@test "compliance: json format outputs valid JSON" {
  local media_dir="$TEST_TMPDIR/compliance3"
  mkdir -p "$media_dir"
  touch "$media_dir/test1.mkv"

  STATE_DIR="$TEST_TMPDIR"
  PATH_PREFIX_ROOT="$media_dir"
  BAZARR_DB="/nonexistent/bazarr.db"
  COMPLIANCE_FORMAT="json"
  COMPLIANCE_VERBOSE=0

  get_embedded_subs() { echo '[]'; }
  export -f get_embedded_subs

  # Capture stdout only (cmd_compliance logs go to stderr)
  local json_out
  json_out="$(cmd_compliance 2>/dev/null)"
  echo "$json_out" | jq -e '.total' >/dev/null
  echo "$json_out" | jq -e '.compliance_rate' >/dev/null
}

@test "compliance: counts no_profile correctly" {
  local media_dir="$TEST_TMPDIR/compliance4"
  mkdir -p "$media_dir"
  touch "$media_dir/a.mkv"
  touch "$media_dir/b.mkv"

  STATE_DIR="$TEST_TMPDIR"
  PATH_PREFIX_ROOT="$media_dir"
  BAZARR_DB="/nonexistent/bazarr.db"
  COMPLIANCE_FORMAT="json"
  COMPLIANCE_VERBOSE=0

  get_embedded_subs() { echo '[]'; }
  export -f get_embedded_subs

  # Capture stdout only
  local json_out np
  json_out="$(cmd_compliance 2>/dev/null)"
  np="$(echo "$json_out" | jq -r '.no_profile')"
  [[ "$np" -eq 2 ]]
}
