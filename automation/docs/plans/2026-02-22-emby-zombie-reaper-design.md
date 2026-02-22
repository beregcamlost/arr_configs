# Emby Zombie Reaper Design

## Problem
Emby 4.9.3.0 accumulates stale sessions (64 at time of investigation, only 2 active). Smart TVs, phones, and browsers leave ghost connections open for weeks. Emby has no built-in session timeout.

## Solution
`emby_zombie_reaper.sh` — a cron-driven script that kills sessions idle longer than a threshold and sends a Discord summary.

## Detection Logic
1. Query `GET /Sessions?api_key=...`
2. For each session with a `LastActivityDate` older than `--max-idle` (default 150 min / 2.5h):
   - Skip if actively playing (has `NowPlayingItem` AND `IsPaused` is false)
   - Otherwise mark as zombie
3. Kill via `POST /Sessions/{Id}/Playing/Stop?api_key=...`
4. Send Discord embed listing killed sessions

## CLI Flags
- `--max-idle MINUTES` (default: 150)
- `--dry-run` — preview without killing
- `--emby-url URL` / `--emby-api-key KEY` (fallback to env vars)

## Cron
Every 30 minutes, sourcing `.env`, with flock.

## No state file needed — pure one-shot check and kill.
