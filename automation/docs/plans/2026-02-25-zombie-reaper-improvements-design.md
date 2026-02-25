# Zombie Reaper Improvements Design

**Date:** 2026-02-25
**Scope:** `emby_zombie_reaper.sh` (~173 lines → ~250 lines)

## Problem

`POST /Sessions/{Id}/Playing/Stop` doesn't remove sessions from Emby's in-memory session list — it only sends a "stop playback" command to the client. Since zombie sessions have no active playback, the command does nothing. The same 14-16 sessions get "killed" every 30 minutes, Discord gets spammed, and nothing actually changes. Only a server restart clears stale sessions.

## 1. State Tracking

Track processed session IDs in a JSON state file at `/APPBOX_DATA/storage/.zombie-reaper-state/seen_sessions.json`.

Format: `{ "session_id": { "first_seen": "ISO8601", "device": "...", "user": "..." }, ... }`

Each run:
- **New zombies** (ID not in state file): attempt `Playing/Stop`, log, add to state file
- **Already-seen zombies** (ID in state file): skip silently — no stop command, no log, no notification
- **State cleanup**: remove entries whose session ID no longer appears in the Emby API response (cleared by restart)
- Discord notification only fires when there are new zombies (not for re-seen ones)

## 2. Auto-Restart

When total zombie count (new + already-seen) reaches **20+**:

- Check for active playback: `NowPlayingItem != null && IsPaused == false`
- If nobody watching: `POST /System/Restart`, clear state file, Discord notification ("Emby restarted: N stale sessions cleared")
- If someone watching: retry every 5min for up to 1h (12 retries max)
- If still blocked after 1h: skip, log warning — next cron run (30min) will re-evaluate
- Restart is a separate code path from per-session stop — never both in same run

## 3. Cron

- Keep existing `2,32 * * * *` schedule (every 30min)
- Keep Tuesday `3 5 * * 2` weekly restart as safety net (unchanged)

## Files Modified

- `automation/scripts/emby_zombie_reaper.sh` — state tracking, auto-restart logic
- `scripts/emby_zombie_reaper.sh` — compat sync
