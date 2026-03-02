#!/usr/bin/env bash
set -euo pipefail

EMBY_URL="${EMBY_URL:-http://127.0.0.1:8096}"
EMBY_API_KEY="${EMBY_API_KEY:-}"
MAX_IDLE_MIN=300
DRY_RUN=0
STATE_DIR="/APPBOX_DATA/storage/.zombie-reaper-state"
RESTART_THRESHOLD=20

usage() {
  cat <<'EOF'
Usage: emby_zombie_reaper.sh [options]

Kill Emby sessions that have been idle or paused longer than a threshold.

Options:
  --emby-url URL       Emby base URL (default: $EMBY_URL or http://127.0.0.1:8096)
  --api-key KEY        Emby API key (or set EMBY_API_KEY)
  --max-idle MINUTES   Kill sessions idle longer than this (default: 300 = 5h)
  --dry-run            List zombies without killing them
  --state-dir DIR      Directory for state tracking (default: /APPBOX_DATA/storage/.zombie-reaper-state)
  --restart-threshold N  Restart Emby when zombie count >= N (default: 20, 0=disable)
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
    --max-idle)          MAX_IDLE_MIN="${2:-}"; shift 2 ;;
    --dry-run)           DRY_RUN=1; shift ;;
    --state-dir)         STATE_DIR="${2:-}"; shift 2 ;;
    --restart-threshold) RESTART_THRESHOLD="${2:-}"; shift 2 ;;
    --help|-h)           usage; exit 0 ;;
    *)                   echo "Unknown option: $1" >&2; usage; exit 1 ;;
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

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/seen_sessions.json"
[[ -f "$STATE_FILE" ]] || echo '{}' > "$STATE_FILE"

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

# Auto-restart check: if total zombies >= threshold, restart Emby instead
if [[ "$RESTART_THRESHOLD" -gt 0 ]] && [[ "$zombie_count" -ge "$RESTART_THRESHOLD" ]]; then
  log "Zombie count ($zombie_count) >= restart threshold ($RESTART_THRESHOLD). Attempting Emby restart..."

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "[DRY-RUN] Would restart Emby to clear ${zombie_count} stale sessions."
  else
    max_retries=12
    retry_delay=300  # 5 minutes
    restarted=0

    for attempt in $(seq 1 "$max_retries"); do
      # Check for active playback
      active_count="$(jq '[.[] | select(.NowPlayingItem != null and ((.PlayState.IsPaused // false) == false))] | length' <<<"$sessions_json")"

      if [[ "$active_count" -gt 0 ]]; then
        log "Restart delayed: $active_count active session(s) (attempt $attempt/$max_retries)"
        if [[ "$attempt" -lt "$max_retries" ]]; then
          sleep "$retry_delay"
          # Re-fetch sessions for next check
          sessions_json="$(curl -fsS "${EMBY_URL}/Sessions?api_key=${EMBY_API_KEY}")"
        fi
        continue
      fi

      # Nobody watching — restart
      if curl -fsS -X POST "${EMBY_URL}/System/Restart?api_key=${EMBY_API_KEY}" >/dev/null 2>&1; then
        log "Emby restarted successfully (attempt $attempt/$max_retries). Clearing state."
        echo '{}' > "$STATE_FILE"
        restarted=1

        # Discord notification
        if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
          discord_payload="$(jq -nc \
            --arg title "🔄 Emby Auto-Restart" \
            --arg desc "Restarted Emby to clear **$zombie_count** stale sessions" \
            --argjson zombies "$zombie_count" \
            --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
            '{embeds: [{
              title: $title,
              description: $desc,
              color: 3066993,
              fields: [
                {name: "👻 Zombies Cleared", value: ($zombies | tostring), inline: true},
                {name: "⏳ Server Status", value: "Restarting (~30s)", inline: true}
              ],
              footer: {text: "Emby Zombie Reaper"},
              timestamp: $ts
            }]}')"
          curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
            -X POST "$DISCORD_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "$discord_payload" >/dev/null 2>&1 || true
        fi
        break
      else
        log "Restart command FAILED (attempt $attempt/$max_retries)"
        break
      fi
    done

    if [[ "$restarted" -eq 0 ]]; then
      log "Restart aborted after $max_retries retries (active playback). Will retry next cron run."
    fi
  fi

  exit 0
fi

# Load existing state
seen_json="$(cat "$STATE_FILE")"

# Classify zombies as new or already-seen
new_zombies="$(jq -c --argjson seen "$seen_json" '
  [ .[] | select(.Id as $id | $seen[$id] == null) ]
' <<<"$zombies_json")"
seen_zombies="$(jq -c --argjson seen "$seen_json" '
  [ .[] | select(.Id as $id | $seen[$id] != null) ]
' <<<"$zombies_json")"

new_count="$(jq 'length' <<<"$new_zombies")"
seen_count="$(jq 'length' <<<"$seen_zombies")"

log "Found ${zombie_count} zombie(s): ${new_count} new, ${seen_count} already-seen."

# Clean state: remove entries for sessions that no longer exist in the API
all_session_ids="$(jq -r '[.[].Id] | join(",")' <<<"$sessions_json")"
seen_json="$(jq --arg ids "$all_session_ids" '
  ($ids | split(",")) as $active |
  with_entries(select(.key as $k | $active | index($k) != null))
' <<<"$seen_json")"

# Process only NEW zombies
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

  # Add to state file (mark as seen)
  seen_json="$(jq --arg id "$session_id" --arg dev "$device" --arg usr "$user" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '. + {($id): {first_seen: $ts, device: $dev, user: $usr}}' <<<"$seen_json")"

  killed_summary="${killed_summary}${line}\n"
done < <(jq -c '.[]' <<<"$new_zombies")

# Save updated state (skip in dry-run mode)
if [[ "$DRY_RUN" -eq 0 ]]; then
  printf '%s\n' "$seen_json" > "$STATE_FILE"
fi

# Discord notification — only when there are new zombies
if [[ "$new_count" -gt 0 ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    _zr_title="👻 Emby Zombie Reaper [DRY RUN]"
    _zr_action="detected"
  else
    _zr_title="👻 Emby Zombie Reaper"
    _zr_action="killed"
  fi

  _zr_desc="$_zr_action **$new_count** zombie session(s)"
  [[ "$seen_count" -gt 0 ]] && _zr_desc="${_zr_desc} · **$seen_count** previously seen (skipped)"

  # Build session list for field
  _zr_sessions="$(printf '%b' "$killed_summary" | while IFS= read -r _line; do
    [[ -n "$_line" ]] && printf '• `%s`\n' "$_line"
  done)"

  # Color: red for kills, orange for dry-run
  _zr_color=15158332
  [[ "$DRY_RUN" -eq 1 ]] && _zr_color=15105570

  _zr_fields="$(jq -nc \
    --arg sessions "$_zr_sessions" \
    --arg new "$new_count" \
    --arg seen "$seen_count" \
    --arg failed "$kill_failed" \
    '[
      {name: "💀 Sessions", value: $sessions, inline: false},
      {name: "🆕 New", value: $new, inline: true},
      {name: "👀 Seen", value: $seen, inline: true},
      {name: "❌ Failed", value: $failed, inline: true}
    ]')"

  _zr_footer="Threshold: $(( MAX_IDLE_MIN / 60 ))h idle · Restart at $RESTART_THRESHOLD zombies"

  discord_payload="$(jq -nc \
    --arg title "$_zr_title" \
    --arg desc "$_zr_desc" \
    --argjson color "$_zr_color" \
    --arg footer "$_zr_footer" \
    --argjson fields "$_zr_fields" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: $fields,
      footer: {text: $footer},
      timestamp: $ts
    }]}')"

  curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
    -X POST "$DISCORD_WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$discord_payload" \
    >/dev/null 2>&1 \
    && log "Discord notification sent." \
    || log "Discord notification failed (non-fatal)."
fi

if [[ "$new_count" -gt 0 ]]; then
  log "Done. ${new_count} new zombie(s) processed, ${seen_count} already-seen skipped."
else
  log "Done. No new zombies (${seen_count} already-seen, waiting for restart to clear)."
fi
