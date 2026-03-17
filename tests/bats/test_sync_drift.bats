#!/usr/bin/env bats
# test_sync_drift.bats — unit tests for find_reference_track() and check_sync_drift()
#
# Strategy:
#   - Awk timing logic is tested directly by creating SRT pairs in TEST_TMP
#     and invoking the same awk script used in check_sync_drift().
#   - find_reference_track() cascade logic is tested by overriding
#     get_embedded_subs() and get_audio_languages() with bash functions
#     that return controlled JSON / strings.  The overrides are installed
#     in setup() AFTER the libs are sourced so they always win.

setup() {
  load helpers/test_helper
  DB=/dev/null
  source "$SUBTITLES_DIR/lib_subtitle_common.sh"
  # Source drift functions; fall back to main script once merged
  if [[ -f "$SUBTITLES_DIR/_sync_drift_functions.sh" ]]; then
    source "$SUBTITLES_DIR/_sync_drift_functions.sh"
  else
    source "$SUBTITLES_DIR/subtitle_quality_manager.sh"
  fi
  TEST_TMP="$(mktemp -d)"

  # Install mock overrides AFTER sourcing so they win over lib definitions.
  # Tests set _MOCK_EMBEDDED_SUBS and _MOCK_AUDIO_LANGS before calling the
  # functions under test.
  get_embedded_subs()  { echo "$_MOCK_EMBEDDED_SUBS"; }
  get_audio_languages() { echo "$_MOCK_AUDIO_LANGS"; }
}

teardown() {
  rm -rf "$TEST_TMP"
}

# ---------------------------------------------------------------------------
# Helper: write an SRT file with N cues starting at base_offset seconds,
# each cue being 3 seconds long with 1 second gap (4s per cue).
#   write_srt_uniform path n_cues base_offset_sec
# ---------------------------------------------------------------------------
write_srt_uniform() {
  local path="$1" n="$2" base="$3"
  # One awk pass formats all N cues — avoids two subshell calls per cue.
  awk -v n="$n" -v base="$base" '
    function fmt(s,   h, m) {
      h = int(s/3600); m = int((s%3600)/60); s = s%60
      return sprintf("%02d:%02d:%02d,000", h, m, s)
    }
    BEGIN {
      for (i = 0; i < n; i++) {
        printf "%d\n%s --> %s\nLine %d\n\n", i+1, fmt(base+i*4), fmt(base+i*4+3), i+1
      }
    }
  ' /dev/null > "$path"
}

# ---------------------------------------------------------------------------
# Helper: run the awk drift-measurement script on two SRT files.
# Compatible with mawk (no three-argument match).
# Returns integer max_drift on stdout.
# ---------------------------------------------------------------------------
compute_drift_awk() {
  local ref_srt="$1" tgt_srt="$2"
  awk '
    function parse_ts(s,   parts, hms, ms_parts) {
      # SRT timestamp: HH:MM:SS,mmm  — split on comma first
      n = split(s, parts, ",")
      ms = (n > 1 ? parts[2]+0 : 0)
      split(parts[1], hms, ":")
      return hms[1]*3600 + hms[2]*60 + hms[3] + ms/1000
    }

    BEGIN {
      ref_count  = 0
      tgt_count  = 0
      phase      = 1
      prev_file  = ""
    }

    FILENAME != prev_file {
      if (NR > 1) phase = 2
      prev_file = FILENAME
    }

    phase == 1 && /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9] -->/ {
      # Extract start timestamp: everything before " --> "
      ts = substr($0, 1, index($0, " -->") - 1)
      ref_times[ref_count++] = parse_ts(ts)
    }

    phase == 2 && /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9] -->/ {
      ts = substr($0, 1, index($0, " -->") - 1)
      tgt_times[tgt_count++] = parse_ts(ts)
    }

    END {
      if (ref_count < 5 || tgt_count < 5) { print 0; exit }

      shorter = (tgt_count < ref_count) ? tgt_count : ref_count
      samples = (shorter < 20) ? shorter : 20
      max_d   = 0

      for (s = 0; s < samples; s++) {
        i = int(s * (tgt_count - 1) / (samples - 1))
        j = int(i * ref_count / tgt_count)
        if (j >= ref_count) j = ref_count - 1

        d = tgt_times[i] - ref_times[j]
        if (d < 0) d = -d
        if (d > max_d) max_d = d
      }

      printf "%.0f", max_d
    }
  ' "$ref_srt" "$tgt_srt"
}

# ---------------------------------------------------------------------------
# Helper: build a single subtitle stream JSON object.
#   build_sub_track stream_index lang codec
# ---------------------------------------------------------------------------
build_sub_track() {
  printf '{"index":%s,"codec_name":"%s","tags":{"language":"%s"},"forced":0}' \
    "$1" "$3" "$2"
}

# ---------------------------------------------------------------------------
# Awk timing tests — 7 tests
# ---------------------------------------------------------------------------

@test "awk drift: identical timings yields 0" {
  write_srt_uniform "$TEST_TMP/ref.srt" 20 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 20 60
  result="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$result" -eq 0 ]]
}

@test "awk drift: 15s uniform offset yields ~15, rating GOOD" {
  write_srt_uniform "$TEST_TMP/ref.srt" 20 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 20 75  # offset +15s
  drift="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$drift" -ge 14 && "$drift" -le 16 ]]
}

@test "awk drift: 45s uniform offset is in WARN band (30-60)" {
  write_srt_uniform "$TEST_TMP/ref.srt" 20 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 20 105  # offset +45s
  drift="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$drift" -ge 30 && "$drift" -lt 60 ]]
}

@test "awk drift: 150s uniform offset is in BAD band (>=60)" {
  write_srt_uniform "$TEST_TMP/ref.srt" 20 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 20 210  # offset +150s
  drift="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$drift" -ge 60 ]]
}

@test "awk drift: progressive drift 0->100s over 20 cues is detected as BAD" {
  # Reference: cue i starts at 60 + i*30 seconds
  # Target: cue i starts at 60 + i*30 + i*5 seconds (drift grows 0->95s)
  # Two awk passes (one per file) — each formats all 20 cues without shell loops.
  awk '
    function fmt(s,   h, m) { h=int(s/3600); m=int((s%3600)/60); s=s%60
      return sprintf("%02d:%02d:%02d,000", h, m, s) }
    BEGIN { for (i=0; i<20; i++) {
      s=60+i*30; printf "%d\n%s --> %s\nRef %d\n\n", i+1, fmt(s), fmt(s+3), i+1 } }
  ' /dev/null > "$TEST_TMP/ref.srt"
  awk '
    function fmt(s,   h, m) { h=int(s/3600); m=int((s%3600)/60); s=s%60
      return sprintf("%02d:%02d:%02d,000", h, m, s) }
    BEGIN { for (i=0; i<20; i++) {
      s=60+i*30+i*5; printf "%d\n%s --> %s\nTgt %d\n\n", i+1, fmt(s), fmt(s+3), i+1 } }
  ' /dev/null > "$TEST_TMP/tgt.srt"
  drift="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$drift" -ge 60 ]]
}

@test "awk drift: different cue counts — proportional mapping works, no crash" {
  # Ref has 20 cues, target has 10 cues with same base; both are well-formed.
  # Proportional mapping stretches target across full ref span, so drift will
  # reflect the span difference — the key guarantee is no crash and a number.
  write_srt_uniform "$TEST_TMP/ref.srt" 20 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 10 60
  drift="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  # Output must be a non-negative integer (function did not crash)
  [[ "$drift" =~ ^[0-9]+$ ]]
}

@test "awk drift: files with fewer than 5 cues each yield 0" {
  write_srt_uniform "$TEST_TMP/ref.srt" 3 60
  write_srt_uniform "$TEST_TMP/tgt.srt" 3 60
  result="$(compute_drift_awk "$TEST_TMP/ref.srt" "$TEST_TMP/tgt.srt")"
  [[ "$result" -eq 0 ]]
}

# ---------------------------------------------------------------------------
# find_reference_track tests — 7 tests
#
# Mock functions are installed in setup() above; each test sets
# _MOCK_EMBEDDED_SUBS and _MOCK_AUDIO_LANGS before calling the function.
# ---------------------------------------------------------------------------

@test "find_reference_track: profile [en,es] target=es picks en (has en text track)" {
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s]' \
    "$(build_sub_track 2 en subrip)" \
    "$(build_sub_track 3 es subrip)")"
  _MOCK_AUDIO_LANGS="en"
  result="$(find_reference_track "/fake.mkv" "es" "en,es")"
  [[ "$result" == "2 en" ]]
}

@test "find_reference_track: profile [ja,en] target=en picks ja (has ja text track)" {
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s]' \
    "$(build_sub_track 2 ja subrip)" \
    "$(build_sub_track 3 en subrip)")"
  _MOCK_AUDIO_LANGS="ja"
  result="$(find_reference_track "/fake.mkv" "en" "ja,en")"
  [[ "$result" == "2 ja" ]]
}

@test "find_reference_track: no profile provided, falls back to audio lang" {
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s]' \
    "$(build_sub_track 2 fr subrip)" \
    "$(build_sub_track 3 es subrip)")"
  _MOCK_AUDIO_LANGS="fr"
  result="$(find_reference_track "/fake.mkv" "es" "")"
  [[ "$result" == "2 fr" ]]
}

@test "find_reference_track: no matching profile or audio lang returns SKIP" {
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s]' \
    "$(build_sub_track 2 de subrip)" \
    "$(build_sub_track 3 es subrip)")"
  _MOCK_AUDIO_LANGS="es"
  result="$(find_reference_track "/fake.mkv" "es" "es")"
  [[ "$result" == "SKIP none" ]]
}

@test "find_reference_track: bitmap profile candidate skipped, fr via profile fallback used" {
  # en track is bitmap (hdmv_pgs_subtitle) — not usable; fr is text
  # Profile: en,fr,es; target=es. en is bitmap, so skip; fr is next profile entry with a text track.
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s,%s]' \
    "$(build_sub_track 2 en hdmv_pgs_subtitle)" \
    "$(build_sub_track 3 fr subrip)" \
    "$(build_sub_track 4 es subrip)")"
  _MOCK_AUDIO_LANGS="fr"
  result="$(find_reference_track "/fake.mkv" "es" "en,fr,es")"
  [[ "$result" == "3 fr" ]]
}

@test "find_reference_track: only one track (the target itself) returns SKIP" {
  _MOCK_EMBEDDED_SUBS="$(printf '[%s]' \
    "$(build_sub_track 2 es subrip)")"
  _MOCK_AUDIO_LANGS="es"
  result="$(find_reference_track "/fake.mkv" "es" "es")"
  [[ "$result" == "SKIP none" ]]
}

@test "find_reference_track: complete language mismatch returns SKIP" {
  # de and fr tracks exist; profile and audio only list es; target=es
  _MOCK_EMBEDDED_SUBS="$(printf '[%s,%s,%s]' \
    "$(build_sub_track 2 de subrip)" \
    "$(build_sub_track 3 fr subrip)" \
    "$(build_sub_track 4 es subrip)")"
  _MOCK_AUDIO_LANGS="es"
  result="$(find_reference_track "/fake.mkv" "es" "es")"
  [[ "$result" == "SKIP none" ]]
}
