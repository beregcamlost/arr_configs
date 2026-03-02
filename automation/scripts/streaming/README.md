# 📡 Streaming Availability Checker

> Cross-reference your Radarr/Sonarr library against streaming providers (Netflix, Disney+, etc.) and automatically manage what you actually need to keep.

---

## 🗂️ Files

| File | Role |
|------|------|
| `streaming_checker.py` | 🚀 CLI entry point — all subcommands live here |
| `arr_client.py` | 🔌 Radarr + Sonarr API client (fetch, tag, delete) |
| `tmdb_client.py` | 🎬 TMDB Watch Providers API — batch flatrate checks |
| `streaming_api_client.py` | 📺 RapidAPI streaming availability — per-season data |
| `emby_client.py` | 🎮 Emby last-played lookups + active session check |
| `db.py` | 🗄️ SQLite state DB — tracks streaming history across scans |
| `config.py` | ⚙️ Config loader — reads `.env`, maps provider names to IDs |
| `discord.py` | 💬 Discord webhook notifications (new matches, deletions) |
| `tests/` | ✅ 134 tests (pytest) |

---

## ✨ Features

- **🎯 TMDB provider matching** — parallel batch checks across your entire library (10 workers)
- **📺 Per-season streaming detection** — RapidAPI tells you which seasons are on which service; auto-tags `keep-local` if you own seasons the service doesn't have
- **🏷️ Auto-tagging** — adds/removes `streaming-available` tag in Radarr/Sonarr as availability changes
- **🚪 Left-streaming tracking** — detects when content disappears from a service and untags it
- **🎮 Emby last-played integration** — filter deletions by watch history; skip actively-playing files
- **🛡️ Keep-local filtering** — items tagged `keep-local` are never deleted or reported as candidates
- **💬 Discord notifications** — rich embeds for new matches, left-streaming events, and deletions
- **🔍 Flexible filtering** — filter by provider, library, size, recency, play history

---

## 🔧 CLI Usage

```bash
# Run from the scripts directory
cd /config/berenstuff/automation/scripts
python3 -m streaming.streaming_checker <command> [options]
```

### 🔍 `scan` — Update streaming state from TMDB

```bash
# Standard scan (country + providers from .env)
python3 -m streaming.streaming_checker scan

# Override country or providers
python3 -m streaming.streaming_checker scan --country US --providers netflix,disney

# Preview without modifying tags
python3 -m streaming.streaming_checker scan --dry-run --verbose
```

### 📋 `report` — Show what's available on streaming

```bash
# Full report grouped by provider
python3 -m streaming.streaming_checker report

# Filter: Netflix only, items > 5 GB, never played in Emby
python3 -m streaming.streaming_checker report \
  --provider netflix --min-size 5 --no-play-days 999

# Sort by size descending, JSON output
python3 -m streaming.streaming_checker report --sort-by size --json
```

### 🗑️ `confirm-delete` — Delete items available on streaming

```bash
# Delete Netflix movies over 4 GB never played, dry-run first
python3 -m streaming.streaming_checker confirm-delete --yes \
  --provider netflix --min-size 4 --no-play-days 999 --dry-run

# For real (requires --yes flag)
python3 -m streaming.streaming_checker confirm-delete --yes \
  --provider netflix --min-size 4 --no-play-days 999
```

### 📺 `check-seasons` — Per-season streaming check + keep-local tagging

```bash
# Check which seasons are actually on streaming vs what you own
python3 -m streaming.streaming_checker check-seasons

# Preview which shows would get keep-local tag
python3 -m streaming.streaming_checker check-seasons --dry-run
```

### 📊 `summary` — Aggregate stats

```bash
python3 -m streaming.streaming_checker summary
python3 -m streaming.streaming_checker summary --json
```

### 🌍 `providers` — List available providers for a country

```bash
python3 -m streaming.streaming_checker providers --country CL
python3 -m streaming.streaming_checker providers --country US
```

---

## 🏗️ Architecture

```
Radarr API ─────┐
                ├──► arr_client.py ──► all_items[]
Sonarr API ─────┘                          │
                                           ▼
                              tmdb_client.batch_check()
                              (10 workers, TMDB Watch Providers)
                                           │
                              ┌────────────┴─────────────┐
                              ▼                           ▼
                         matches[]                  left_streaming[]
                              │                           │
                    ┌─────────┴────────┐        remove streaming-available tag
                    ▼                  ▼
              upsert db          add streaming-available tag
              (streaming_status)      │
                                      ▼
                               discord notify

check-seasons (separate):
  active TV matches ──► streaming_api_client (RapidAPI)
                     ──► compare owned seasons vs streaming seasons
                     ──► tag keep-local if mismatch
```

### 🗄️ State Database

SQLite at `/APPBOX_DATA/storage/.streaming-state/streaming_state.db`

- `streaming_status` — one row per (tmdb_id, media_type, provider_id); tracks `first_seen`, `last_seen`, `left_at`, `deleted_at`, `streaming_seasons`
- `scan_history` — per-scan metrics for trend tracking

---

## 🧪 Tests

```bash
cd /config/berenstuff/automation/scripts
python3 -m pytest streaming/tests/ -v
# 134 tests covering all modules
```

---

## ⚙️ Configuration

All secrets in `/config/berenstuff/.env`:

```bash
TMDB_API_KEY=...
RAPIDAPI_KEY=...           # for check-seasons
RADARR_URL=http://...
RADARR_KEY=...
SONARR_URL=http://...
SONARR_KEY=...
EMBY_URL=http://...
EMBY_API_KEY=...
DISCORD_WEBHOOK_URL=...
STREAMING_PROVIDERS=netflix,disney   # comma-separated
STREAMING_COUNTRY=CL
```
