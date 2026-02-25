# Zombie Reaper Improvements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the zombie reaper smart — track seen sessions to avoid re-killing, and auto-restart Emby when zombie count exceeds 20.

**Architecture:** Add a JSON state file to track already-processed session IDs. Each run classifies zombies as "new" or "already-seen". Only new zombies get a stop attempt + Discord notification. When total zombies hit 20+, trigger an Emby restart (with active-playback safety check and retry loop). State file is cleared on restart.

**Tech Stack:** Bash, jq, curl, Emby REST API

---

### Task 1: Add state directory and CLI flags

**Files:**
- Modify: `automation/scripts/emby_zombie_reaper.sh:1-56`

**Context:** The script currently has 4 CLI flags (`--emby-url`, `--api-key`, `--max-idle`, `--dry-run`). We need to add a `--state-dir` flag and a `--restart-threshold` flag. The state directory defaults to `/APPBOX_DATA/storage/.zombie-reaper-state`.

**Step 1: Add new variables and CLI flags**

After line 7 (`DRY_RUN=0`), add:

```bash
STATE_DIR="/APPBOX_DATA/storage/.zombie-reaper-state"
RESTART_THRESHOLD=20
```

Add to the usage text (after `--dry-run` line):

```
  --state-dir DIR      Directory for state tracking (default: /APPBOX_DATA/storage/.zombie-reaper-state)
  --restart-threshold N  Restart Emby when zombie count >= N (default: 20, 0=disable)
```

Add to the `case` block:

```bash
    --state-dir)           STATE_DIR="${2:-}"; shift 2 ;;
    --restart-threshold)   RESTART_THRESHOLD="${2:-}"; shift 2 ;;
```

After the existing validation block (line 50), add:

```bash
mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/seen_sessions.json"
[[ -f "$STATE_FILE" ]] || echo '{}' > "$STATE_FILE"
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/emby_zombie_reaper.sh`
Expected: no output (clean parse)

**Step 3: Commit**

```bash
git add automation/scripts/emby_zombie_reaper.sh
git commit -m "feat(zombie-reaper): add --state-dir and --restart-threshold CLI flags"
```

---

### Task 2: State tracking — classify new vs already-seen zombies

**Files:**
- Modify: `automation/scripts/emby_zombie_reaper.sh:95-137`

**Context:** After the jq filter produces `zombies_json` (the array of zombie sessions), the current code loops through ALL zombies and tries to kill each one. We need to split them into "new" (not in state file) and "seen" (already in state file), then only process new ones.

**Step 1: Add state classification after `zombie_count` check**

Replace the block from `log "Found ${zombie_count} zombie session(s)."` through the end of the `while` loop (lines 102-137) with this logic:

```bash
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

# Save updated state
printf '%s\n' "$seen_json" > "$STATE_FILE"
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/emby_zombie_reaper.sh`
Expected: no output (clean parse)

**Step 3: Commit**

```bash
git add automation/scripts/emby_zombie_reaper.sh
git commit -m "feat(zombie-reaper): track seen sessions, only process new zombies"
```

---

### Task 3: Update Discord notification for new-only reporting

**Files:**
- Modify: `automation/scripts/emby_zombie_reaper.sh` (Discord notification block, currently lines 139-170)

**Context:** The Discord notification currently reports all zombies. It should now only report new zombies (skip notification entirely when `new_count == 0`). Also include the total/seen count for context.

**Step 1: Update the Discord notification block**

Replace the entire Discord notification block with:

```bash
# Discord notification — only when there are new zombies
if [[ "$new_count" -gt 0 ]] && [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    title="Emby Zombie Reaper [DRY RUN]"
    action_word="detected"
  else
    title="Emby Zombie Reaper"
    action_word="killed"
  fi

  fail_note=""
  [[ "$kill_failed" -gt 0 ]] && fail_note="\n(${kill_failed} session(s) failed to stop)"

  context_note=""
  [[ "$seen_count" -gt 0 ]] && context_note="\n(${seen_count} previously-seen zombie(s) skipped)"

  discord_payload="$(jq -nc \
    --arg title "$title" \
    --arg desc "$(printf "**%s %d new zombie session(s):**\n\n\`\`\`\n%b\`\`\`%b%b" "$action_word" "$new_count" "$killed_summary" "$fail_note" "$context_note")" \
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
```

Also update the final log line:

```bash
if [[ "$new_count" -gt 0 ]]; then
  log "Done. ${new_count} new zombie(s) processed, ${seen_count} already-seen skipped."
else
  log "Done. No new zombies (${seen_count} already-seen, waiting for restart to clear)."
fi
```

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/emby_zombie_reaper.sh`
Expected: no output (clean parse)

**Step 3: Commit**

```bash
git add automation/scripts/emby_zombie_reaper.sh
git commit -m "feat(zombie-reaper): Discord notification only for new zombies"
```

---

### Task 4: Auto-restart when zombie count exceeds threshold

**Files:**
- Modify: `automation/scripts/emby_zombie_reaper.sh`

**Context:** When the total zombie count (new + seen) reaches the restart threshold (default 20), the script should restart Emby instead of processing individual sessions. It must check for active playback first, retrying every 5min for up to 1h. On restart, clear the state file and send a Discord notification.

**Step 1: Add auto-restart logic**

Insert this block **before** the new/seen zombie classification (i.e., right after `zombie_count` is computed and the `zombie_count -eq 0` early exit). This ensures restart takes priority over per-session processing:

```bash
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
            --arg title "Emby Auto-Restart" \
            --arg desc "$(printf "**Restarted Emby to clear %d stale sessions.**\n\nServer will be back in ~30 seconds." "$zombie_count")" \
            --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
            '{embeds: [{
              title: $title,
              description: $desc,
              color: 3066993,
              footer: {text: "Emby Zombie Reaper"},
              timestamp: $ts
            }]}')"
          curl -sS -X POST "$DISCORD_WEBHOOK_URL" \
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
```

Key details:
- This block goes **after** `zombie_count` is computed and the `zombie_count -eq 0` early exit, but **before** the state classification/per-session processing
- `exit 0` at the end ensures we never do both restart AND per-session kills in the same run
- Re-fetches sessions on each retry to get fresh playback state
- Clears state file on successful restart (all sessions will be gone)

**Step 2: Verify syntax**

Run: `bash -n automation/scripts/emby_zombie_reaper.sh`
Expected: no output (clean parse)

**Step 3: Commit**

```bash
git add automation/scripts/emby_zombie_reaper.sh
git commit -m "feat(zombie-reaper): auto-restart Emby when zombie count >= threshold"
```

---

### Task 5: Compat sync and final validation

**Files:**
- Modify: `scripts/emby_zombie_reaper.sh` (compat copy)

**Step 1: Sync compat copy**

```bash
cp automation/scripts/emby_zombie_reaper.sh scripts/emby_zombie_reaper.sh
```

**Step 2: Syntax check both copies**

Run: `bash -n automation/scripts/emby_zombie_reaper.sh && bash -n scripts/emby_zombie_reaper.sh`
Expected: no output (clean parse)

**Step 3: Dry-run test**

Run: `source /config/berenstuff/.env && bash automation/scripts/emby_zombie_reaper.sh --dry-run`

Expected output should show:
- "Found N zombie(s): N new, 0 already-seen" (first run, state file is empty)
- `[DRY-RUN] Would kill: ...` for each zombie
- OR `[DRY-RUN] Would restart Emby to clear N stale sessions.` if count >= 20

**Step 4: Live test (non-dry-run)**

Run: `source /config/berenstuff/.env && bash automation/scripts/emby_zombie_reaper.sh`

Verify:
- State file created at `/APPBOX_DATA/storage/.zombie-reaper-state/seen_sessions.json`
- State file contains the session IDs that were processed
- Discord notification only shows new zombies

Then run again immediately:

Run: `source /config/berenstuff/.env && bash automation/scripts/emby_zombie_reaper.sh`

Verify:
- "Found N zombie(s): 0 new, N already-seen"
- No Discord notification (no new zombies)
- Log says "No new zombies (N already-seen, waiting for restart to clear)."

**Step 5: Commit**

```bash
git add scripts/emby_zombie_reaper.sh
git commit -m "chore: sync emby_zombie_reaper.sh compat copy"
```
