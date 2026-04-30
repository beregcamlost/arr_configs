#!/usr/bin/env bash
# lib_transmission.sh — Shared Transmission RPC helper library.
#
# Provides: transmission_rpc PAYLOAD OUTPUT_FILE → prints HTTP status code
#
# Requires these env vars to be set before sourcing:
#   TRANSMISSION_URL   — full RPC URL (e.g. https://host/transmission/rpc)
#   TRANSMISSION_USER  — Transmission username
#   TRANSMISSION_PASS  — Transmission password
#
# Expects a `log` function to be defined by the sourcing script.
# Caches the session ID in TRANSMISSION_SESSION_ID (must be declared as a
# non-readonly variable in the sourcing script before sourcing this library).

set -euo pipefail

[[ -n "${_LIB_TRANSMISSION_LOADED:-}" ]] && return 0
_LIB_TRANSMISSION_LOADED=1

# Cached Transmission session ID — populated on first 409, reused for subsequent calls.
# Sourcing scripts that need to pre-declare this variable may do so before sourcing;
# this default assignment is a no-op if it is already set.
: "${TRANSMISSION_SESSION_ID:=}"

# transmission_rpc PAYLOAD OUTPUT_FILE
# Makes a Transmission RPC call, retrying once on 409 to pick up the session ID.
# Caches the session ID in TRANSMISSION_SESSION_ID for subsequent calls.
# Prints the HTTP status code.
transmission_rpc() {
  local payload="$1" output_file="$2"
  local http_code rpc_headers_file
  rpc_headers_file="$(mktemp)"

  http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
    -H 'Content-Type: application/json' \
    ${TRANSMISSION_SESSION_ID:+-H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID"} \
    -d "$payload" -D "$rpc_headers_file" -o "$output_file" -w '%{http_code}' \
    "$TRANSMISSION_URL" </dev/null || true)"

  if [[ "$http_code" == "409" ]]; then
    TRANSMISSION_SESSION_ID="$(awk -F': ' \
      'tolower($1)=="x-transmission-session-id"{gsub("\r","",$2); print $2}' \
      "$rpc_headers_file" | tail -n1)"
    http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
      -H 'Content-Type: application/json' \
      -H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID" \
      -d "$payload" -o "$output_file" -w '%{http_code}' \
      "$TRANSMISSION_URL" </dev/null || true)"
  fi

  rm -f "$rpc_headers_file"
  printf '%s' "$http_code"
}
