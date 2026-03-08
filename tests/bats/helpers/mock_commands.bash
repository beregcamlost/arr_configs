# mock_commands.bash — PATH-based mocks for external commands

setup_mocks() {
  MOCK_BIN="$(mktemp -d)"
  export PATH="$MOCK_BIN:$PATH"
}

teardown_mocks() {
  [[ -n "${MOCK_BIN:-}" && -d "$MOCK_BIN" ]] && rm -rf "$MOCK_BIN"
}

mock_command() {
  local cmd="$1" output="$2" exit_code="${3:-0}"
  cat > "$MOCK_BIN/$cmd" <<MOCK_EOF
#!/bin/bash
echo "$output"
exit $exit_code
MOCK_EOF
  chmod +x "$MOCK_BIN/$cmd"
}

# Mock that captures arguments for later assertion
mock_command_spy() {
  local cmd="$1" output="${2:-}" exit_code="${3:-0}"
  cat > "$MOCK_BIN/$cmd" <<MOCK_EOF
#!/bin/bash
echo "\$@" >> "$MOCK_BIN/${cmd}.calls"
echo "$output"
exit $exit_code
MOCK_EOF
  chmod +x "$MOCK_BIN/$cmd"
}

get_mock_calls() {
  local cmd="$1"
  cat "$MOCK_BIN/${cmd}.calls" 2>/dev/null || true
}
