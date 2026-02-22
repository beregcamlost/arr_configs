#!/usr/bin/env bash
set -euo pipefail

EMBY_URL="${EMBY_URL:-http://127.0.0.1:8096}"
EMBY_API_KEY="${EMBY_API_KEY:-}"
MAX_IDLE_MIN=300
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: emby_zombie_reaper.sh [options]

Kill Emby sessions that have been idle or paused longer than a threshold.

Options:
  --emby-url URL       Emby base URL (default: $EMBY_URL or http://127.0.0.1:8096)
  --api-key KEY        Emby API key (or set EMBY_API_KEY)
  --max-idle MINUTES   Kill sessions idle longer than this (default: 300 = 5h)
  --dry-run            List zombies without killing them
  --help               Show this help

Examples:
  EMBY_API_KEY=xxxxx emby_zombie_reaper.sh
  emby_zombie_reaper.sh --api-key xxxxx --max-idle 60
  emby_zombie_reaper.sh --dry-run
EOF
}

log() { printf '%s [zombie-reaper] %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S')" "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --emby-url)   EMBY_URL="${2:-}"; shift 2 ;;
    --api-key)    EMBY_API_KEY="${2:-}"; shift 2 ;;
    --max-idle)   MAX_IDLE_MIN="${2:-}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --help|-h)    usage; exit 0 ;;
    *)            echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "$EMBY_API_KEY" ]]; then
  echo "EMBY_API_KEY is required (or pass --api-key)." >&2
  exit 1
fi

if ! [[ "$MAX_IDLE_MIN" =~ ^[0-9]+$ ]] || [[ "$MAX_IDLE_MIN" -le 0 ]]; then
  echo "--max-idle must be a positive integer." >&2
  exit 1
fi

LOCK_FILE="/tmp/emby_zombie_reaper.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another instance is already running"; exit 0; }

EMBY_URL="${EMBY_URL%/}"
NOW_EPOCH="$(date -u +%s)"
MAX_IDLE_SEC=$((MAX_IDLE_MIN * 60))

log "Fetching sessions from ${EMBY_URL} (max idle: ${MAX_IDLE_MIN}m)..."

sessions_json="$(curl -fsS "${EMBY_URL}/Sessions?api_key=${EMBY_API_KEY}")"

# Find zombies: sessions idle or paused longer than threshold.
# Skip sessions actively playing (has NowPlayingItem AND not paused).
zombies_json="$(jq -r \
  --argjson now "$NOW_EPOCH" \
  --argjson max_idle "$MAX_IDLE_SEC" '
  [
    .[]
    | select(.LastActivityDate != null)
    | .last_epoch = (
        .LastActivityDate
        | sub("\\.[0-9]+Z$"; "Z")
        | fromdateiso8601
      )
    | .idle_sec = ($now - .last_epoch)
    | select(.idle_sec > $max_idle)
    | select(
        # skip actively playing (not paused)
        (.NowPlayingItem == null) or
        ((.PlayState.IsPaused // false) == true)
      )
    | {
        Id,
        DeviceName: (.DeviceName // "unknown"),
        Client: (.Client // "unknown"),
        UserName: (.UserName // "no-user"),
        idle_sec,
        idle_hours: ((.idle_sec / 3600 * 10 | floor) / 10),
        is_paused: ((.NowPlayingItem != null) and ((.PlayState.IsPaused // false) == true)),
        now_playing: (.NowPlayingItem.Name // null)
      }
  ]
  | sort_by(-.idle_sec)
' <<<"$sessions_json")"

zombie_count="$(jq 'length' <<<"$zombies_json")"

if [[ "$zombie_count" -eq 0 ]]; then
  log "No zombie sessions found."
  exit 0
fi

log "Found ${zombie_count} zombie session(s)."

# Build a summary for logging and Discord
killed_summary=""
kill_failed=0

while IFS= read -r zombie; do
  session_id="$(jq -r '.Id' <<<"$zombie")"
  device="$(jq -r '.DeviceName' <<<"$zombie")"
  client="$(jq -r '.Client' <<<"$zombie")"
  user="$(jq -r '.UserName' <<<"$zombie")"
  idle_hours="$(jq -r '.idle_hours' <<<"$zombie")"
  is_paused="$(jq -r '.is_paused' <<<"$zombie")"
  now_playing="$(jq -r '.now_playing // empty' <<<"$zombie")"

  status_detail="idle"
  [[ "$is_paused" == "true" ]] && status_detail="paused on: ${now_playing}"

  line="${device} (${client}) | ${user} | ${idle_hours}h ${status_detail}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[DRY-RUN] Would kill: ${line}"
  else
    log "Killing: ${line}"
    if curl -fsS -X POST "${EMBY_URL}/Sessions/${session_id}/Playing/Stop?api_key=${EMBY_API_KEY}" \
         -H "Content-Type: application/json" \
         -d '{"Command":"Stop"}' >/dev/null 2>&1; then
      log "  Killed OK"
    else
      log "  Kill FAILED (session may already be gone)"
      kill_failed=$((kill_failed + 1))
    fi
  fi

  killed_summary="${killed_summary}${line}\n"
done < <(jq -c '.[]' <<<"$zombies_json")

# Discord notification
if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    title="Emby Zombie Reaper [DRY RUN]"
    action_word="detected"
  else
    title="Emby Zombie Reaper"
    action_word="killed"
  fi

  fail_note=""
  [[ "$kill_failed" -gt 0 ]] && fail_note="\n(${kill_failed} session(s) failed to stop)"

  discord_payload="$(jq -nc \
    --arg title "$title" \
    --arg desc "$(printf "**%s %d zombie session(s):**\n\n\`\`\`\n%b\`\`\`%b" "$action_word" "$zombie_count" "$killed_summary" "$fail_note")" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: 15158332,
      footer: {text: "Emby Zombie Reaper"},
      timestamp: $ts
    }]}')"

  curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$discord_payload" \
    >/dev/null 2>&1 \
    && log "Discord notification sent." \
    || log "Discord notification failed (non-fatal)."
fi

log "Done. ${zombie_count} zombie(s) ${DRY_RUN:+would be }processed."
