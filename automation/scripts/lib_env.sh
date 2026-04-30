#!/usr/bin/env bash
# lib_env.sh — Atomic .env loader
#
# Source this library, then call: load_env [path/to/.env]
#
# Problem solved: if .env is being edited mid-run, a plain `source .env` may
# read a partial file (half-written line, truncated value).  Snapshotting via
# cp into a temp file gives us an atomic read of whatever bytes are currently
# on disk — the kernel cp is a single syscall that cannot see a partial write.
#
# Usage:
#   source /path/to/lib_env.sh
#   load_env /config/berenstuff/.env

# Guard against double-sourcing
[[ -n "${_LIB_ENV_LOADED:-}" ]] && return 0
readonly _LIB_ENV_LOADED=1

# load_env <env_file>
#   Copies <env_file> to a mktemp path, sources it with set -a (export all),
#   then removes the temp file.  The RETURN trap is scoped to this function
#   call only; it does NOT clobber any caller trap on EXIT.
load_env() {
  local env_file="${1:-/config/berenstuff/.env}"
  if [[ ! -f "$env_file" ]]; then
    printf '[lib_env] WARNING: env file not found: %s\n' "$env_file" >&2
    return 0
  fi

  local tmp
  tmp="$(mktemp /tmp/.env.$$.XXXXXX)"

  # Ensure temp file is removed even if sourcing fails.
  # RETURN trap is function-scoped; does not clobber the caller's EXIT trap.
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN

  cp "$env_file" "$tmp"

  set -a
  # shellcheck source=/dev/null
  source "$tmp"
  set +a

  rm -f "$tmp"
  trap - RETURN
}
