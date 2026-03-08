#!/usr/bin/env bats
# test_lib_common.bats — unit tests for pure functions in lib_subtitle_common.sh

setup() {
  load helpers/test_helper
  # The lib expects DB to be set; provide a dummy path so the variable is satisfied
  DB=/dev/null
  source "$SUBTITLES_DIR/lib_subtitle_common.sh"
}

# ---------------------------------------------------------------------------
# normalize_track_lang — 15 tests
# Uses printf (no trailing newline), so we capture into a variable.
# ---------------------------------------------------------------------------

@test "normalize_track_lang: eng -> en" {
  result="$(normalize_track_lang "eng")"
  [[ "$result" == "en" ]]
}

@test "normalize_track_lang: spa -> es" {
  result="$(normalize_track_lang "spa")"
  [[ "$result" == "es" ]]
}

@test "normalize_track_lang: fre -> fr" {
  result="$(normalize_track_lang "fre")"
  [[ "$result" == "fr" ]]
}

@test "normalize_track_lang: fra -> fr" {
  result="$(normalize_track_lang "fra")"
  [[ "$result" == "fr" ]]
}

@test "normalize_track_lang: ger -> de" {
  result="$(normalize_track_lang "ger")"
  [[ "$result" == "de" ]]
}

@test "normalize_track_lang: deu -> de" {
  result="$(normalize_track_lang "deu")"
  [[ "$result" == "de" ]]
}

@test "normalize_track_lang: chi -> zh" {
  result="$(normalize_track_lang "chi")"
  [[ "$result" == "zh" ]]
}

@test "normalize_track_lang: zho -> zh" {
  result="$(normalize_track_lang "zho")"
  [[ "$result" == "zh" ]]
}

@test "normalize_track_lang: jpn -> ja" {
  result="$(normalize_track_lang "jpn")"
  [[ "$result" == "ja" ]]
}

@test "normalize_track_lang: kor -> ko" {
  result="$(normalize_track_lang "kor")"
  [[ "$result" == "ko" ]]
}

@test "normalize_track_lang: ara -> ar" {
  result="$(normalize_track_lang "ara")"
  [[ "$result" == "ar" ]]
}

@test "normalize_track_lang: 2-letter en passthrough" {
  result="$(normalize_track_lang "en")"
  [[ "$result" == "en" ]]
}

@test "normalize_track_lang: 2-letter es passthrough" {
  result="$(normalize_track_lang "es")"
  [[ "$result" == "es" ]]
}

@test "normalize_track_lang: und stays und" {
  result="$(normalize_track_lang "und")"
  [[ "$result" == "und" ]]
}

@test "normalize_track_lang: empty string returns empty" {
  result="$(normalize_track_lang "")"
  [[ "$result" == "" ]]
}

@test "normalize_track_lang: case insensitive ENG -> en" {
  result="$(normalize_track_lang "ENG")"
  [[ "$result" == "en" ]]
}

# ---------------------------------------------------------------------------
# lang_to_iso639_2 — 8 tests
# Uses echo, so we use run + assert_output.
# ---------------------------------------------------------------------------

@test "lang_to_iso639_2: en -> eng" {
  run lang_to_iso639_2 "en"
  assert_success
  assert_output "eng"
}

@test "lang_to_iso639_2: es -> spa" {
  run lang_to_iso639_2 "es"
  assert_success
  assert_output "spa"
}

@test "lang_to_iso639_2: fr -> fra" {
  run lang_to_iso639_2 "fr"
  assert_success
  assert_output "fra"
}

@test "lang_to_iso639_2: de -> deu" {
  run lang_to_iso639_2 "de"
  assert_success
  assert_output "deu"
}

@test "lang_to_iso639_2: ja -> jpn" {
  run lang_to_iso639_2 "ja"
  assert_success
  assert_output "jpn"
}

@test "lang_to_iso639_2: unknown code passes through" {
  run lang_to_iso639_2 "xyz"
  assert_success
  assert_output "xyz"
}

@test "lang_to_iso639_2: 3-letter code passes through (not in case)" {
  run lang_to_iso639_2 "eng"
  assert_success
  assert_output "eng"
}

@test "lang_to_iso639_2: case insensitive EN -> eng" {
  run lang_to_iso639_2 "EN"
  assert_success
  assert_output "eng"
}

# ---------------------------------------------------------------------------
# expand_lang_codes — 8 tests
# Uses echo, output has trailing space. Use assert_output --partial.
# ---------------------------------------------------------------------------

@test "expand_lang_codes: eng,spa contains eng en spa es" {
  run expand_lang_codes "eng,spa"
  assert_success
  assert_output --partial "eng"
  assert_output --partial "en"
  assert_output --partial "spa"
  assert_output --partial "es"
}

@test "expand_lang_codes: en expands to en and eng" {
  run expand_lang_codes "en"
  assert_success
  assert_output --partial "en"
  assert_output --partial "eng"
}

@test "expand_lang_codes: fre expands to fre fr fra" {
  run expand_lang_codes "fre"
  assert_success
  assert_output --partial "fre"
  assert_output --partial "fr"
  assert_output --partial "fra"
}

@test "expand_lang_codes: fra expands to fra fr fre" {
  run expand_lang_codes "fra"
  assert_success
  assert_output --partial "fra"
  assert_output --partial "fr"
  assert_output --partial "fre"
}

@test "expand_lang_codes: zh expands to zh zho chi" {
  run expand_lang_codes "zh"
  assert_success
  assert_output --partial "zh"
  assert_output --partial "zho"
  assert_output --partial "chi"
}

@test "expand_lang_codes: empty input returns empty output" {
  run expand_lang_codes ""
  assert_success
  assert_output ""
}

@test "expand_lang_codes: eng,en deduplicates — eng appears once" {
  run expand_lang_codes "eng,en"
  assert_success
  # Strip trailing space, split on whitespace, count occurrences of "eng"
  count=$(echo "$output" | tr ' ' '\n' | grep -cx "eng" || true)
  [[ "$count" -eq 1 ]]
}

@test "expand_lang_codes: mixed eng,fr contains both families" {
  run expand_lang_codes "eng,fr"
  assert_success
  assert_output --partial "eng"
  assert_output --partial "en"
  assert_output --partial "fr"
  assert_output --partial "fre"
}

# ---------------------------------------------------------------------------
# lang_in_set — 5 tests
# Returns exit code; use run + assert_success / assert_failure.
# ---------------------------------------------------------------------------

@test "lang_in_set: lang present in set returns success" {
  run lang_in_set "en" "en es fr"
  assert_success
}

@test "lang_in_set: lang absent from set returns failure" {
  run lang_in_set "de" "en es fr"
  assert_failure
}

@test "lang_in_set: empty set returns failure" {
  run lang_in_set "en" ""
  assert_failure
}

@test "lang_in_set: empty lang returns failure" {
  run lang_in_set "" "en es"
  assert_failure
}

@test "lang_in_set: 3-letter code found in set" {
  run lang_in_set "eng" "en eng es spa"
  assert_success
}

# ---------------------------------------------------------------------------
# is_text_sub_codec — 6 tests
# ---------------------------------------------------------------------------

@test "is_text_sub_codec: subrip is a text codec" {
  run is_text_sub_codec "subrip"
  assert_success
}

@test "is_text_sub_codec: ass is a text codec" {
  run is_text_sub_codec "ass"
  assert_success
}

@test "is_text_sub_codec: mov_text is a text codec" {
  run is_text_sub_codec "mov_text"
  assert_success
}

@test "is_text_sub_codec: webvtt is a text codec" {
  run is_text_sub_codec "webvtt"
  assert_success
}

@test "is_text_sub_codec: hdmv_pgs_subtitle is not a text codec" {
  run is_text_sub_codec "hdmv_pgs_subtitle"
  assert_failure
}

@test "is_text_sub_codec: dvd_subtitle is not a text codec" {
  run is_text_sub_codec "dvd_subtitle"
  assert_failure
}

# ---------------------------------------------------------------------------
# is_tv_path / is_movie_path — 6 tests
# ---------------------------------------------------------------------------

@test "is_tv_path: /tv/ path returns true" {
  run is_tv_path "/media/tv/Show/Season 1/ep.mkv"
  assert_success
}

@test "is_tv_path: /tvanimated/ path returns true" {
  run is_tv_path "/media/tvanimated/Show/ep.mkv"
  assert_success
}

@test "is_tv_path: /movies/ path returns false" {
  run is_tv_path "/media/movies/Film/film.mkv"
  assert_failure
}

@test "is_movie_path: /movies/ path returns true" {
  run is_movie_path "/media/movies/Film/film.mkv"
  assert_success
}

@test "is_movie_path: /moviesanimated/ path returns true" {
  run is_movie_path "/media/moviesanimated/Film/film.mkv"
  assert_success
}

@test "is_movie_path: /tv/ path returns false" {
  run is_movie_path "/media/tv/Show/ep.mkv"
  assert_failure
}

# ---------------------------------------------------------------------------
# sql_escape — 4 tests
# Uses printf (no trailing newline), so capture into a variable.
# ---------------------------------------------------------------------------

@test "sql_escape: single quote is doubled" {
  result="$(sql_escape "it's")"
  [[ "$result" == "it''s" ]]
}

@test "sql_escape: empty string returns empty" {
  result="$(sql_escape "")"
  [[ "$result" == "" ]]
}

@test "sql_escape: string without quotes passes through unchanged" {
  result="$(sql_escape "no quotes")"
  [[ "$result" == "no quotes" ]]
}

@test "sql_escape: multiple single quotes are all doubled" {
  result="$(sql_escape "it's a 'test'")"
  [[ "$result" == "it''s a ''test''" ]]
}

# ---------------------------------------------------------------------------
# escape_regex — 3 tests
# Uses printf + sed, so use run + assert_output.
# ---------------------------------------------------------------------------

@test "escape_regex: dot is escaped" {
  run escape_regex "hello.world"
  assert_success
  assert_output "hello\.world"
}

@test "escape_regex: square brackets are escaped" {
  run escape_regex "[test]"
  assert_success
  assert_output "\[test\]"
}

@test "escape_regex: plain string passes through unchanged" {
  run escape_regex "plain"
  assert_success
  assert_output "plain"
}
