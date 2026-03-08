#!/usr/bin/env bats
# test_watermarks.bats — unit tests for strip_srt_watermarks() in lib_subtitle_common.sh

setup() {
  load helpers/test_helper
  DB=/dev/null
  source "$SUBTITLES_DIR/lib_subtitle_common.sh"
  TEST_TMP="$(mktemp -d)"
}

teardown() {
  rm -rf "$TEST_TMP"
}

# ---------------------------------------------------------------------------
# Helper: write content to a file without a trailing newline interpretation
# ---------------------------------------------------------------------------
write_srt() {
  printf '%s' "$2" > "$1"
}

# ---------------------------------------------------------------------------
# Note on calling convention:
#   Always invoke strip_srt_watermarks via `run` so bats disables errexit and
#   the function's explicit `return` codes (0, 1, 2) are captured cleanly in
#   $status. Direct calls inside test bodies inherit set -e from the lib,
#   which would cause awk's exit 10/20 to abort the subshell before the
#   shell-level remapping to return 1/2 executes.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1. Clean file — no watermarks, no font tags -> rc=1, file unchanged
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: clean file returns 1 (no changes needed)" {
  cp "$FIXTURES_DIR/good.srt" "$TEST_TMP/clean.srt"
  run strip_srt_watermarks "$TEST_TMP/clean.srt"
  assert_equal "$status" 1
}

@test "strip_srt_watermarks: clean file content is unchanged after call" {
  cp "$FIXTURES_DIR/good.srt" "$TEST_TMP/clean.srt"
  local before
  before="$(cat "$TEST_TMP/clean.srt")"
  run strip_srt_watermarks "$TEST_TMP/clean.srt"
  local after
  after="$(cat "$TEST_TMP/clean.srt")"
  [[ "$before" == "$after" ]]
}

# ---------------------------------------------------------------------------
# 2. Watermarked file — two watermark cues stripped, rc=0
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: watermarked file returns 0 (modified)" {
  cp "$FIXTURES_DIR/watermarked.srt" "$TEST_TMP/wm.srt"
  run strip_srt_watermarks "$TEST_TMP/wm.srt"
  assert_equal "$status" 0
}

@test "strip_srt_watermarks: YTS watermark cue removed from file" {
  cp "$FIXTURES_DIR/watermarked.srt" "$TEST_TMP/wm.srt"
  run strip_srt_watermarks "$TEST_TMP/wm.srt"
  run grep -i "YTS" "$TEST_TMP/wm.srt"
  assert_failure
}

@test "strip_srt_watermarks: opensubtitles watermark cue removed from file" {
  cp "$FIXTURES_DIR/watermarked.srt" "$TEST_TMP/wm.srt"
  run strip_srt_watermarks "$TEST_TMP/wm.srt"
  run grep -i "opensubtitles" "$TEST_TMP/wm.srt"
  assert_failure
}

# ---------------------------------------------------------------------------
# 3. All-watermark file — every cue matches is_wm_line() -> rc=2, file deleted
#    Build a synthetic SRT using only patterns that is_wm_line() matches:
#    yify, opensubtitles.org, "downloaded from ...", yts.*
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: all-watermark file returns 2 (file deleted)" {
  write_srt "$TEST_TMP/all_wm.srt" \
'1
00:00:01,000 --> 00:00:03,000
YIFY Subtitles

2
00:00:04,000 --> 00:00:06,000
opensubtitles.org

3
00:00:07,000 --> 00:00:09,000
Downloaded from yts.mx

'
  run strip_srt_watermarks "$TEST_TMP/all_wm.srt"
  assert_equal "$status" 2
}

@test "strip_srt_watermarks: all-watermark file is removed from disk" {
  write_srt "$TEST_TMP/all_wm2.srt" \
'1
00:00:01,000 --> 00:00:03,000
YIFY Subtitles

2
00:00:04,000 --> 00:00:06,000
Downloaded from yts.mx

'
  run strip_srt_watermarks "$TEST_TMP/all_wm2.srt"
  [[ ! -f "$TEST_TMP/all_wm2.srt" ]]
}

# ---------------------------------------------------------------------------
# 4. Font tags stripped from kept cues
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: font tags stripped from kept cue text, rc=0" {
  write_srt "$TEST_TMP/fonts.srt" \
'1
00:01:00,000 --> 00:01:04,000
<font color="#ffffff">Hello world</font>

'
  run strip_srt_watermarks "$TEST_TMP/fonts.srt"
  assert_equal "$status" 0
  run grep -i "<font" "$TEST_TMP/fonts.srt"
  assert_failure
}

@test "strip_srt_watermarks: cue text content preserved after font tag removal" {
  write_srt "$TEST_TMP/fonts2.srt" \
'1
00:01:00,000 --> 00:01:04,000
<font color="#ffffff">Hello world</font>

'
  run strip_srt_watermarks "$TEST_TMP/fonts2.srt"
  run grep "Hello world" "$TEST_TMP/fonts2.srt"
  assert_success
}

# ---------------------------------------------------------------------------
# 5. Cue renumbering after watermark removal
#    watermarked.srt has 12 cues; cues 3 and 10 are watermarks -> 10 kept
#    Sequential check: extracted cue-index lines must be 1 2 3 ... 10
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: cues renumbered sequentially after removal" {
  cp "$FIXTURES_DIR/watermarked.srt" "$TEST_TMP/renum.srt"
  run strip_srt_watermarks "$TEST_TMP/renum.srt"

  # Collect all bare-integer lines (cue index lines) from the stripped file
  local -a indices=()
  while IFS= read -r line; do
    indices+=("$line")
  done < <(grep -E '^[0-9]+[[:space:]]*$' "$TEST_TMP/renum.srt")

  # Expect exactly 10 cues remaining
  assert_equal "${#indices[@]}" 10

  # Each index must equal its 1-based position
  local i
  for i in "${!indices[@]}"; do
    local expected=$(( i + 1 ))
    local actual
    actual="$(printf '%s' "${indices[$i]}" | tr -d '[:space:]')"
    assert_equal "$actual" "$expected"
  done
}

# ---------------------------------------------------------------------------
# 6. Empty file — awk sees no cues, had_changes=0 -> exit 10 -> rc=1
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: empty file returns 1 (no changes)" {
  cp "$FIXTURES_DIR/empty.srt" "$TEST_TMP/empty.srt"
  run strip_srt_watermarks "$TEST_TMP/empty.srt"
  assert_equal "$status" 1
}

# ---------------------------------------------------------------------------
# 7. YIFY pattern — cue containing "YIFY Subtitles" removed; normal cue kept
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: YIFY cue removed and normal cue preserved" {
  write_srt "$TEST_TMP/yify.srt" \
'1
00:01:00,000 --> 00:01:03,000
This is a normal subtitle line.

2
00:01:05,000 --> 00:01:07,000
YIFY Subtitles

'
  run strip_srt_watermarks "$TEST_TMP/yify.srt"
  assert_equal "$status" 0
  run grep -i "YIFY" "$TEST_TMP/yify.srt"
  assert_failure
  run grep "normal subtitle line" "$TEST_TMP/yify.srt"
  assert_success
}

# ---------------------------------------------------------------------------
# 8. "Downloaded from" pattern removed
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: downloaded-from cue removed, normal cue preserved" {
  write_srt "$TEST_TMP/dl.srt" \
'1
00:00:30,000 --> 00:00:33,000
Downloaded from some piracy site

2
00:01:00,000 --> 00:01:03,000
A perfectly normal line.

'
  run strip_srt_watermarks "$TEST_TMP/dl.srt"
  run grep -i "downloaded from" "$TEST_TMP/dl.srt"
  assert_failure
  run grep "perfectly normal" "$TEST_TMP/dl.srt"
  assert_success
}

# ---------------------------------------------------------------------------
# 9. Case-insensitive watermark matching — OPENSUBTITLES.ORG (all uppercase)
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: uppercase OPENSUBTITLES.ORG cue is matched and removed" {
  write_srt "$TEST_TMP/caps.srt" \
'1
00:00:10,000 --> 00:00:12,000
OPENSUBTITLES.ORG

2
00:01:00,000 --> 00:01:03,000
Some real dialogue here.

'
  run strip_srt_watermarks "$TEST_TMP/caps.srt"
  assert_equal "$status" 0
  run grep -i "opensubtitles" "$TEST_TMP/caps.srt"
  assert_failure
}

# ---------------------------------------------------------------------------
# 10. File permissions preserved after modification (rc=0 path)
# ---------------------------------------------------------------------------

@test "strip_srt_watermarks: file permissions preserved on modified file (rc=0)" {
  cp "$FIXTURES_DIR/watermarked.srt" "$TEST_TMP/perms.srt"
  chmod 755 "$TEST_TMP/perms.srt"
  run strip_srt_watermarks "$TEST_TMP/perms.srt"
  assert_equal "$status" 0
  local mode
  mode="$(stat -c '%a' "$TEST_TMP/perms.srt")"
  assert_equal "$mode" "755"
}

@test "strip_srt_watermarks: file permissions unchanged when no modification needed (rc=1)" {
  cp "$FIXTURES_DIR/good.srt" "$TEST_TMP/perms_clean.srt"
  chmod 755 "$TEST_TMP/perms_clean.srt"
  run strip_srt_watermarks "$TEST_TMP/perms_clean.srt"
  assert_equal "$status" 1
  local mode
  mode="$(stat -c '%a' "$TEST_TMP/perms_clean.srt")"
  assert_equal "$mode" "755"
}
