#!/usr/bin/env bats
# test_codec_helpers.bats — unit tests for pure helper functions in library_codec_manager.sh

setup() {
  load helpers/test_helper
  # Satisfy variables the script reads at parse-time and the trap references
  export TMP_DIR
  TMP_DIR="$(mktemp -d)"
  export STATE_DIR="$TMP_DIR"
  export DB_PATH="$TMP_DIR/test.db"
  export LOG_PATH="$TMP_DIR/test.log"
  export BACKUP_DIR="$TMP_DIR/backups"
  export BAZARR_DB="$TMP_DIR/fake_bazarr.db"
  export LOG_LEVEL="info"
  source "$TRANSCODE_DIR/library_codec_manager.sh"
  # library_codec_manager.sh sets -euo pipefail; disable errexit so that
  # a failing assert_output does not kill the bats subprocess.
  set +e
}

teardown() {
  rm -rf "$TMP_DIR"
}

# ---------------------------------------------------------------------------
# comma_fmt — 5 tests
# Uses echo | sed, so run + assert_output is the right pattern.
# ---------------------------------------------------------------------------

@test "comma_fmt: 1000 -> 1,000" {
  run comma_fmt 1000
  assert_output "1,000"
}

@test "comma_fmt: 999 stays 999 (no comma)" {
  run comma_fmt 999
  assert_output "999"
}

@test "comma_fmt: 12345678 -> 12345,678 (sed adds one rightmost comma group)" {
  run comma_fmt 12345678
  assert_output "12345,678"
}

@test "comma_fmt: 0 stays 0" {
  run comma_fmt 0
  assert_output "0"
}

@test "comma_fmt: 100 stays 100" {
  run comma_fmt 100
  assert_output "100"
}

# ---------------------------------------------------------------------------
# progress_bar — 5 tests
# Uses printf with \uNNNN sequences — output is actual Unicode.
# Capture via $(...) and compare against printf-built expected strings.
# ---------------------------------------------------------------------------

@test "progress_bar: 0/100 is all light blocks (width 16)" {
  result="$(progress_bar 0 100)"
  expected="$(printf '%0.s░' {1..16})"
  [[ "$result" == "$expected" ]]
}

@test "progress_bar: 100/100 is all filled blocks (width 16)" {
  result="$(progress_bar 100 100)"
  expected="$(printf '%0.s█' {1..16})"
  [[ "$result" == "$expected" ]]
}

@test "progress_bar: 50/100 is 8 filled + 8 light" {
  result="$(progress_bar 50 100)"
  filled="$(printf '%0.s█' {1..8})"
  light="$(printf '%0.s░' {1..8})"
  [[ "$result" == "${filled}${light}" ]]
}

@test "progress_bar: 0/0 (total<=0 guard) is all light blocks" {
  result="$(progress_bar 0 0)"
  expected="$(printf '%0.s░' {1..16})"
  [[ "$result" == "$expected" ]]
}

@test "progress_bar: 100/100 with custom width 8 is 8 filled blocks" {
  result="$(progress_bar 100 100 8)"
  expected="$(printf '%0.s█' {1..8})"
  [[ "$result" == "$expected" ]]
}

# ---------------------------------------------------------------------------
# fps_to_float — 5 tests
# Uses printf/awk — capture via $(...).
# ---------------------------------------------------------------------------

@test "fps_to_float: 24000/1001 starts with 23.97" {
  result="$(fps_to_float "24000/1001")"
  [[ "$result" == 23.97* ]]
}

@test "fps_to_float: 30/1 -> 30.000000" {
  result="$(fps_to_float "30/1")"
  [[ "$result" == "30.000000" ]]
}

@test "fps_to_float: empty string -> 0" {
  result="$(fps_to_float "")"
  [[ "$result" == "0" ]]
}

@test "fps_to_float: 0/0 -> 0 (division-by-zero guard)" {
  result="$(fps_to_float "0/0")"
  [[ "$result" == "0" ]]
}

@test "fps_to_float: 25/1 -> 25.000000" {
  result="$(fps_to_float "25/1")"
  [[ "$result" == "25.000000" ]]
}

# ---------------------------------------------------------------------------
# iso_to_lang_name — 5 tests
# Uses echo — run + assert_output is the right pattern.
# ---------------------------------------------------------------------------

@test "iso_to_lang_name: eng -> English" {
  run iso_to_lang_name "eng"
  assert_output "English"
}

@test "iso_to_lang_name: spa -> Spanish" {
  run iso_to_lang_name "spa"
  assert_output "Spanish"
}

@test "iso_to_lang_name: jpn -> Japanese" {
  run iso_to_lang_name "jpn"
  assert_output "Japanese"
}

@test "iso_to_lang_name: unknown code -> empty string" {
  run iso_to_lang_name "unknown"
  assert_output ""
}

@test "iso_to_lang_name: uppercase ENG -> English (case insensitive)" {
  run iso_to_lang_name "ENG"
  assert_output "English"
}

# ---------------------------------------------------------------------------
# _resolve_match_dir — 5 tests
# Uses printf — capture via $(...).
# ---------------------------------------------------------------------------

@test "_resolve_match_dir: TV path with Season dir strips to series root" {
  result="$(_resolve_match_dir "/media/tv/ShowName/Season 1/ep.mkv")"
  [[ "$result" == "/media/tv/ShowName" ]]
}

@test "_resolve_match_dir: TV path without Season dir strips to parent" {
  result="$(_resolve_match_dir "/media/tv/ShowName/ep.mkv")"
  [[ "$result" == "/media/tv/ShowName" ]]
}

@test "_resolve_match_dir: movie path strips to parent dir" {
  result="$(_resolve_match_dir "/media/movies/Film (2020)/film.mkv")"
  [[ "$result" == "/media/movies/Film (2020)" ]]
}

@test "_resolve_match_dir: tvanimated path with Season dir strips to series root" {
  result="$(_resolve_match_dir "/media/tvanimated/Anime/Season 2/ep.mkv")"
  [[ "$result" == "/media/tvanimated/Anime" ]]
}

@test "_resolve_match_dir: moviesanimated path strips to parent dir" {
  result="$(_resolve_match_dir "/media/moviesanimated/Film/film.mkv")"
  [[ "$result" == "/media/moviesanimated/Film" ]]
}
