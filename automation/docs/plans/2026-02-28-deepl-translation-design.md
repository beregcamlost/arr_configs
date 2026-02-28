# DeepL Subtitle Translation — Design

## Problem

Bazarr has no translation provider configured — only download providers (opensubtitlescom, subf2m, embeddedsubtitles). When Bazarr can't find a subtitle in a profile language, the only option is manual Google Translate (one sub at a time). The Bazarr `action=translate` API endpoint exists but does nothing without a provider. Docker isn't available for Lingarr.

## Solution

Standalone Python script using the DeepL free API (500K chars/month) to automatically translate missing profile-language subtitles from the best available source SRT.

## Architecture

```
automation/scripts/translation/
├── __init__.py
├── translator.py          # CLI entry point (click)
├── config.py              # Config dataclass, load from env
├── db.py                  # SQLite state: translation log, cooldowns
├── deepl_client.py        # DeepL SDK wrapper
├── srt_parser.py          # Parse/write SRT files
├── subtitle_scanner.py    # Find missing subs via Bazarr DB + filesystem
├── discord.py             # Notification embeds
└── tests/
    └── test_*.py
```

## Entry Points

1. **Cron** (`translate --since 60`): every 30 min, scans Bazarr DB for recently imported files with missing profile-language SRTs, translates from best available source
2. **Import hook** (`translate --file <path>`): single-file mode, called from `arr_profile_extract_on_import.sh` in background (replaces broken Bazarr translate fallback)

## CLI Subcommands

- `translate --since N` — cron mode, scan + translate recent files
- `translate --file PATH` — single-file mode for import hook
- `status` — chars used this month, recent translations, failures
- `usage` — query DeepL API for remaining quota

## Core Flow

1. Parse source SRT — extract text blocks, preserve timestamps/indices
2. Batch text into ~4KB chunks for DeepL API
3. Translate each chunk with source_lang + target_lang
4. Reassemble: original timestamps + translated text → write `<stem>.<lang>.srt`
5. Trigger Bazarr scan-disk so it picks up the new file

## Source SRT Selection

- Find `<stem>.*.srt` files in media directory
- Skip forced subs, skip same-language as target
- Pick largest file (most complete)
- Prefer English source when available (best DeepL quality)

## Missing Subtitle Detection

- Query Bazarr DB `table_episodes.missing_subtitles` / `table_movies.missing_subtitles`
- Cross-reference with profile langs via `table_languages_profiles`
- Filter by file mtime within `--since` minutes (recent imports only)
- Skip files already in translation_log within 24h cooldown

## State DB

Location: `/APPBOX_DATA/storage/.translation-state/translation_state.db`

`translation_log` table:
- `media_path TEXT` — path to media file
- `source_lang TEXT` — source SRT language
- `target_lang TEXT` — translation target language
- `chars_used INTEGER` — characters consumed
- `status TEXT` — success/failed/quota_exceeded
- `created_at TEXT` — ISO timestamp

24h cooldown per `(media_path, target_lang)` — no retry within a day.

## Config

From `.env` (new variable: `DEEPL_API_KEY`):
- `DEEPL_API_KEY` — DeepL free API key
- `BAZARR_API_KEY` — for scan-disk rescan
- `DISCORD_WEBHOOK_URL` — notifications

Defaults: Bazarr URL `http://127.0.0.1:6767/bazarr`, DB `/opt/bazarr/data/db/bazarr.db`

## Import Hook Integration

Replace broken Bazarr translate fallback in `arr_profile_extract_on_import.sh`:

```bash
(
  sleep 10
  source /config/berenstuff/.env
  PYTHONPATH=/config/berenstuff/automation/scripts \
    python3 /config/berenstuff/automation/scripts/translation/translator.py \
    translate --file "$file_path"
) >> "${LOG}" 2>&1 </dev/null &
disown
```

## Cron

```
*/30 * * * * source /config/berenstuff/.env && flock -n /tmp/deepl_translate.lock \
  PYTHONPATH=/config/berenstuff/automation/scripts \
  python3 /config/berenstuff/automation/scripts/translation/translator.py \
  translate --since 60 >> /config/berenstuff/automation/logs/deepl_translate.log 2>&1
```

## Error Handling

- DeepL 456 (quota exceeded) → log, Discord alert, stop
- DeepL 429 (rate limit) → SDK retry with backoff
- File locked by codec converter → skip, next run
- Empty/corrupt source SRT → skip, log warning

## Not In Scope

- No backfill of old content (quota protection — recent imports only)
- No translation quality scoring
- No segment caching
- No web UI

## Dependencies

- `deepl` Python SDK (`pip install deepl`)
- `click`, `requests` (already installed)
- Python 3.12 (already available)
