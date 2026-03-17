#!/usr/bin/env bats
# test_scoring.bats — unit tests for scoring functions in subtitle_quality_manager.sh

setup() {
  load helpers/test_helper
  # Source lib first (it's needed by the manager)
  source "$SUBTITLES_DIR/lib_subtitle_common.sh"
  # Source manager — guard skips arg parsing when sourced
  source "$SUBTITLES_DIR/subtitle_quality_manager.sh"
}

# ---------------------------------------------------------------------------
# score_subtitle — 10 tests
# Signature: score_subtitle cues first last duration mojibake watermarks
# ---------------------------------------------------------------------------

@test "score_subtitle: normal subtitle rates GOOD" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 0)"
  [[ "$result" == "GOOD" ]]
}

@test "score_subtitle: mojibake=1 always rates BAD" {
  result="$(score_subtitle 500 1.0 3300.0 3600 1 0)"
  [[ "$result" == "BAD" ]]
}

@test "score_subtitle: low cues per hour rates BAD" {
  # 50 cues / 3600s = 50 cues/hr < 200 threshold
  result="$(score_subtitle 50 1.0 3300.0 3600 0 0)"
  [[ "$result" == "BAD" ]]
}

@test "score_subtitle: low coverage rates BAD" {
  # last=1000 / duration=3600 = 28% < 50% threshold
  result="$(score_subtitle 500 1.0 1000.0 3600 0 0)"
  [[ "$result" == "BAD" ]]
}

@test "score_subtitle: watermarks=1 does not affect GOOD rating" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 1)"
  [[ "$result" == "GOOD" ]]
}

@test "score_subtitle: high cue density rates WARN" {
  # 2000 cues / 3600s = 2000 cues/hr > 1200 threshold
  result="$(score_subtitle 2000 1.0 3300.0 3600 0 0)"
  [[ "$result" == "WARN" ]]
}

@test "score_subtitle: medium coverage (50-70%) rates WARN" {
  # last=2300 / duration=3600 = 64% — between 50% and 70%
  result="$(score_subtitle 500 1.0 2300.0 3600 0 0)"
  [[ "$result" == "WARN" ]]
}

@test "score_subtitle: zero duration skips density and coverage checks, rates GOOD" {
  result="$(score_subtitle 500 1.0 3300.0 0 0 0)"
  [[ "$result" == "GOOD" ]]
}

@test "score_subtitle: zero cues with nonzero duration rates BAD" {
  # 0 cues / 3600s = 0 cues/hr < 200 threshold
  result="$(score_subtitle 0 0 0 3600 0 0)"
  [[ "$result" == "BAD" ]]
}

@test "score_subtitle: late first cue with coverage under 80% rates WARN" {
  # first=400 > 300s threshold; last=2700/3600=75% < 80%
  result="$(score_subtitle 500 400.0 2700.0 3600 0 0)"
  [[ "$result" == "WARN" ]]
}

# ---------------------------------------------------------------------------
# score_subtitle 7th param (sync_drift_rating) — 4 tests
# ---------------------------------------------------------------------------

@test "score_subtitle: 7th arg BAD overrides GOOD to BAD" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 0 BAD)"
  [[ "$result" == "BAD" ]]
}

@test "score_subtitle: 7th arg WARN downgrades GOOD to WARN" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 0 WARN)"
  [[ "$result" == "WARN" ]]
}

@test "score_subtitle: 7th arg SKIP has no effect on GOOD" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 0 SKIP)"
  [[ "$result" == "GOOD" ]]
}

@test "score_subtitle: 6-arg backward compat still works" {
  result="$(score_subtitle 500 1.0 3300.0 3600 0 0)"
  [[ "$result" == "GOOD" ]]
}

# ---------------------------------------------------------------------------
# analyze_srt_file — 8 tests
# Output format: cue_count first_s last_s mojibake watermarks
# ---------------------------------------------------------------------------

@test "analyze_srt_file: good.srt yields 15 cues, no mojibake, no watermarks" {
  output="$(analyze_srt_file "$FIXTURES_DIR/good.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$cues"      -eq 15 ]]
  [[ "$mojibake"  -eq 0  ]]
  [[ "$watermarks" -eq 0 ]]
}

@test "analyze_srt_file: sparse.srt yields 5 cues, no mojibake, no watermarks" {
  output="$(analyze_srt_file "$FIXTURES_DIR/sparse.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$cues"      -eq 5 ]]
  [[ "$mojibake"  -eq 0 ]]
  [[ "$watermarks" -eq 0 ]]
}

@test "analyze_srt_file: watermarked.srt detects watermarks" {
  output="$(analyze_srt_file "$FIXTURES_DIR/watermarked.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$watermarks" -eq 1 ]]
}

@test "analyze_srt_file: mojibake.srt detects mojibake" {
  output="$(analyze_srt_file "$FIXTURES_DIR/mojibake.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$mojibake" -eq 1 ]]
}

@test "analyze_srt_file: empty.srt yields 0 cues" {
  output="$(analyze_srt_file "$FIXTURES_DIR/empty.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$cues" -eq 0 ]]
}

@test "analyze_srt_file: forced.srt yields 3 cues" {
  output="$(analyze_srt_file "$FIXTURES_DIR/forced.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$cues" -eq 3 ]]
}

@test "analyze_srt_file: watermark_only.srt detects watermarks" {
  output="$(analyze_srt_file "$FIXTURES_DIR/watermark_only.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$watermarks" -eq 1 ]]
}

@test "analyze_srt_file: good.srt first timestamp is greater than zero" {
  output="$(analyze_srt_file "$FIXTURES_DIR/good.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  # first=60.0s (00:01:00,000)
  result="$(awk "BEGIN { print ($first > 0) }")"
  [[ "$result" -eq 1 ]]
}

@test "analyze_srt_file: fansub_watermarked.srt detects fan sub watermarks" {
  # Fan sub patterns: "The EVIL team", "Dr. Infinito", "GrupoTS"
  # These are detected via the static WATERMARK_PATTERNS fallback
  _CACHED_WATERMARK_PATTERNS="galaxytv|yify|yts|opensubtitles|addic7ed|subscene|podnapisi|sub[sz]cene|the evil team|dr\\.? ?infinito|grupots|grupo ?ts"
  output="$(analyze_srt_file "$FIXTURES_DIR/fansub_watermarked.srt")"
  read -r cues first last mojibake watermarks <<< "$output"
  [[ "$cues" -eq 10 ]]
  [[ "$watermarks" -eq 1 ]]
}

# ---------------------------------------------------------------------------
# subtitle_quality_score — 7 tests
# Signature: subtitle_quality_score sub_file media_seconds forced_num
# Output: integer (higher = better; can be negative due to penalty model)
# ---------------------------------------------------------------------------

@test "subtitle_quality_score: good.srt with 3600s media scores non-zero" {
  score="$(subtitle_quality_score "$FIXTURES_DIR/good.srt" 3600 0)"
  [[ "$score" -ne 0 ]]
}

@test "subtitle_quality_score: sparse.srt scores lower than good.srt" {
  good_score="$(subtitle_quality_score "$FIXTURES_DIR/good.srt" 3600 0)"
  sparse_score="$(subtitle_quality_score "$FIXTURES_DIR/sparse.srt" 3600 0)"
  [[ "$good_score" -gt "$sparse_score" ]]
}

@test "subtitle_quality_score: good.srt ranks higher than sparse.srt" {
  good_score="$(subtitle_quality_score "$FIXTURES_DIR/good.srt" 3600 0)"
  sparse_score="$(subtitle_quality_score "$FIXTURES_DIR/sparse.srt" 3600 0)"
  [[ "$good_score" -gt "$sparse_score" ]]
}

@test "subtitle_quality_score: forced.srt with forced=1 scores non-zero" {
  score="$(subtitle_quality_score "$FIXTURES_DIR/forced.srt" 3600 1)"
  [[ "$score" -ne 0 ]]
}

@test "subtitle_quality_score: empty.srt scores exactly 0" {
  score="$(subtitle_quality_score "$FIXTURES_DIR/empty.srt" 3600 0)"
  [[ "$score" -eq 0 ]]
}

@test "subtitle_quality_score: good.srt with 0s media scores non-zero (cues still count)" {
  score="$(subtitle_quality_score "$FIXTURES_DIR/good.srt" 0 0)"
  [[ "$score" -ne 0 ]]
}

@test "subtitle_quality_score: forced.srt scores differently with forced=1 vs forced=0" {
  score_forced="$(subtitle_quality_score "$FIXTURES_DIR/forced.srt" 3600 1)"
  score_normal="$(subtitle_quality_score "$FIXTURES_DIR/forced.srt" 3600 0)"
  [[ "$score_forced" -ne "$score_normal" ]]
}
