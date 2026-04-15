# 🌍 Subtitle Translator

> Automatically translate missing subtitle languages using Gemini, DeepL, or Google Translate — bridging the gap when Bazarr can't find a subtitle in your profile language.

---

## 🗂️ Files

| File | Role |
|------|------|
| `translator.py` | 🚀 CLI entry point — `translate`, `status`, `usage` commands |
| `subtitle_scanner.py` | 🔎 Scan Bazarr DB for missing subtitles; find best source SRT |
| `deepl_client.py` | 🌐 DeepL API wrapper — batched SRT cue translation |
| `gemini_client.py` | 🤖 Gemini API client — multi-key rotation, batched translation |
| `google_client.py` | 🔄 Google Translate fallback |
| `subtitle_quality_checker.py` | 🔍 Gemini-powered content quality checker (lang detection, quality scoring) |
| `srt_parser.py` | 📄 SRT parse/write with timing preservation |
| `db.py` | 🗄️ SQLite state DB — translation history + 24h cooldown |
| `config.py` | ⚙️ Config loader + language code mappings (ISO 639 ↔ provider codes) |
| `discord.py` | 💬 Discord webhook — translation summaries + quota warnings |
| `tests/` | ✅ pytest tests covering all modules |

---

## ✨ Features

- **🤖 Multi-provider** — Gemini 2.5 Pro (primary, auto-fallback to 2.5 Flash), DeepL Pro API (fallback), Google Translate (last resort)
- **🔄 Automatic failover** — Gemini (Pro → Flash) → DeepL → Google, with per-key rotation and per-model quota tracking
- **🧠 Smart source SRT selection** — picks the largest non-forced SRT; prefers English if it's within 20% of the largest
- **🔍 Bazarr profile integration** — reads your Bazarr language profile to know exactly which languages are needed per file
- **⏱️ 24-hour cooldown** — avoids re-translating the same (file, language) pair too soon after a failure
- **📦 Batched translation** — sends subtitle cues in ~3K char batches to stay within model limits
- **🏷️ Marker files** — `.deepl`/`.gemini` signals `auto-maintain` to defer muxing until translation is confirmed stable
- **🔔 Discord notifications** — green summary on success, red alert on quota exceeded
- **↔️ Two modes** — cron batch mode (`--since N`) and single-file import hook mode (`--file PATH`)

---

## 🔧 CLI Usage

```bash
# Run from the scripts directory
cd /config/berenstuff/automation/scripts
python3 -m translation.translator <command> [options]
```

### 🔄 `translate` — Run translations

```bash
# Cron mode: translate all files with missing subs modified in last 60 minutes
python3 -m translation.translator translate --since 60

# Import hook mode: translate a single just-imported file (runs in background)
python3 -m translation.translator translate --file "/path/to/Show.S01E01.mkv"

# Cap characters used in this run (budget control)
python3 -m translation.translator translate --since 60 --max-chars 50000
```

### 📊 `status` — Check recent translations

```bash
python3 -m translation.translator status
# Output:
#   Monthly usage: 42,318 / 500,000 chars (8.5%)
#
#   Recent translations (10):
#     2026-03-01 14:22:05 | success         | en->fr | 3,241 chars | Show.S02E05.mkv
#     2026-03-01 14:20:11 | no_source       | ?->es  |         0 chars | Movie.mkv
```

### 📈 `usage` — Live quota check from DeepL API

```bash
python3 -m translation.translator usage
# DeepL API usage: 42,318 / 500,000 chars (8.5%)
```

---

## 🏗️ Architecture

```
Import event (Sonarr/Radarr)
       │
       ▼
arr_profile_extract_on_import.sh
       │  (background, </dev/null &, disown)
       ▼
translator.py --file /path/to/file.mkv
       │
       ├──► subtitle_scanner.find_best_source_srt()   ← picks en.srt / largest
       ├──► subtitle_scanner.find_missing_langs_on_disk()  ← what's absent
       ├──► db.is_on_cooldown()                        ← 24h skip guard
       ├──► gemini/deepl/google translate_srt_cues()    ← batched API calls
       ├──► srt_parser.write_srt()                     ← output fr.srt
       ├──► touch fr.srt.{gemini,deepl}                ← defer auto-maintain mux
       ├──► db.record_translation()                    ← state + cooldown
       └──► bazarr scan-disk                           ← Bazarr picks up new SRT

Cron mode (every 30 min):
  translator.py --since 60
       │
       └──► subtitle_scanner.scan_recent_missing()     ← Bazarr DB query
            └── same pipeline above, for each file
```

### 🗄️ State Database

SQLite at `/APPBOX_DATA/storage/.translation-state/translation_state.db`

- Tracks every translation attempt: file path, source lang, target lang, chars used, status, timestamp
- Cooldown enforced per `(media_path, target_lang)` — prevents hammering on problem files
- Monthly character usage aggregated for quota warnings

### 🗺️ Language Mapping

Config maps ISO 639-1/2 codes to DeepL API identifiers:

```
en → EN-US      fr → FR      es → ES
de → DE         ja → JA      zh → ZH
pt → PT-PT      it → IT      ...
```

---

## 🧪 Tests

```bash
cd /config/berenstuff/automation/scripts
python3 -m pytest translation/tests/ -v
# 151 tests covering all modules
```

---

## ⚙️ Configuration

All secrets in `/config/berenstuff/.env`:

```bash
DEEPL_API_KEY=....:fx          # Free tier key (ends with :fx)
GEMINI_API_KEYS=AIza...,AIza...  # Comma-separated keys for rotation
BAZARR_URL=http://127.0.0.1:6767/bazarr
BAZARR_API_KEY=...
DISCORD_WEBHOOK_URL=...
# State dir defaults to /APPBOX_DATA/storage/.translation-state/

# Per-key budgets (default values = DeepL/Gemini free tier limits)
DEEPL_MONTHLY_BUDGET_PER_KEY=500000      # chars/mo per DeepL key
GEMINI_MONTHLY_BUDGET_PER_KEY=500000     # chars/mo per Gemini key
GEMINI_DAILY_REQUESTS_BUDGET_PER_KEY=9   # requests/day per Gemini key (~50 RPD free tier ÷ ~5 batches/file, −10% safety)
GOOGLE_MONTHLY_BUDGET=500000             # aggregate Google chars/mo (no per-key; unauthenticated)
```

Budget is tracked per API key via the `key_index` column in `translation_log`. A provider only falls through to the next when **all** its keys are budget-exhausted.

Legacy names `DEEPL_MONTHLY_BUDGET`, `GEMINI_MONTHLY_BUDGET`, and `GEMINI_DAILY_REQUESTS_BUDGET` (without `_PER_KEY`) are still honored but emit a deprecation warning in the log.

### 📅 Cron Schedule

```cron
# Every 30 minutes, with flock to prevent overlap
*/30 * * * * flock -n /tmp/deepl_translate.lock \
  python3 -m translation.translator translate --since 60 \
  >> /config/berenstuff/automation/logs/deepl_translate.log 2>&1
```

> **Budgets:** DeepL Pro 400K chars/month budget cap, Gemini 2.5 Pro 25 RPM / Flash 30 RPM (free tier, 1500 req/day per key × 13 keys). Google Translate is free (unofficial API, no guaranteed SLA). The `status` command shows current consumption.
