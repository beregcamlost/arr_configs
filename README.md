```
╔══════════════════════════════════════════════════════════════════════════════════════════╗
║                                                                                          ║
║   ██████╗ ███████╗██████╗ ███████╗███╗   ██╗███████╗████████╗██╗   ██╗███████╗███████╗   ║
║   ██╔══██╗██╔════╝██╔══██╗██╔════╝████╗  ██║██╔════╝╚══██╔══╝██║   ██║██╔════╝██╔════╝   ║
║   ██████╔╝█████╗  ██████╔╝█████╗  ██╔██╗ ██║███████╗   ██║   ██║   ██║█████╗  █████╗     ║
║   ██╔══██╗██╔══╝  ██╔══██╗██╔══╝  ██║╚██╗██║╚════██║   ██║   ██║   ██║██╔══╝  ██╔══╝     ║
║   ██████╔╝███████╗██║  ██║███████╗██║ ╚████║███████║   ██║   ╚██████╔╝██║     ██║        ║
║   ╚═════╝ ╚══════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚══════╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝        ║
║                                                                                          ║
║                    🎬  M E D I A  A U T O M A T I O N  S U I T E  🎬                     ║
║                 Sonarr · Radarr · Bazarr · Emby · DeepL · TMDB · Discord                 ║
║                                                                                          ║
╚══════════════════════════════════════════════════════════════════════════════════════════╝
```

<div align="center">

[![Private Repo](https://img.shields.io/badge/visibility-private-red?style=for-the-badge&logo=github&logoColor=white)](https://github.com/beregcamlost/arr_configs)
[![Platform](https://img.shields.io/badge/platform-appbox-blueviolet?style=for-the-badge&logo=linux&logoColor=white)](https://github.com/beregcamlost/arr_configs)
[![Shell](https://img.shields.io/badge/shell-bash%205%2B-4EAA25?style=for-the-badge&logo=gnubash&logoColor=white)](https://www.gnu.org/software/bash/)
[![Python](https://img.shields.io/badge/python-3.x-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![SQLite](https://img.shields.io/badge/state-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Discord](https://img.shields.io/badge/notifications-Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com/)

[![Sonarr](https://img.shields.io/badge/Sonarr-✓-35C5F4?style=flat-square&logo=sonarr&logoColor=white)](https://sonarr.tv)
[![Radarr](https://img.shields.io/badge/Radarr-✓-FFC230?style=flat-square)](https://radarr.video)
[![Bazarr](https://img.shields.io/badge/Bazarr-✓-00b8ff?style=flat-square)](https://www.bazarr.media)
[![Emby](https://img.shields.io/badge/Emby-✓-52B54B?style=flat-square&logo=emby&logoColor=white)](https://emby.media)
[![Prowlarr](https://img.shields.io/badge/Prowlarr-✓-orange?style=flat-square)](https://prowlarr.com)
[![DeepL](https://img.shields.io/badge/DeepL-✓-0F2B46?style=flat-square&logo=deepl&logoColor=white)](https://www.deepl.com)
[![TMDB](https://img.shields.io/badge/TMDB-✓-01D277?style=flat-square&logo=themoviedatabase&logoColor=white)](https://www.themoviedatabase.org)
[![Transmission](https://img.shields.io/badge/Transmission-✓-C00?style=flat-square)](https://transmissionbt.com)

[![Cron Jobs](https://img.shields.io/badge/cron%20jobs-~14%20active-success?style=flat-square&logo=clockify&logoColor=white)]()
[![Tests](https://img.shields.io/badge/tests-528%20passing-brightgreen?style=flat-square&logo=pytest&logoColor=white)]()

</div>

---

> **🏠 Personal media server automation stack** — a battle-tested suite of shell scripts and Python services that automate subtitle management, codec normalization, streaming availability tracking, and library maintenance across a full \*arr ecosystem running on an appbox.

---

## 📋 Table of Contents

- [🗺️ Architecture Overview](#-architecture-overview)
- [⚡ Systems at a Glance](#-systems-at-a-glance)
- [🎯 Media Pipeline Orchestrator](#-media-pipeline-orchestrator)
- [🎬 Subtitle System](#-subtitle-system)
- [🔄 Codec Manager](#-codec-manager)
- [📺 Streaming Checker](#-streaming-checker)
- [🛡️ Deletion Safety Guards](#-deletion-safety-guards)
- [🌐 Translation System](#-translation-system)
- [👻 Emby Zombie Reaper](#-emby-zombie-reaper)
- [🧹 Arr Cleanup](#-arr-cleanup)
- [🚫 Grab Monitor](#-grab-monitor)
- [⏰ Cron Schedule](#-cron-schedule)
- [🔌 Integration Flow](#-integration-flow)
- [📁 Repository Structure](#-repository-structure)
- [📚 Shared Libraries](#-shared-libraries)
- [🛠️ Tech Stack](#-tech-stack)
- [⚙️ Configuration](#-configuration)
- [📖 Operational Lessons](#-operational-lessons)

---

## 🗺️ Architecture Overview

```mermaid
graph TB
    subgraph INGEST["📥 Ingest Layer"]
        SR[🔵 Sonarr]
        RR[🟡 Radarr]
        TR[🔴 Transmission]
    end

    subgraph HOOK["🪝 Import Hook"]
        WH[arr_profile_extract_on_import.sh]
    end

    subgraph SUBTITLE["🎬 Subtitle Pipeline"]
        PH0["Phase 0 — Extract & Strip\nembedded tracks → external SRTs"]
        PH1["Phase 1 — Collision Detection\npre-mux conflict resolution"]
        PH15["Phase 1.5 — Source Cleanup\nnon-profile SRTs deleted"]
        PH2["Phase 2 — Dedup & Score\nGOOD-GOOD collapse by quality"]
        BZ[🔵 Bazarr\nSearch + Rescan]
        DL[🌐 DeepL\nTranslation Fallback]
    end

    subgraph CODEC["🔄 Codec Pipeline"]
        CQ["Priority Queue\nimport→audio→video"]
        CM[library_codec_manager.sh]
        FF[⚙️ ffmpeg\nH.264 CRF19 + AAC 192k]
    end

    subgraph STREAM["📺 Streaming"]
        SC[streaming_checker.py]
        TMDB[🎬 TMDB API]
        MOTN[🌙 Movie of the Night API]
        WM[📊 Watchmode API]
        JW[🎯 JustWatch GraphQL]
        SKP["Skip Candidate\n(no transcode)"]
    end

    subgraph NOTIFY["🔔 Notifications"]
        DC[Discord Webhooks]
    end

    subgraph MAINT["🔧 Maintenance"]
        ZR[👻 Zombie Reaper]
        AC[🧹 Arr Cleanup]
        EM[📺 Emby]
    end

    TR --> SR
    TR --> RR
    SR --> WH
    RR --> WH
    WH --> PH0
    WH --> CQ
    WH --> SKP
    PH0 --> PH1
    PH1 --> PH15
    PH15 --> PH2
    PH2 --> BZ
    BZ --> DL
    SC --> TMDB
    SC --> MOTN
    SC --> WM
    SC --> JW
    SC --> SKP
    SKP --> CQ
    CQ --> CM
    CM --> FF
    FF --> EM
    DL --> DC
    CM --> DC
    SC --> DC
    ZR --> EM
    AC --> SR
    AC --> RR
    AC --> TR

    classDef ingest fill:#1a3a5c,stroke:#35C5F4,color:#fff
    classDef hook fill:#3a1a5c,stroke:#a855f7,color:#fff
    classDef subtitle fill:#1a3a1a,stroke:#22c55e,color:#fff
    classDef codec fill:#3a2a1a,stroke:#f59e0b,color:#fff
    classDef stream fill:#1a2a3a,stroke:#06b6d4,color:#fff
    classDef notify fill:#3a1a1a,stroke:#ef4444,color:#fff
    classDef maint fill:#2a2a2a,stroke:#6b7280,color:#fff

    class SR,RR,TR ingest
    class WH hook
    class PH0,PH1,PH15,PH2,BZ,DL subtitle
    class CQ,CM,FF codec
    class SC,TMDB,MOTN,WM,JW,SKP stream
    class DC notify
    class ZR,AC,EM maint
```

---

## ⚡ Systems at a Glance

| System | Language | Tests | Cron Freq | Notifications |
|--------|----------|-------|-----------|---------------|
| 🎯 Pipeline Orchestrator | Bash | — | Every 5 min | — |
| 🎬 Subtitle Manager | Bash | 159 ✅ | via pipeline / daily | ✅ Discord |
| 🔄 Codec Manager | Bash | — | via pipeline / 3 AM daily | ✅ Discord |
| 📺 Streaming Checker | Python | 271 ✅ | Weekly / Monthly | ✅ Discord |
| 🌐 Translation System | Python | 151 ✅ | via pipeline | ✅ Discord |
| 👻 Zombie Reaper | Bash | — | via pipeline | ✅ Discord |
| 🧹 Arr Cleanup | Bash | — | via pipeline | — |
| 🚫 Grab Monitor | Bash | — | via pipeline | ✅ Discord |
| 📈 Trending Auto-Add | Python | 21 ✅ | Weekly (DISABLED) | — |
| 💾 SQLite Backup | Bash | — | Every 3 days | — |

<details>
<summary>📊 <strong>Key Numbers</strong></summary>
<br>

| Metric | Value |
|--------|-------|
| 📜 Total cron jobs | ~14 |
| 🧪 Total tests | 528 passing |
| 🌐 Translation budget | Gemini primary (13 free keys), DeepL 400K/month fallback, Google last resort |
| 💾 State databases | 6 (codec, streaming, translation, subtitle-quality, bazarr, grab-monitor) |
| 🔌 External APIs | 9 (Sonarr, Radarr, Bazarr, Emby, TMDB, DeepL, MoTN, Watchmode, JustWatch) |
| 📡 Discord webhooks | All systems |
| 🎞️ Target codec | H.264 CRF19 + AAC 192k stereo |
| ⏭️ Skips | UHD / 4K / HDR / streaming candidates / stale candidates |

</details>

---

## 🎯 Media Pipeline Orchestrator

> **`automation/scripts/media_pipeline.sh`** — A single cron entry that replaced 10 high-frequency independent jobs. Runs every 5 minutes and coordinates all short-cycle subsystems through a shared SQLite `pipeline_state` table, eliminating lock contention and silent skips.

### 🔧 Why It Exists

Before the orchestrator, each subsystem ran its own cron line with its own `flock` file. This caused:

- **Lock contention** — subtitle dedupe and auto-maintain sharing one lock; codec resume and grab-monitor each grabbing their own; invocations piling up every few minutes
- **No coordination** — codec resume could start while subtitle mux was mid-write on the same MKV
- **Silent skips** — `flock -n` exits 1 with no output; heavy minutes had 4-5 jobs silently no-op
- **No priority** — audio-only remuxes queued behind slow video transcodes; grab monitor ran on the same cadence regardless

### ⚙️ Subsystems Coordinated

| # | Subsystem | Step Role | Timeout |
|---|-----------|-----------|---------|
| 1 | 🚫 Grab Monitor | Quick job | 60s |
| 2 | 🧹 Arr Cleanup | Quick job | 60s |
| 3 | 👻 Zombie Reaper | Quick job | 60s |
| 4 | 🎬 Subtitle Dedupe quick | Subtitle pipeline | 180s |
| 5 | 🎬 Subtitle Auto-maintain quick | Subtitle pipeline | 180s |
| 6 | 🎬 Bazarr Subtitle Recovery | Subtitle pipeline | 180s |
| 7 | 🌐 Translation | Translation step | 300s |
| 8 | 🔄 Codec Resume | Codec step | 600s |

### 🗄️ DB Coordination

A `pipeline_state` table in the codec SQLite database tracks which step is active, when it started, and its last heartbeat. Steps check this table before acquiring work — if another step is touching the same MKV, they yield rather than race.

```sql
-- pipeline_state table (in codec DB)
step_name TEXT PRIMARY KEY,
pid       INTEGER,
started   INTEGER,   -- Unix epoch
heartbeat INTEGER,
status    TEXT       -- 'running' | 'done' | 'timeout'
```

### 🔢 Priority Ordering

```
Priority 0  →  Quick jobs (grab-monitor, arr-cleanup, zombie-reaper)   fast, no media I/O
Priority 1  →  Audio-only remux in codec queue                         no quality loss, sub-second per file
Priority 10 →  Video transcode                                         slow, CPU intensive
Priority 99 →  Ineligible / skipped                                    never dequeued
```

Codec `resume` always drains all priority-1 items before touching priority-10, so audio fixes never wait behind a multi-hour transcode.

### 🔀 Execution Flow

```mermaid
flowchart TD
    CRON(["⏰ cron\n*/5 * * * *"]) --> PL

    subgraph PL["🎯 media_pipeline.sh"]
        QK["⚡ Quick Jobs\n(grab-monitor · arr-cleanup · zombie-reaper)\n60s timeout each"]
        SUB["🎬 Subtitle Pipeline\n(dedupe · auto-maintain · bazarr-recovery)\n180s timeout each"]
        TR["🌐 Translation\n300s timeout"]
        CC["🔄 Codec Resume\n600s timeout"]

        QK --> SUB --> TR --> CC
    end

    DB[("🗄️ pipeline_state\n(codec DB)")]

    QK <-->|"heartbeat\n+ yield"| DB
    SUB <-->|"heartbeat\n+ yield"| DB
    TR <-->|"heartbeat\n+ yield"| DB
    CC <-->|"heartbeat\n+ yield"| DB

    CC --> EM["📺 Emby rescan"]
    CC --> DC["🔔 Discord"]
    SUB --> BZ["🔵 Bazarr rescan"]
    TR --> DC

    style QK fill:#1a3a1a,stroke:#22c55e,color:#fff
    style SUB fill:#1a2a3a,stroke:#06b6d4,color:#fff
    style TR fill:#2a1a3a,stroke:#818cf8,color:#fff
    style CC fill:#3a2a1a,stroke:#f59e0b,color:#fff
    style DB fill:#1a1a3a,stroke:#818cf8,color:#fff
```

---

## 🎬 Subtitle System

> **`automation/scripts/subtitles/`** — The crown jewel. A multi-phase subtitle pipeline that extracts, cleans, deduplicates, and manages subtitles across an entire media library.

### 📂 Key Files

| File | Purpose |
|------|---------|
| `subtitle_quality_manager.sh` | Main entry point — `audit`, `mux`, `strip`, `auto-maintain`, `compliance` subcommands |
| `lib_subtitle_common.sh` | Shared library — language helpers, codec helpers, path classifiers |
| `arr_profile_extract_on_import.sh` | Unified Sonarr + Radarr import hook |
| `library_subtitle_dedupe.sh` | Removes duplicate/low-quality external subs |
| `bazarr_subtitle_recovery.sh` | Retries missing subtitle downloads with translation fallback; Stage 4 (searches exhausted) sends a Discord "⚠️ Stuck Alerts" notification instead of deleting and re-grabbing |

### 🔀 Multi-Phase Pipeline

```mermaid
flowchart LR
    A([📁 New Import]) --> B

    subgraph P0["🔧 Phase 0 — Extract & Strip"]
        B["Extract embedded\ntext tracks → .srt"]
        B2["Strip non-profile\nembedded tracks"]
    end

    subgraph P05["⚖️ Phase 0.5 — Enforce 1-Best"]
        B3["enforce_one_per_lang:\nscore all sources per lang\nkeep best, strip/delete losers"]
    end

    subgraph P1["🛡️ Phase 1 — Collision + Quality"]
        C["Pre-mux collision check\nWARN → mark for upgrade\nBAD → delete + provider cycle"]
    end

    subgraph P175["🌐 Phase 1.75 — Translate Sources"]
        C2["Translate from non-profile\nSRTs before cleanup"]
    end

    subgraph P15["🗑️ Phase 1.5 — Source Cleanup"]
        D["Non-profile SRTs deleted\nonce all langs satisfied"]
    end

    subgraph P2["🏆 Phase 2 — Dedup & Score"]
        E["GOOD-GOOD duplicates\ncollapsed by quality score\nkeeps highest scorer per lang"]
    end

    F([✅ Clean MKV\n+ Quality Subs])

    B --> B2 --> B3 --> C --> C2 --> D --> E --> F

    style P0 fill:#1a3a1a,stroke:#22c55e,color:#fff
    style P05 fill:#2a3a1a,stroke:#84cc16,color:#fff
    style P1 fill:#3a2a1a,stroke:#f59e0b,color:#fff
    style P175 fill:#1a2a3a,stroke:#818cf8,color:#fff
    style P15 fill:#3a1a1a,stroke:#ef4444,color:#fff
    style P2 fill:#1a2a3a,stroke:#06b6d4,color:#fff
```

### ✨ Features

- 🔍 **Language detection** — Python `langdetect` (offline, fast) → DeepL API → Google Translate fallback; renames `und` tracks to actual language
- 🏷️ **Quality scoring** — `subtitle_quality_score()` ranks all sources; `enforce_one_per_lang()` keeps 1-best per language
- ⚠️ **WARN/BAD handling** — WARN subs kept but marked for upgrade; BAD subs deleted immediately with provider/translation cycle
- 🔄 **Upgrade retries** — `needs_upgrade` SQLite table tracks WARN/MISSING subs for daily retry via providers + DeepL
- 📊 **Compliance reporting** — `compliance` subcommand audits entire library against Bazarr profiles (text + JSON output)
- 💧 **Watermark stripping** — removes common watermark lines from SRT files
- 🛡️ **Remux integrity validation** — `validate_streams_match()` verifies video + audio stream counts survive every strip/mux operation; rejects output and keeps original intact on any mismatch
- 🎭 **HI/SDH/CC awareness** — `subtitle_group_key()` looks past hearing-impaired qualifiers for dedup grouping
- 🌊 **Streaming candidate skip** — pre-loaded associative array pattern; zero subprocess calls in hot loops
- 🔄 **Bazarr integration** — `bazarr_rescan_for_file()` + per-language subtitle search after every operation
- 📢 **Rich Discord embeds** — file list, DeepL deferral tracking, per-operation summaries

### ⏱️ Scan Schedule

```
Every  5 min  → subtitle dedupe quick scan      (--since 10 min, via pipeline)
Every  5 min  → subtitle auto-maintain quick    (--since 15 min, via pipeline)
Every  5 min  → bazarr subtitle recovery        (--since 30 min, via pipeline)
Daily  1 AM   → subtitle auto-maintain full     (entire library, independent cron)
Weekly Sun 4AM→ subtitle dedupe full            (entire library, independent cron)
```

> 💡 **`--since` logic**: checks both MKV mtime AND SRT mtime (OR logic), so new imports without SRTs are always caught by quick scans.

---

## 🔄 Codec Manager

> **`automation/scripts/transcode/`** — SQLite-backed incremental audit and conversion pipeline. Targets H.264 + AAC normalization while skipping content that shouldn't be touched.

### 📂 Key Files

| File | Purpose |
|------|---------|
| `library_codec_manager.sh` | Main entry — `audit`, `plan`, `convert`, `resume`, `enqueue-import` subcommands |

### 🎯 Conversion Targets

```
Video   →  H.264  CRF 19  (x264)
Audio   →  AAC    192k    stereo normalization
Container → MKV or MP4 (preserved); others remuxed to MKV
```

### ⚡ Priority Queue

```mermaid
graph LR
    A["🚀 enqueue-import\npriority = 0\n(immediate)"] --> Q
    B["🔊 Audio-only remux\npriority = 1\n(fast, no quality loss)"] --> Q
    C["🎞️ Video transcode\npriority = 10\n(slow, CPU intensive)"] --> Q
    D["⏭️ Ineligible\npriority = 99\n(skipped)"] --> Q
    Q[(📋 SQLite\nconversion_plan)]

    style A fill:#1a3a1a,stroke:#22c55e,color:#fff
    style B fill:#2a2a1a,stroke:#f59e0b,color:#fff
    style C fill:#3a1a1a,stroke:#ef4444,color:#fff
    style D fill:#2a2a2a,stroke:#6b7280,color:#fff
    style Q fill:#1a1a3a,stroke:#818cf8,color:#fff
```

### 🚫 Skip Conditions

| Condition | Reason |
|-----------|--------|
| UHD / 4K / HDR | Quality preservation |
| Streaming candidates | No point converting content we might delete |
| Stale candidates | Flagged 90d+ unwatched + on streaming (tier 1.5) |
| Already H.264 + AAC | Nothing to do |
| Currently being converted | Concurrency guard |
| Micro-encode (1080p MKV, already H.264 + AAC compliant) | Already-degraded source with no codec/container/audio fix needed; only skipped when fully compliant. Logged to `$STATE_DIR/micro_encodes.csv` + Discord alert |

### 🔬 Micro-Encode Detection

Files that still need codec/container/audio fixes are always converted regardless of BPF. Detection only causes a skip when the file is already fully compliant (`already_compliant`).

| BPF Range | Severity | Discord Alert |
|-----------|----------|---------------|
| < 0.02 | SEVERE — likely corrupt or near-unwatchable source | 🔴 Red alert |
| 0.02 – 0.05 | Low bitrate — noticeable compression artifacts | 🟡 Yellow alert |
| 0.05 – 0.08 | Borderline — marginal quality | ℹ️ Info only |

> **BPF** = Bits Per Frame = `(file_size_bits) / (fps × duration_seconds)`. Calculated at audit time; stored in the codec DB alongside the conversion plan.

### 📊 Post-Swap Actions

After every successful transcode:
1. ✅ Sonarr/Radarr rescan
2. ✅ Bazarr `scan-disk`
3. ✅ Direct Bazarr DB `audio_language` update (Sonarr `languages` field is immutable after import)
4. ✅ Emby library refresh

---

## 📺 Streaming Checker

> **`automation/scripts/streaming/`** — Python CLI that cross-references the local media library against real-time streaming provider availability. Flags content that's freely available so you can reclaim disk space.

### 🔌 APIs Used (4-Source Voting)

| API | Purpose | Auth |
|-----|---------|------|
| 🎬 TMDB | Primary streaming availability (always votes) | API key |
| 🌙 Movie of the Night (RapidAPI) | Cross-validation voter + per-season availability | API key |
| 📊 Watchmode | Cross-validation voter | API key |
| 🎯 JustWatch (GraphQL) | Cross-validation voter (TMDB's upstream source) | None needed |
| 📺 Emby Activity Log | Last-played timestamps for staleness detection | API key |

> **Default providers:** Netflix, Disney+, Crunchyroll (CL region)

### 📟 CLI Subcommands

```bash
streaming scan              # Refresh availability from APIs
streaming report            # Show what's available on streaming
streaming confirm-delete    # Mark items for deletion
streaming check-seasons     # Per-season streaming breakdown
streaming stale-flag        # Tier 1.5: flag 90d+ unwatched items on streaming
streaming stale-delete      # Tier 1.5: delete flagged items after 15d grace
streaming stale-cleanup     # Tier 2: yearly stale cleanup
streaming summary           # Per-provider and per-library stats
streaming providers         # List known providers
```

### 🏷️ Keep-Local Logic

```mermaid
flowchart TD
    A([📦 Item in Library]) --> B{Has\nkeep-local tag?}
    B -->|Yes| C["Touch in DB\nclear left_at\nupdate last_seen"]
    C --> D([✅ Excluded from\nreport & left-streaming])
    B -->|No| E{Available on\nstreaming?}
    E -->|Yes| F([🚨 Flag for\npotential deletion])
    E -->|No| G([✅ Keep — not\navailable elsewhere])

    style C fill:#1a3a1a,stroke:#22c55e,color:#fff
    style D fill:#1a3a1a,stroke:#22c55e,color:#fff
    style F fill:#3a1a1a,stroke:#ef4444,color:#fff
    style G fill:#1a2a3a,stroke:#06b6d4,color:#fff
```

### 🧪 Test Coverage

```
271 tests passing
├── 96  streaming core tests
├── 33  keep-local filtering tests
├── 27  cross-validation voting tests (4-source model)
├── 21  trending auto-add tests
├── 20  JustWatch client tests
├── 15  per-season streaming tests
├── 14  left-streaming tracking tests
├── 12  Discord notification tests
├── 10  stale flag/delete tests
├──  8  CLI argument tests
├──  5  Crunchyroll addon detection tests
└── 10  miscellaneous utility tests
```

---

## 🛡️ Deletion Safety Guards

> Multiple layers of protection against accidental data loss — applied at every point where files or library entries can be permanently removed.

### 📺 Streaming Checker

| Guard | Protection |
|-------|-----------|
| `stale-cleanup` | Aborts entirely if the keep-local set is empty — indicates Radarr/Sonarr are unreachable; refuses to delete anything without a valid exclusion list |
| `stale-delete` | Same keep-local empty-set abort before any deletion |

### 🔄 Codec Manager

| Guard | Protection |
|-------|-----------|
| `prune-backups` | Verifies the converted file exists (non-zero size, readable) before deleting the `.bak` backup; refuses to delete backup if converted file is missing or suspect |
| MKV in-place rewrites | Every swap writes an audit record with pre/post size, mtime, and inode — lets you verify or roll back after the fact |
| Work directory cleanup | Stale intermediate files in the codec work directory (interrupted conversions) are auto-removed after 24 hours, preventing accumulation without touching live media |

---

## 🌐 Translation System

> **`automation/scripts/translation/`** — Automatic subtitle translation for missing profile languages using Gemini, DeepL, and Google Translate. Bridges the gap when Bazarr can't find subtitles in the required language.

### 🧠 Smart Source Selection

```
1. Gather all non-forced external SRTs for the file
2. Pick the largest one (most content)
3. If an English SRT exists and is within 20% of the largest → prefer English
4. Translate → target language SRT
5. 24h cooldown per (file, language) pair to avoid hammering the API
```

### 🔀 Two Entry Points

```
pipeline every 5 min (translation step)
  └── translate --since 60
        queries Bazarr DB for missing_subtitles
        translates in batch (300s timeout)

Import Hook (background, async)
  └── translate --file /path/to/media.mkv
        triggered immediately after import
        runs detached (</dev/null & disown)
```

### 📊 Budget & Quota Management

| Metric | Value |
|--------|-------|
| Primary | Gemini 2.5 Pro/Flash (13 free API keys, 1500 req/day each) |
| Fallback | DeepL Pro API (400K chars/month budget cap) |
| Last resort | Google Translate (free, unofficial API) |
| Cooldown | 24h per (file, language) |
| Discord alert | Quota warning webhook on low balance |
| Override | `--monthly-budget 0` for unlimited (manual only) |

### 🧪 Test Coverage

```
151 tests passing
├── Provider fallback chain (Gemini → DeepL → Google)
├── CLI argument handling
├── Source SRT selection logic
├── State DB cooldown enforcement
├── Bazarr DB profile + missing_subtitles parsing
├── Gemini multi-key rotation + model fallback
├── Google Translate error recovery
└── Language code mapping validation
```

---

## 👻 Emby Zombie Reaper

> **`emby_zombie_reaper.sh`** — Hunts down and terminates idle or paused Emby sessions that have overstayed their welcome.

### ⚙️ Behavior

| Trigger | Action |
|---------|--------|
| Session idle > 5 hours | 💀 Kill session |
| 20 zombie kills accumulated | 🔄 Auto-restart Emby |
| Every Tuesday 05:03 UTC | 🔄 Weekly safety restart (independent cron) |

### 🔍 State Tracking

- Runs as a quick-job step inside the pipeline every 5 minutes (60s timeout)
- Tracks kill counts across invocations in state file
- Sends Discord notification on auto-restart

---

## 🧹 Arr Cleanup

> **`arr_cleanup_importblocked.sh`** — Evicts stale "already imported" queue entries from Sonarr and Radarr, and removes the corresponding torrents from Transmission.

### ⚙️ What It Does

```
Every 5 minutes (via pipeline, quick-jobs step):
  1. Query Sonarr + Radarr for queue items in "importBlocked" status
  2. Blocklist queue items with executable extensions (.exe, .msi, etc.)
  3. Remove matching entries from arr queues
  4. Delete corresponding torrents from Transmission
  5. Free up queue space for new downloads
```

---

## 🚫 Grab Monitor

> **`automation/scripts/grab-monitor.sh`** — Enforces a language policy at download time. Monitors recent grabs from Sonarr and Radarr and removes any download whose detected audio languages fall outside the allowed set.

### 🎯 Allowed Languages Per Item

```
{Original language of the series/movie} ∪ {English} ∪ {Spanish} ∪ {Spanish Latino}
```

- **Attack on Titan** (Japanese-origin): Japanese + English + Spanish → allowed ✅
- **Amélie** (French-origin): French + English → allowed ✅
- **Rooster** (English-origin): French + English MULTI → **blocked** ✅

### ⚙️ How It Works

```
Every 5 minutes (via pipeline, quick-jobs step):
  1. Query Sonarr + Radarr history for grabs in the last 5 minutes
  2. For each grab: look up the series/movie original language via API
  3. Build allowed set: {originalLang, English, Spanish, Spanish Latino}
  4. If any parsed language is outside the allowed set:
     → Remove torrent from Transmission
     → Discord notification with violation details
     → Log action to state dir
  5. Mark all processed grabs as seen (SQLite) to avoid reprocessing
```

### 📂 Key Files

| File | Purpose |
|------|---------|
| `automation/scripts/grab-monitor.sh` | Canonical script |
| `scripts/grab-monitor.sh` | Compat copy (used by cron) |

### 📋 State

- **State DB**: `/APPBOX_DATA/storage/.grab-monitor-state/seen.db`
- **Log**: `/APPBOX_DATA/storage/.grab-monitor-state/grab-monitor.log`

---

## ⏰ Cron Schedule

> The 26-line schedule was consolidated to ~14 lines. Ten high-frequency jobs now run inside the unified pipeline. Low-frequency jobs remain as independent entries.

### 🎯 Unified Pipeline (replaces 10 separate cron lines)

```
*/5 * * * *   media_pipeline.sh
```

Coordinates inside each 5-minute window: grab-monitor → arr-cleanup → zombie-reaper → subtitle dedupe → subtitle auto-maintain → bazarr recovery → translation → codec resume.

### 📅 Low-Frequency Independent Jobs

| Schedule | Job | System | Notes |
|----------|-----|--------|-------|
| `0 1 * * *` | 🎬 Subtitle auto-maintain full | Subtitles | Full library scan |
| `0 4 * * 0` | 🎬 Subtitle dedupe full | Subtitles | Weekly, Sunday 4 AM |
| `0 3 * * *` | 🔄 Codec audit | Codec | Incremental, ~7 min |
| `45 3 * * *` | 🔄 Codec plan | Codec | Daily 3:45 AM |
| `30 4 * * 0` | 🔄 Codec verify-and-clean | Codec | Weekly, Sunday 4:30 AM |
| `0 5 * * 0` | 📺 Streaming scan | Streaming | Weekly, Sunday 5 AM |
| `30 5 * * 0` | 📺 Streaming stale-flag | Streaming | Weekly, Sunday 5:30 AM |
| `0 6 * * 0` | 📺 Streaming check-seasons | Streaming | Weekly, Sunday 6 AM |
| `30 6 * * 0` | 📺 Streaming stale-delete | Streaming | Weekly, Sunday 6:30 AM |
| `0 7 * * 0` | 📺 Streaming stale-cleanup | Streaming | Weekly, Sunday 7 AM |
| `0 4 * * *` | 🔍 Streaming verify-disputed | Streaming | Daily 4 AM |
| `0 7 1 * *` | 📺 Streaming monthly stale-cleanup | Streaming | 1st of month |
| `35 3 * * 2` | 📊 Emby last played report | Reports | Tuesday 03:35 UTC |
| `3 5 * * 2` | 👻 Emby weekly restart | Emby | Tuesday 05:03 UTC |
| `50 3 * * 1` | 🔵 Bazarr weekly restart | Maintenance | Monday — prevents FD leak |
| `50 3 * * 3` | 🔵 Sonarr weekly restart | Maintenance | Wednesday — prevents .NET growth |
| `50 3 * * 4` | 🟡 Radarr weekly restart | Maintenance | Thursday — prevents .NET growth |
| `0 2 */3 * *` | 💾 SQLite backup | Maintenance | All state DBs |

---

## 🔌 Integration Flow

```mermaid
sequenceDiagram
    participant TR as 🔴 Transmission
    participant SR as 🔵 Sonarr/Radarr
    participant WH as 🪝 Import Hook
    participant SUB as 🎬 Subtitle Pipeline
    participant BZ as 🔵 Bazarr
    participant DL as 🌐 DeepL
    participant CM as 🔄 Codec Manager
    participant SC as 📺 Streaming Checker
    participant EM as 📺 Emby
    participant DC as 🔔 Discord

    TR->>SR: Download complete
    SR->>WH: Webhook (OnImport)
    WH->>SUB: Phase 0: extract + strip embedded
    WH->>CM: enqueue-import (priority 0)
    WH->>SC: is_streaming_candidate?

    SUB->>BZ: rescan + search subtitles
    BZ-->>SUB: subtitles found / missing
    SUB->>DL: translate missing langs (background)
    DL->>DC: Translation summary

    SC-->>CM: streaming candidate → priority 99 (skip)
    CM->>EM: post-swap rescan
    CM->>DC: Conversion summary

    Note over SUB,DL: Pipeline: every 5 min
    Note over CM: Pipeline: every 5 min (codec step)
    Note over SC: Cron: weekly (independent)
```

---

## 📁 Repository Structure

```
📦 berenstuff/
├── 📄 .env                          # All secrets (never committed)
├── 📄 CLAUDE.md                     # Development conventions
│
├── 📂 apps/                         # App config backups
│   ├── 📂 bazarr_data/
│   ├── 📂 radarr_config/
│   └── 📂 sonarr_config/
│
├── 📂 automation/
│   ├── 📂 configs/                  # Tracked configs & crontab
│   │   ├── 📄 crontab.env-sourced  # ~14 cron jobs (install with: crontab <file>)
│   │   ├── 📄 bazarr-config.yaml
│   │   ├── 📄 radarr-config.xml
│   │   └── 📄 sonarr-config.xml
│   │
│   ├── 📂 docs/                     # Runbooks and design docs
│   ├── 📂 logs/                     # All cron/script logs
│   │
│   └── 📂 scripts/
│       ├── 📂 subtitles/            # 🎬 Subtitle system (bash)
│       │   ├── subtitle_quality_manager.sh
│       │   ├── lib_subtitle_common.sh
│       │   ├── arr_profile_extract_on_import.sh
│       │   ├── library_subtitle_dedupe.sh
│       │   └── bazarr_subtitle_recovery.sh
│       │
│       ├── 📄 media_pipeline.sh     # 🎯 Unified pipeline orchestrator
│       │
│       ├── 📂 transcode/            # 🔄 Codec manager (bash)
│       │   └── library_codec_manager.sh
│       │
│       ├── 📂 streaming/            # 📺 Streaming checker (python)
│       │   ├── streaming_checker.py
│       │   └── justwatch_client.py
│       │
│       ├── 📂 translation/          # 🌐 DeepL translator (python)
│       │
│       ├── 📄 arr_cleanup_importblocked.sh
│       ├── 📄 grab-monitor.sh
│       ├── 📄 emby_zombie_reaper.sh
│       └── 📄 emby_last_played_report.sh
│
├── 📂 backups/                      # Config backups
└── 📂 scripts/                      # Compat copies for Sonarr/Radarr hooks
```

---

## 📚 Shared Libraries

> Reusable Bash libraries that multiple scripts source instead of duplicating logic.

| Library | Path | Purpose |
|---------|------|---------|
| `lib_subtitle_common.sh` | `automation/scripts/subtitles/` | Language helpers, codec helpers, path classifiers, quality scoring — shared across all subtitle scripts |
| `lib_arr_notify.sh` | `automation/scripts/` | Unified notification helpers for Emby (library refresh, rescan), Sonarr/Radarr (rescan, queue ops), and Bazarr (scan-disk, subtitle search) |
| `lib_transmission.sh` | `automation/scripts/` | Transmission RPC wrapper — torrent lookup, removal, and status queries used by grab-monitor and arr-cleanup |

> All three are sourced with `source "$(dirname "$0")/../lib_..."` patterns. Compat copies in `scripts/` must stay in sync whenever these libraries change.

---

## 🛠️ Tech Stack

<div align="center">

| Layer | Technology | Purpose |
|-------|-----------|---------|
| 🐚 Shell | Bash 5+ (`set -euo pipefail`) | Core automation scripts |
| 🐍 Python | Python 3 + Click | Streaming checker + translator |
| 🗄️ State | SQLite (6 databases) | Codec plans + pipeline_state, streaming index, translation, subtitle quality, grab-monitor |
| 🎞️ Media | ffprobe / ffmpeg | Media analysis and conversion |
| 🔗 *arr APIs | Sonarr · Radarr · Bazarr | Library management |
| 📺 Server | Emby | Media server |
| 🧲 Torrents | Transmission | Download client |
| 🎬 Metadata | TMDB API | Streaming availability data |
| 🌙 Streaming | Movie of the Night (RapidAPI) | Per-season availability + cross-validation |
| 📊 Streaming | Watchmode API | Cross-validation voter |
| 🎯 Streaming | JustWatch GraphQL | Cross-validation voter (no API key) |
| 🌐 Translation | Gemini + DeepL Pro + Google Translate | Subtitle translation (Gemini primary, DeepL fallback, Google last resort) |
| 🔔 Alerts | Discord Webhooks | All system notifications |
| 🔒 Concurrency | `flock` | Cron job mutual exclusion |
| 🔍 Indexers | Prowlarr (23 indexers) | Torrent search |

</div>

---

## ⚙️ Configuration

### 🔑 Environment (`.env`)

All secrets live in `.env` and are sourced by cron via the env-sourced crontab. Never committed to git.

```bash
# *arr APIs
SONARR_API_KEY=...
RADARR_API_KEY=...
BAZARR_API_KEY=...
EMBY_API_KEY=...
PROWLARR_API_KEY=...

# External APIs
DEEPL_API_KEY=...             # Pro tier (no :fx suffix)
TMDB_API_KEY=...
RAPIDAPI_KEY=...             # Movie of the Night

# Webhooks
DISCORD_WEBHOOK_URL=...

# Paths
APPBOX_DATA=/APPBOX_DATA/storage
```

### 📦 Installing Crontab

```bash
# NEVER use sed piped to crontab -
# Always edit the tracked file, then install:
crontab automation/configs/crontab.env-sourced
```

---

## 📖 Operational Lessons

> Hard-won lessons from running this stack in production. Each one cost debugging time.

<details>
<summary>🚨 <strong>Critical: Never pipe sed to crontab -</strong></summary>

Complex cron lines with pipes/quotes/escapes break sed patterns and **can wipe the entire crontab**. Always edit `automation/configs/crontab.env-sourced` then install with `crontab <file>`.

</details>

<details>
<summary>🔄 <strong>Always `</dev/null` for external commands in while-read loops</strong></summary>

`ffmpeg`, `sqlite3`, `curl`, and other tools can consume bytes from process substitution pipes (e.g., `find | sort`), causing truncated paths or skipped iterations. Best practice: pre-load data into bash arrays before the loop, or add `</dev/null` to every external command inside the loop body.

</details>

<details>
<summary>🔇 <strong>`flock -n` silently exits</strong></summary>

When cron jobs use non-blocking flock and another process holds the lock, the invocation exits with code 1 and writes nothing to the log. This is by design. Don't mistake silent flock skips for stalled jobs.

</details>

<details>
<summary>🗄️ <strong>SQLite queries fail when DB is locked by active process</strong></summary>

The codec converter holds a WAL lock during ffmpeg runs. `sqlite3` CLI from another process hits `busy_timeout` and may return empty results. Check if a conversion is running before concluding the DB is empty.

</details>

<details>
<summary>📍 <strong>Codec manager DB has `-media` suffix</strong></summary>

Actual path: `/APPBOX_DATA/storage/.transcode-state-media/` (not `.transcode-state/`).

</details>

<details>
<summary>🔧 <strong>Sonarr `languages` field is immutable after import</strong></summary>

`PUT /api/v3/episodefile` returns 202 but doesn't update `languages`. Bazarr reads `audio_language` from this field. Fix: write directly to Bazarr DB after codec conversion.

</details>

<details>
<summary>🐛 <strong>PRAGMA busy_timeout output leaks into queries</strong></summary>

`sqlite3 "PRAGMA busy_timeout=30000; SELECT ..."` prints `30000` before the SELECT result. Fix: pipe through `tail -1` and guard against the `"30000"` value. Caused `set -e` failures in full scan mode.

</details>

<details>
<summary>🗑️ <strong>Orphaned temp files pollute find scans</strong></summary>

Interrupted ffmpeg operations leave temp files (`.striptmp.*`, `.bloattmp.*`, `.subtmp.*`, `.collisiontmp.*`). Four-layer fix: (1) all temp files use dot-prefix (e.g. `.Movie.striptmp.mkv`) so Radarr/Emby skip them — prevents metadata orphans, (2) auto-cleanup at scan start removes stale temps older than 1 hour, (3) `! -name "*tmp.*"` in all find patterns, (4) normal success path cleans up immediately.

</details>

<details>
<summary>🔢 <strong>`grep -c || echo 0` outputs double zero</strong></summary>

`grep -c` exits 1 AND outputs `0` when no matches, then `|| echo 0` fires, producing `0\n0`. Fix:
```bash
cue_count="$(grep -cE ... 2>/dev/null)" || cue_count=0
```
Caused `analyze_srt_file()` failures in full scan mode.

</details>

---

<div align="center">

---

```
╔════════════════════════════════════════════════╗
║   Built with ☕, bash, and questionable        ║
║   late-night automation decisions.             ║
║                                                ║
║   Private repo — beregcamlost/arr_configs      ║
╚════════════════════════════════════════════════╝
```

[![GitHub](https://img.shields.io/badge/GitHub-beregcamlost%2Farr__configs-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/beregcamlost/arr_configs)

</div>
