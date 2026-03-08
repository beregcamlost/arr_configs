# test_helper.bash — load bats-assert and source libs safely

BATS_TEST_DIRNAME="$(cd "$(dirname "${BATS_TEST_FILENAME}")" && pwd)"
VENDOR_DIR="$BATS_TEST_DIRNAME/vendor"

load "$VENDOR_DIR/bats-support/load"
load "$VENDOR_DIR/bats-assert/load"

# Project paths
PROJECT_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
SCRIPTS_DIR="$PROJECT_ROOT/automation/scripts"
SUBTITLES_DIR="$SCRIPTS_DIR/subtitles"
TRANSCODE_DIR="$SCRIPTS_DIR/transcode"
FIXTURES_DIR="$BATS_TEST_DIRNAME/fixtures"
