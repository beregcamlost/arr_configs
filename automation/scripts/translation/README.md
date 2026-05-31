# 🌍 Subtitle Translator

> Fill in missing **Spanish** subtitles for your library using a **local Ollama LLM** — translating English subtitles to Spanish when Bazarr can't find a `es` subtitle in your profile. Where a genuine Spanish subtitle is already baked into the file, it's extracted directly instead of translated.

> **Scope:** This translator is **EN→ES only** and uses a **single local Ollama provider**. The old multi-provider stack (DeepL / Gemini / Google Translate) was **removed on 2026-04-29** — there are no third-party translation APIs, no API keys, no failover chain, and no character quotas anymore.

---

## 🗂️ Files

| File | Role |
|------|------|
| `translator.py` | 🚀 CLI entry point — `translate` and `status` commands |
| `ollama_client.py` | 🤖 Local Ollama LLM client — batched EN→ES SRT cue translation |
| `subtitle_scanner.py` | 🔎 Scan Bazarr DB for missing subtitles; find best source SRT; read profile langs |
| `srt_parser.py` | 📄 SRT parse/write with timing preservation |
| `db.py` | 🗄️ SQLite state DB — translation history, cooldown, permanent-fail guard |
| `config.py` | ⚙️ Config loader + Ollama language map (`OLLAMA_LANG_MAP`) |
| `discord.py` | 💬 Discord webhook — translation summaries |
| `lib_metrics.py` | 📊 Optional run metrics (fail-soft; no-op if missing) |
| `tests/` | ✅ pytest tests covering the modules |

---

## ✨ Features

- **🤖 Local Ollama provider** — translation runs entirely against a local Ollama LLM (`OLLAMA_BASE_URL` + `OLLAMA_MODEL`). No third-party APIs, no keys, no per-call cost.
- **🇪🇸 EN→ES only** — the only translation path is English → Spanish. Any non-`es` target language is skipped (a non-ES target would otherwise save Spanish content under the wrong language tag).
- **📼 Embedded-Spanish extraction fast-path** — before invoking the LLM for a missing `es` subtitle, the source media is probed for a genuine, non-forced, non–hearing-impaired Spanish text subtitle stream. If one exists and passes a language fingerprint, it's extracted directly to `{stem}.es.srt` (provider `embedded-extract`) — skipping GPU translation entirely. Bypass with `--no-embedded-fallback`.
- **⚡ Parallel batch translation** — `--since` / `--all` runs fan out across a `ThreadPoolExecutor` (default **8** workers, override with `TRANSLATOR_WORKERS`).
- **🔍 Bazarr profile integration** — reads the Bazarr language profile for each file to determine which languages are needed, then checks what's already present on disk.
- **⏱️ Cooldown + permanent-fail guard** — a per-`(media, target)` cooldown avoids re-hammering problem files; a permanent-fail guard skips files with a prior unrecoverable parse failure (scoped to `provider='ollama'`).
- **🏷️ Marker files** — translated output gets a `.ollama` marker; extracted embedded Spanish gets a `.embedded` marker. These signal `auto-maintain` to defer muxing until the subtitle is confirmed stable, and let you audit embedded-vs-translated.
- **🔔 Discord notifications** — a summary of translated/failed files after each run.
- **↔️ Two modes** — cron/batch mode (`--since N` or `--all`) and single-file import-hook mode (`--file PATH`).

---

## 🔧 CLI Usage

```bash
# Run from the scripts directory
cd /config/berenstuff/automation/scripts
python3 -m translation.translator <command> [options]
```

### 🔄 `translate` — Run translations

```bash
# Cron/batch mode: translate files with missing subs modified in the last N minutes
python3 -m translation.translator translate --since 10

# Translate all files with missing subtitles (mutually exclusive with --since)
python3 -m translation.translator translate --all

# Import-hook mode: translate a single just-imported file
python3 -m translation.translator translate --file "/path/to/Show.S01E01.mkv"

# Cap the number of files processed in this batch run
python3 -m translation.translator translate --since 10 --max-files 3

# Skip the embedded-Spanish fast-path and always use the LLM translator
python3 -m translation.translator translate --since 10 --no-embedded-fallback
```

**Options**

| Option | Meaning |
|--------|---------|
| `--since N` | Only process files modified in the last `N` minutes |
| `--all` | Process all files with missing subtitles (mutually exclusive with `--since`) |
| `--file PATH` | Translate a single file |
| `--max-files N` | Cap files processed per batch run (applied after the scan) |
| `--no-embedded-fallback` | Skip the embedded-Spanish track check; always use the LLM translator |
| `--state-dir DIR` | Override the state directory |
| `--bazarr-db PATH` | Override the Bazarr DB path |

> `OLLAMA_BASE_URL` must be set — `translate` exits with an error if it isn't, since the local LLM is the only provider.

### 📊 `status` — Recent activity

```bash
python3 -m translation.translator status
# Output:
#   Monthly usage: 128,540 chars
#     embedded-extract: 41,902 chars
#     ollama: 86,638 chars
#
#   Recent translations (10):
#     2026-05-30 14:22:05 | success         | en->es | 3,241 chars | ollama  | Show.S02E05.mkv
#     2026-05-30 14:20:11 | success         | spa-embedded->es |  5,108 chars | embedded-extract | Movie.mkv
#     2026-05-30 14:18:44 | no_source       | ?->es  |      0 chars | ollama  | Other.mkv
```

`status` reports total monthly characters with a **per-provider breakdown** (`ollama`, `embedded-extract`) and the 10 most recent rows. There is **no quota denominator or percentage** — translation is local and uncapped.

---

## 🏗️ Architecture

```
Import event (Sonarr/Radarr) ─── or ─── Cron (media_pipeline.sh slow lane)
       │                                        │
       ▼                                        ▼
translator.py --file /path/file.mkv     translator.py --since 10 --max-files 3
       │                                        │
       │                          scan_recent_missing()  ← Bazarr DB query
       │                                        │  (fan out across 8 workers)
       └────────────────┬───────────────────────┘
                        ▼
        _resolve_profile_for_path()           ← Bazarr profileId for the file
        get_profile_langs()                   ← languages required by the profile
        find_missing_langs_on_disk()          ← which profile langs are absent
                        │
                        ▼  (for each missing target; non-'es' targets skipped)
        is_permanently_failed() / is_on_cooldown()   ← retry guards
                        │
            ┌───────────┴────────────────────────────────┐
            ▼                                             ▼
  Embedded-Spanish fast-path                    LLM translation path
  (es target, source is media file,             (no usable embedded Spanish)
   --no-embedded-fallback NOT set)                        │
            │                              find_best_source_srt()  ← English source SRT
  probe streams (mkvmerge -J /ffprobe)                    │
  extract (mkvextract / ffmpeg copy)         parse_srt(source)
  language-fingerprint (langdetect /                      │
    ES/EN marker ratio)                      _translate_cues_with_ollama()  ← EN→ES
            │                                              │
  write {stem}.es.srt                          write {stem}.es.srt
  touch {stem}.es.srt.embedded                 touch {stem}.es.srt.ollama
  record_translation(provider=                 record_translation(provider=
    'embedded-extract')                          'ollama')
            └───────────────────┬─────────────────────────┘
                                ▼
                  _trigger_bazarr_rescan()   ← Bazarr scan-disk picks up the new SRT
                                ▼
                  notify_translations()      ← Discord summary
```

### 🗄️ State Database

SQLite, resolved as:

- `${PIPELINE_DB}` when the `PIPELINE_DB` env var is set (centralized pipeline DB — Phase 6), **or**
- `{state_dir}/translation_state.db` otherwise.

It tracks every translation attempt — media path, source lang, target lang, chars used, status, provider, timestamp — and backs the retry guards:

- **Cooldown** (`is_on_cooldown`) — per `(media_path, target_lang)`, prevents re-hammering problem files.
- **Permanent-fail** (`is_permanently_failed`) — skips files with a prior unrecoverable parse failure, scoped to `provider='ollama'` (legacy DeepL-era failures no longer block Ollama retries).

Monthly character totals are aggregated **per provider** for the `status` view and the Discord summary — purely informational, not a budget.

### 🗺️ Language Mapping

`config.py` exposes `OLLAMA_LANG_MAP`, which maps the ISO language codes used on disk to the language names the Ollama prompt expects. In practice only the EN→ES pair is exercised:

```
en → English
es → Spanish
```

A target is translated only if its base language is `es`; the source side accepts the supported source languages, but the live path is `en → es`.

---

## 🧪 Tests

```bash
cd /config/berenstuff/automation/scripts
python3 -m pytest translation/tests/ -v
```

---

## ⚙️ Configuration

Config is loaded from the environment / `/config/berenstuff/.env`. The translator is **local-only** — there are no API keys, no per-key budgets, and no quota tracking.

```bash
# ── Ollama (translation provider) ─────────────────────────────────
OLLAMA_BASE_URL=http://127.0.0.1:11434   # REQUIRED — translate exits if unset
OLLAMA_MODEL=...                         # model used for EN→ES translation

# ── Bazarr (profile lookup + post-translation rescan) ─────────────
BAZARR_URL=http://127.0.0.1:6767/bazarr
BAZARR_API_KEY=...
# Bazarr DB path (also overridable per-run with --bazarr-db)

# ── Notifications ─────────────────────────────────────────────────
DISCORD_WEBHOOK_URL=...

# ── State / parallelism ───────────────────────────────────────────
PIPELINE_DB=...            # optional — use the centralized pipeline DB instead of state_dir
TRANSLATOR_WORKERS=8       # parallel workers for --since/--all (default 8)
```

| Variable | Purpose | Required |
|----------|---------|----------|
| `OLLAMA_BASE_URL` | Base URL of the local Ollama server (the sole translation provider) | **Yes** — `translate` exits if unset |
| `OLLAMA_MODEL` | Ollama model used for EN→ES translation | No (uses the client default) |
| `BAZARR_URL` | Bazarr base URL for triggering post-translation `scan-disk` | No (rescan skipped if unset) |
| `BAZARR_API_KEY` | Bazarr API key for the rescan call | No (rescan skipped if unset) |
| `DISCORD_WEBHOOK_URL` | Discord webhook for run summaries | No |
| `PIPELINE_DB` | Path to the centralized pipeline DB; overrides the default `{state_dir}/translation_state.db` | No |
| `TRANSLATOR_WORKERS` | Worker count for parallel `--since` / `--all` batches (default `8`) | No |

### ⏰ Scheduling

The translator is **not** run from its own cron. It is invoked by **`media_pipeline.sh`'s slow lane** (cron every 30 min, guarded by `flock /tmp/media_pipeline_slow.lock`) as the final step, roughly:

```bash
python3 translation/translator.py translate \
  --since ${TRANSLATOR_SINCE_MINUTES:-10} \
  --max-files ${TRANSLATOR_MAX_FILES_PER_RUN:-3} \
  --bazarr-db <bazarr.db>
```

So the real cadence into the translator is a **10-minute window capped at 3 files per run** (`--since 10 --max-files 3`). The old standalone `/tmp/deepl_translate.lock` `--since 60` cron is obsolete.
