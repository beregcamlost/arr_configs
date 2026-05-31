# 🎬 Subtitle System

> A subtitle quality + lifecycle subsystem for MKV/MP4/M4V media — import-time
> sidecar extraction, ongoing quality maintenance (audit / strip / dedupe),
> Bazarr-profile compliance, and provider/translation recovery for missing
> languages. Bazarr-profile-aware, codec-smart, streaming-candidate-safe.

> ⚠️ **Subtitle standard is SIDECAR-ONLY (since 2026-05-30).**
> Subtitles are kept as external `.srt` sidecars, **not embedded** into the media container.
> Embedding forced Emby Web to run a slow on-the-fly ffmpeg extraction at playback
> (measured **14s–183s** on the 2-vCPU appbox, per track, first play); external sidecars load
> instantly. Concretely:
> - `subtitle_quality_manager.sh` defaults to **`SUB_EMBED=0`** (keep sidecars, never embed).
>   The `mux` command and the mux step inside `auto-maintain` are gated on this flag — with
>   the default they no-op and log `KEEP N sidecar(s) (embed disabled, SUB_EMBED=0)`.
>   Set `SUB_EMBED=1` or pass `--embed` to opt back into the **legacy** embed behavior.
> - `../arr_remux_on_import.sh` (the MKV→MP4 import remux hook, one directory up) **extracts**
>   embedded text subs to `.<lang>[.forced].srt` sidecars (no-clobber) and remuxes
>   video+audio only — subs are never muxed into the MP4.

---

## 🗂️ Files

This directory contains exactly three scripts plus this README. The import-time
extraction hook lives one directory **up** (`../arr_remux_on_import.sh`).

| File | Role |
|------|------|
| `subtitle_quality_manager.sh` | ⭐ Main tool — 7 subcommands: `audit` / `mux` / `strip` / `auto-maintain` / `enqueue` / `compliance` / `watermark` |
| `lib_subtitle_common.sh` | 📚 Shared library sourced by all scripts — language helpers, codec helpers, path classifiers, quality scoring, watermark stripping, Bazarr/Emby/Discord utilities |
| `library_subtitle_dedupe.sh` | 🗑️ Quality-deduplicate **external** `.srt` sidecars across the library (best-per-lang) |
| `../arr_remux_on_import.sh` | 📥 Sonarr/Radarr import hook (one dir up) — MKV→MP4 remux that extracts embedded text subs to `.srt` sidecars |

> There is **no** `arr_profile_extract_on_import.sh` or `bazarr_subtitle_recovery.sh`. The
> recovery/upgrade logic that used to be described as a separate script now lives **inside**
> `auto-maintain` (Phase 3 provider cycle + the `sqm_needs_upgrade` state table). The one-time
> library **de-embed migration** is `../transcode/batch_extract_embedded.sh` (lives in the
> `transcode/` dir, not here) — it extracts embedded text subs to sidecars and strips them
> from the container across the whole library.

---

## ✨ Features

### 🔁 `auto-maintain` phase order (per file)

This is the real per-file pipeline run by `subtitle_quality_manager.sh auto-maintain`.
Under the default **`SUB_EMBED=0`** the mux/collision steps are skipped — external SRTs
are kept as sidecars, not embedded.

```
Pre-flight ── load streaming candidates · load_guard · drain pending queue
              · cleanup orphaned *.striptmp/.bloattmp/.subtmp/.collisiontmp (>60 min)
              · (full mode only) run_upgrade_retries from sqm_needs_upgrade

Per-file safety skips ── converter running · playback in progress · streaming candidate
                         · (full mode) unchanged-since-last-audit (MKV + SRT mtime check)

Phase 0    ── Extract + strip non-profile embedded tracks
              (only with --keep-profile-langs; non-profile TEXT subs → .srt, then strip)
Phase 0.5  ── enforce_one_per_lang — 1-best-per-(lang,forced) across embedded + external
Phase 0.75 ── Sync-drift validation of embedded profile-language tracks
Phase 1    ── Audit + conditionally mux external SRTs (GATED by SUB_EMBED — NO mux by default)
Phase 1.75 ── Translate from non-profile sources before they're cleaned up
Phase 1.5  ── Delete non-profile external SRTs once profile languages are satisfied
Phase 3    ── Provider cycle + translation for completely MISSING profile languages
Phase 2    ── Auto-strip BAD embedded tracks (always in full mode; quick mode only if a
              GOOD replacement was muxed this run)
Phase 3*   ── Emby refresh (per modified file) + deferred Bazarr scan-disk (deduped per dir)
```

### 🧠 Quality Scoring
- `score_subtitle()` rates each track GOOD / WARN / BAD from cue density (cues/hour),
  coverage (% of runtime), late first-cue, mojibake, watermarks, and sync drift.
- `subtitle_quality_score()` (in `lib_subtitle_common.sh`) produces the numeric best-of
  score used by dedupe and extraction (cues, text lines, char count, forced-aware coverage).
- Watermark detection (`galaxytv`, `yify`, `yts`, `opensubtitles`, `addic7ed`, `subscene`,
  `podnapisi`, etc.) → instant BAD. Patterns are stored in the state DB and manageable via
  the `watermark` subcommand.

### 🌐 Language Intelligence (from `lib_subtitle_common.sh`)
- `expand_lang_codes()` — accepts mixed 2-letter and 3-letter codes, expands to a match set
- `normalize_track_lang()` — pure-bash 3→2 letter ISO normalization (safe in hot loops)
- `lang_to_iso639_2()` — 2→3 letter for MP4/M4V metadata
- `detect_srt_language()` — detects `und` SRTs via a **3-method cascade**:
  langdetect (offline) → DeepL API → **googletrans / Google Translate API** (last-resort fallback)
- `resolve_bazarr_profile_langs()` — reads the actual Bazarr language profile from SQLite
- `lang_in_set()`, `get_audio_languages()`

### 🛡️ Safety Features
- Converter-conflict skip — files being transcoded by the codec manager are left alone
- Playback skip — files currently being played are never modified
- Streaming-candidate skip — files flagged by the streaming checker are never muxed/stripped
- `load_guard` — skips heavy work when system load exceeds threshold
- `</dev/null` on all ffmpeg/sqlite3 calls inside pipeline loops
- Orphaned temp-file cleanup at scan start (`.striptmp.*`, `.bloattmp.*`, `.subtmp.*`, `.collisiontmp.*` older than 60 min)
- `validate_streams_match()` — every rewrite is rejected if video/audio stream counts change
- `--since N` filter checks both MKV mtime AND SRT mtime so fresh imports without SRTs are caught
- `sqm_pending_work` enqueue/drain queue — files enqueued by dedupe survive the `--since` window
- Emby refresh + Bazarr scan-disk deduplicated per series/movie dir

### 📢 Discord Notifications
- Rich embeds with per-file breakdown and counters (muxed / stripped / extracted / cleaned / skipped)
- Exhausted-language alerts (profile language with no provider or translation source)
- Colors: green (success), orange (partial/warning), yellow (skip), blue (neutral)

---

## 🔧 CLI Usage

### `subtitle_quality_manager.sh` — Main Tool (7 subcommands)

```bash
SQM=/config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh

# audit — score all subtitle tracks (embedded + external) in a directory
$SQM audit --path "/APPBOX_DATA/storage/media/tv/Severance" --recursive

# mux — (LEGACY, OFF BY DEFAULT) embed good external SRTs into the container.
#        Requires SUB_EMBED=1 or --embed; otherwise it KEEPs sidecars and no-ops.
SUB_EMBED=1 $SQM mux --path "/APPBOX_DATA/storage/media/tv/Severance/Season 1" --dry-run
$SQM mux --embed --path "/APPBOX_DATA/storage/media/tv/Severance/Season 1"

# strip — remove specific language tracks
$SQM strip --path "/APPBOX_DATA/storage/media/tv/Show" \
     --track eng --recursive --dry-run

# strip — keep only certain languages, remove everything else
$SQM strip --path "/APPBOX_DATA/storage/media/tv/Show" \
     --keep-only en,fr,es --recursive

# auto-maintain — quick scan (only files with MKV/SRT changed in last N min)
$SQM auto-maintain --path-prefix /APPBOX_DATA/storage/media \
     --since 15 --keep-profile-langs

# auto-maintain — full scan (no --since; runs upgrade retries + Phase 2 on everything)
$SQM auto-maintain --path-prefix /APPBOX_DATA/storage/media \
     --keep-profile-langs

# enqueue — add file(s) to the pending work queue for the next auto-maintain run
$SQM enqueue /path/to/file.mkv [/path/to/file2.mkv ...]

# compliance — report subtitle compliance against Bazarr profiles
$SQM compliance --path-prefix /APPBOX_DATA/storage/media
$SQM compliance --path-prefix /APPBOX_DATA/storage/media --format json
$SQM compliance --path-prefix /APPBOX_DATA/storage/media --verbose   # include OK files

# watermark — manage watermark patterns used by quality scoring
$SQM watermark list
$SQM watermark add "myreleasegroup"
$SQM watermark remove "myreleasegroup"          # builtin patterns cannot be removed
$SQM watermark test /path/to/file.en.srt        # show which patterns match a file
```

> **`mux` is legacy.** Sidecars are the standard now, so by default `mux` (and the mux
> step inside `auto-maintain`) keep the `.srt` files in place and log
> `KEEP N sidecar(s) (embed disabled, SUB_EMBED=0)`. Pass `--embed` / set `SUB_EMBED=1`
> to opt into the old embedding behavior.

### `library_subtitle_dedupe.sh` — External Sidecar Deduplication

Quality-dedupes **external** `.srt` sidecars per media file: converts `ass`/`ssa`/`vtt`→`srt`,
strips watermark/font-tag cues, profile-filters, then keeps the single best scorer per
`(lang, forced)` group and renames it to a canonical name. State persists in SQLite so
unchanged files are skipped next run.

```bash
DEDUPE=/config/berenstuff/automation/scripts/subtitles/library_subtitle_dedupe.sh

# Quick scan — only directories with media/subs modified in last N min
$DEDUPE --since 10

# Full library scan
$DEDUPE

# Dry-run to preview convert/strip/rename/remove decisions
$DEDUPE --dry-run --since 10
```

---

## 🏗️ Architecture

```
Sonarr/Radarr import event
          │
          ▼
../arr_remux_on_import.sh   (MKV → MP4 remux hook, one dir up)
          │
          ├─ Safety gates: skip image/styled subs (PGS/DVDSUB/ASS/SSA), attachments, AV1
          ├─ Extract each embedded TEXT sub → .<lang>[.forced].srt sidecar (no-clobber)
          ├─ Remux video+audio only (-c:v copy -c:a copy +faststart) — subs NOT embedded
          └─ Trigger Sonarr/Radarr rescan + Emby Library/Refresh


Cron (quick every few min / full daily):
  subtitle_quality_manager.sh auto-maintain
          │
          ├─ load streaming candidates · load_guard · drain pending queue · cleanup tmp
          ├─ [Phase 0]    extract + strip non-profile embedded (with --keep-profile-langs)
          ├─ [Phase 0.5]  enforce_one_per_lang (1 best per lang/forced)
          ├─ [Phase 0.75] sync-drift validation of embedded profile tracks
          ├─ [Phase 1]    audit + mux external SRTs  (GATED by SUB_EMBED — NO mux default)
          ├─ [Phase 1.75] translate from non-profile sources before cleanup
          ├─ [Phase 1.5]  delete non-profile externals once profile satisfied
          ├─ [Phase 3]    provider cycle + translation for MISSING profile langs
          ├─ [Phase 2]    auto-strip BAD embedded tracks
          └─ [Phase 3*]   Emby refresh + deferred Bazarr scan-disk (deduped per dir)


Cron (quick + full):
  library_subtitle_dedupe.sh
          │
          ├─ convert ass/ssa/vtt → srt
          ├─ strip watermark/font-tag cues
          ├─ profile-filter, then keep 1 best .srt per (lang, forced) → canonical name
          ├─ persist per-file state (SQLite: pipeline.db)
          ├─ Bazarr scan-disk + Emby refresh on changes
          └─ enqueue changed files into the quality-manager pending queue
```

### 🗄️ Shared Library (`lib_subtitle_common.sh`)

Sourced by `subtitle_quality_manager.sh`, `library_subtitle_dedupe.sh`, and
`../arr_remux_on_import.sh`. Never execute directly. The table below is a
**non-exhaustive selection** of the helpers the subsystem relies on.

| Helper | Purpose |
|--------|---------|
| `is_tv_path()` / `is_movie_path()` | Library path classification |
| `expand_lang_codes()` | Accept 2-letter or 3-letter language codes, expand to match set |
| `normalize_track_lang()` | Fast ISO 639-2→1 normalization (pure bash) |
| `lang_to_iso639_2()` | 2→3 letter for MP4/M4V metadata |
| `detect_srt_language()` | Identify SRT language (langdetect → DeepL → Google Translate) |
| `resolve_bazarr_profile_langs()` | Read the language profile from the Bazarr DB |
| `get_audio_languages()` | Extract audio track languages from ffprobe JSON |
| `subtitle_quality_score()` | Numeric best-of score (cues/coverage/forced-aware) |
| `convert_subtitle_to_srt()` | Convert ass/ssa/vtt → srt via ffmpeg |
| `strip_srt_watermarks()` | Remove watermark cue blocks + `<font>` tags from an SRT |
| `enforce_one_per_lang()` | Keep 1 best subtitle per (lang, forced) across embedded + external |
| `strip_all_embedded_subs()` | Strip every embedded subtitle stream via ffmpeg |
| `validate_streams_match()` | Reject rewrites that drop video/audio streams |
| `bazarr_rescan_for_file()` | Trigger Bazarr scan-disk for a file's series/movie |
| `curl_with_retry()` / `load_guard()` | HTTP retry helper / system-load gate |
| `upsert_/resolve_/drain_/touch_*_sqm_needs_upgrade()` | needs-upgrade state-DB helpers (recovery) |

---

## 📅 Cron Schedule

> ⚠️ **Illustrative.** No crontab lives alongside these scripts; the actual scheduler is
> the source of truth. These scripts take `--since` as an arbitrary integer and have no
> built-in schedule. In the live setup the quality/dedupe/translation jobs are typically
> driven by the `media_pipeline.sh` orchestrator (a quick lane and a slow lane), with a
> separate daily full `auto-maintain`.

| Cadence (example) | Command | Purpose |
|-------------------|---------|---------|
| quick (slow lane) | `library_subtitle_dedupe.sh --since 10` | Quick external-sidecar dedupe |
| full (weekly/daily) | `library_subtitle_dedupe.sh` | Full library dedupe |
| quick (slow lane) | `auto-maintain --since 15 --keep-profile-langs` | Quick maintenance scan |
| daily 1 AM | `auto-maintain --keep-profile-langs` (full) | Full nightly maintenance + upgrade retries |

> The EN→ES subtitle translator (`../translation/translator.py`) is a **separate subsystem**
> invoked by the orchestrator's slow lane — not part of this directory.

---

## ⚙️ Configuration

All secrets in `/config/berenstuff/.env`:

```bash
BAZARR_API_KEY=...
BAZARR_URL=http://127.0.0.1:6767/bazarr
EMBY_URL=http://...
EMBY_API_KEY=...
DISCORD_WEBHOOK_URL=...
RADARR_KEY=...
SONARR_KEY=...
DEEPL_API_KEY=...        # optional — only for detect_srt_language() fallback

# Subtitle standard: SIDECAR-ONLY. Leave unset (defaults to 0).
# SUB_EMBED=1            # legacy: re-enable embedding subs into the container
```

**Databases & state:**
- Bazarr DB: `/opt/bazarr/data/db/bazarr.db`
- Quality-manager state DB: `/APPBOX_DATA/storage/.subtitle-quality-state/subtitle_quality_state.db`
  (holds `sqm_file_audits`, `sqm_pending_work`, `sqm_needs_upgrade`, `sqm_watermark_patterns`, etc.)
- Dedupe state DB: `/APPBOX_DATA/storage/pipeline.db` (override via `PIPELINE_DB` or `--db-path`).
  The `.subtitle-dedupe-state/` directory holds only the `flock` lock file, **not** the DB.
