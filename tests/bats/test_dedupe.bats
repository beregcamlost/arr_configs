#!/usr/bin/env bats
# test_dedupe.bats — unit tests for pure functions in library_subtitle_dedupe.sh
#
# subtitle_group_key()      — 8 tests
# canonical_subtitle_path() — 4 tests
# subtitle_signature()      — 3 tests

# ---------------------------------------------------------------------------
# Sourcing strategy
#
# subtitle_group_key, canonical_subtitle_path, subtitle_signature, and
# normalize_lang_code are all defined INSIDE the BASH_SOURCE guard in
# library_subtitle_dedupe.sh and are therefore NOT available when the file
# is sourced normally (sourced files have BASH_SOURCE[0] != $0).
#
# We extract only the function bodies via sed and eval them so the functions
# become available in the test shell without executing the rest of the script
# (which would try to open the lock file, create directories, etc.).
#
# LANG_NORM is also declared inside the guard, so we declare it here first
# and pre-populate it with the common mappings that load_language_map() would
# read from the Bazarr DB at runtime.
# ---------------------------------------------------------------------------

setup() {
  load helpers/test_helper

  # Declare LANG_NORM before eval-ing normalize_lang_code, which references it.
  declare -gA LANG_NORM=()
  LANG_NORM[en]=en   LANG_NORM[eng]=en
  LANG_NORM[es]=es   LANG_NORM[spa]=es
  LANG_NORM[fr]=fr   LANG_NORM[fre]=fr  LANG_NORM[fra]=fr
  LANG_NORM[de]=de   LANG_NORM[deu]=de  LANG_NORM[ger]=de
  LANG_NORM[it]=it   LANG_NORM[ita]=it
  LANG_NORM[ja]=ja   LANG_NORM[jpn]=ja
  LANG_NORM[hi]=hi   LANG_NORM[hin]=hi
  LANG_NORM[ko]=ko   LANG_NORM[kor]=ko
  LANG_NORM[pt]=pt   LANG_NORM[por]=pt
  LANG_NORM[zh]=zh
  LANG_NORM[und]=und

  # Extract the four pure functions from the dedupe script and eval them.
  # sed ranges: /^FUNCNAME()/,/^}/ captures each top-level function body.
  eval "$(sed -n \
    '/^normalize_lang_code()/,/^}/p
     /^subtitle_group_key()/,/^}/p
     /^canonical_subtitle_path()/,/^}/p
     /^subtitle_signature()/,/^}/p' \
    "$SUBTITLES_DIR/library_subtitle_dedupe.sh")"
}

teardown() {
  # Remove any temp dirs created by signature tests
  if [[ -n "${TEST_TMP:-}" && -d "$TEST_TMP" ]]; then
    rm -rf "$TEST_TMP"
  fi
}

# ---------------------------------------------------------------------------
# normalize_lang_code — tested implicitly through subtitle_group_key, but
# also verified directly here for 3-letter codes and Chinese special cases.
# ---------------------------------------------------------------------------

@test "normalize_lang_code: spa -> es" {
  result="$(normalize_lang_code "spa")"
  [[ "$result" == "es" ]]
}

@test "normalize_lang_code: jpn -> ja" {
  result="$(normalize_lang_code "jpn")"
  [[ "$result" == "ja" ]]
}

@test "normalize_lang_code: zho -> zh (hardcoded case)" {
  result="$(normalize_lang_code "zho")"
  [[ "$result" == "zh" ]]
}

@test "normalize_lang_code: chi -> zh (hardcoded case)" {
  result="$(normalize_lang_code "chi")"
  [[ "$result" == "zh" ]]
}

@test "normalize_lang_code: zht -> zt (traditional Chinese)" {
  result="$(normalize_lang_code "zht")"
  [[ "$result" == "zt" ]]
}

@test "normalize_lang_code: unknown code passes through unchanged" {
  result="$(normalize_lang_code "xyz")"
  [[ "$result" == "xyz" ]]
}

# ---------------------------------------------------------------------------
# subtitle_group_key — 8 tests
# Uses printf (no trailing newline), so we capture into a variable.
# ---------------------------------------------------------------------------

@test "subtitle_group_key: plain English -> en|0" {
  result="$(subtitle_group_key "Movie.en.srt")"
  [[ "$result" == "en|0" ]]
}

@test "subtitle_group_key: forced English -> en|1" {
  result="$(subtitle_group_key "Movie.en.forced.srt")"
  [[ "$result" == "en|1" ]]
}

@test "subtitle_group_key: HI qualifier skipped, lang found before it -> en|0" {
  # Movie.en.hi.srt — last segment is 'hi' (a known qualifier), the segment
  # before it is 'en', so the group key should be the English group.
  result="$(subtitle_group_key "Movie.en.hi.srt")"
  [[ "$result" == "en|0" ]]
}

@test "subtitle_group_key: SDH qualifier skipped, lang found before it -> en|0" {
  result="$(subtitle_group_key "Movie.en.sdh.srt")"
  [[ "$result" == "en|0" ]]
}

@test "subtitle_group_key: hi.srt with no prior lang segment -> hi|0 (Hindi)" {
  # When 'hi' appears as the last segment and there is no preceding language
  # segment, treat it as the Hindi language code.
  result="$(subtitle_group_key "Movie.hi.srt")"
  [[ "$result" == "hi|0" ]]
}

@test "subtitle_group_key: 3-letter spa normalised -> es|0" {
  result="$(subtitle_group_key "Movie.spa.srt")"
  [[ "$result" == "es|0" ]]
}

@test "subtitle_group_key: no language segment -> und|0" {
  result="$(subtitle_group_key "Movie.srt")"
  [[ "$result" == "und|0" ]]
}

@test "subtitle_group_key: CC qualifier + forced -> en|1" {
  # Movie.en.cc.forced.srt
  # forced=1, strip .forced -> Movie.en.cc
  # last segment = 'cc' (qualifier), look back -> 'en'
  result="$(subtitle_group_key "Movie.en.cc.forced.srt")"
  [[ "$result" == "en|1" ]]
}

# ---------------------------------------------------------------------------
# canonical_subtitle_path — 4 tests
# Uses printf (no trailing newline), so capture into a variable.
# ---------------------------------------------------------------------------

@test "canonical_subtitle_path: en non-forced -> stem.en.srt" {
  result="$(canonical_subtitle_path "/path/Movie" "en|0")"
  [[ "$result" == "/path/Movie.en.srt" ]]
}

@test "canonical_subtitle_path: en forced -> stem.en.forced.srt" {
  result="$(canonical_subtitle_path "/path/Movie" "en|1")"
  [[ "$result" == "/path/Movie.en.forced.srt" ]]
}

@test "canonical_subtitle_path: und non-forced -> stem.srt (no lang segment)" {
  result="$(canonical_subtitle_path "/path/Movie" "und|0")"
  [[ "$result" == "/path/Movie.srt" ]]
}

@test "canonical_subtitle_path: und forced -> stem.forced.srt" {
  result="$(canonical_subtitle_path "/path/Movie" "und|1")"
  [[ "$result" == "/path/Movie.forced.srt" ]]
}

# ---------------------------------------------------------------------------
# subtitle_signature — 3 tests
# Creates real files in a temp dir; checks hash stability and divergence.
# ---------------------------------------------------------------------------

@test "subtitle_signature: no files -> 'none'" {
  TEST_TMP="$(mktemp -d)"
  result="$(subtitle_signature "$TEST_TMP/Movie")"
  [[ "$result" == "none" ]]
}

@test "subtitle_signature: same files produce same hash on repeated calls" {
  TEST_TMP="$(mktemp -d)"
  printf 'subtitle line 1\n' > "$TEST_TMP/Movie.en.srt"
  printf 'subtitle line 2\n' > "$TEST_TMP/Movie.es.srt"

  hash1="$(subtitle_signature "$TEST_TMP/Movie")"
  hash2="$(subtitle_signature "$TEST_TMP/Movie")"

  [[ "$hash1" != "none" ]]
  [[ "$hash1" == "$hash2" ]]
}

@test "subtitle_signature: different file sets produce different hashes" {
  TEST_TMP="$(mktemp -d)"
  printf 'English subtitle\n' > "$TEST_TMP/Movie.en.srt"
  hash_before="$(subtitle_signature "$TEST_TMP/Movie")"

  printf 'Spanish subtitle\n' > "$TEST_TMP/Movie.es.srt"
  hash_after="$(subtitle_signature "$TEST_TMP/Movie")"

  [[ "$hash_before" != "none" ]]
  [[ "$hash_after"  != "none" ]]
  [[ "$hash_before" != "$hash_after" ]]
}
