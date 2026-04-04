# Language Grab Filter — Design Spec
**Date:** 2026-04-04  
**Status:** Approved

## Context

Sonarr grabbed all 4 episodes of Rooster S01 as `MULTI.1080p.WEB.X264-HiggsBoson` (French primary + English secondary audio). Root causes:
1. French was whitelisted in "Unwanted Languages" CF
2. "NOT MY LANGS" AND-logic bypassed when English (a wanted language) is present
3. No mechanism to reject MULTI releases containing wanted + unwanted languages

Goal: Accept **only** releases whose audio languages are a subset of `{Original, English, Spanish, Spanish (Latino)}` — nothing more, nothing less. Works for all 77 series and 585 movies, including 25 Japanese anime, 12 French movies, and content in 16+ original languages.

---

## Architecture

Two complementary layers:

### Layer 1 — Custom Format Hardening (Sonarr + Radarr)

**What CFs can do:** Detect "does release contain language X?" and apply scores. Cannot check "does release ONLY contain languages from set Y?"

**What CFs cannot do:** Block MULTI releases that contain a wanted language alongside an unwanted one (AND-logic bypass). This is a hard Sonarr limitation.

Coverage after changes: ~95% of real-world cases. Single-language unwanted releases are caught natively.

### Layer 2 — Grab Monitor Script

A bash cron script that checks recent grabs every 3 minutes. For each grabbed release, it validates all detected languages against the series/movie's original language + EN/ES/LAT. If any language is outside the allowed set → removes from Transmission + blacklists in Sonarr/Radarr + Discord notification.

Coverage: The remaining ~5% (MULTI releases with wanted + unwanted languages). Reactive but catches within 3 minutes, before significant download occurs.

---

## CF Changes

### Both Sonarr and Radarr

#### 1. "NOT MY LANGS" CF
**Sonarr ID:** 10 | **Radarr ID:** 10

Current specs (Sonarr): EN, LAT(34), JAP, ORIGINAL, ES  
Current specs (Radarr): EN, LAT(37), ORIGINAL, ES ← already lean, no JAP

**Change (Sonarr only):** Remove JAP(8) spec. "ORIGINAL(-2)" dynamically covers Japanese for anime.

**Final specs (both):** all `negate=true, required=true`
| Spec | Sonarr ID | Radarr ID |
|------|-----------|-----------|
| EN   | 1         | 1         |
| ES   | 3         | 3         |
| LAT  | 34        | 37        |
| ORIGINAL | -2   | -2        |

**Logic:** Fires -10000 when release has NONE of {English, Spanish, LAT, Original}. Catches pure-foreign-language releases.

---

#### 2. "Unwanted Languages" CF  
**Sonarr ID:** 28 | **Radarr ID:** 21

**Goal:** Keep only languages that have **zero original-language content** in the library. All other languages rely on "NOT MY LANGS" + Layer 2 monitor.

**Languages to REMOVE (currently in CF, but have original-language content):**

| Language | Sonarr ID | Radarr ID | Content |
|----------|-----------|-----------|---------|
| German   | 4         | 4         | 6 Radarr movies |
| Danish   | 6         | 6         | 1 Radarr movie |
| Russian  | 11        | 11        | 3 Radarr movies |
| Norwegian| 15        | 15        | 1 Radarr movie |
| Portuguese| 18       | 18        | 1 Radarr movie |
| Korean   | 21        | 21        | 3 Sonarr series + 9 Radarr movies |
| Hindi    | 27 (Sonarr) | 26 (Radarr) | 2 Radarr movies |
| Thai     | 32 (Sonarr) | 28 (Radarr) | 4 Radarr movies |
| Serbian  | 40        | 40        | 1 Radarr movie |
| Indonesian| 44       | 44        | 2 Radarr movies |

Also kept OUT (currently already whitelisted, must remain out):
- French (2): 12 Radarr movies
- Italian (5): 1 Radarr movie
- Japanese (8): 25 Sonarr series + 36 Radarr movies
- Chinese (10): 1 Sonarr series + 8 Radarr movies

**Languages that STAY in "Unwanted Languages"** (zero content in library):

Sonarr (IDs 1–46): Dutch(7), Icelandic(9), Polish(12), Vietnamese(13), Swedish(14), Finnish(16), Turkish(17), Flemish(19), Greek(20), Hungarian(22), Hebrew(23), Lithuanian(24), Czech(25), Arabic(26), Bulgarian(28), Malayalam(29), Ukrainian(30), Slovak(31), Portuguese Brazil(33), Romanian(35), Latvian(36), Persian(37), Catalan(38), Croatian(39), Bosnian(41), Estonian(42), Tamil(43), Macedonian(45), Slovenian(46)

Radarr (IDs 1–56): Same set + Bengali(34), Telugu(45), Albanian(50), Afrikaans(51), Marathi(52), Tagalog(53), Urdu(54), Romansh(55), Mongolian(56)

Score: **-10000** (unchanged)

---

### Layer 1 Coverage Summary

| Release type | Layer 1 catches? | Layer 2 catches? |
|---|---|---|
| French-only for English series | ✅ NOT MY LANGS (no EN/ES/LAT/Original) | N/A |
| French+English MULTI for English series | ❌ AND-logic bypass | ✅ Monitor |
| Japanese-only for English series | ✅ NOT MY LANGS | N/A |
| Japanese-only for Japanese anime | ✅ passes (Original=Japanese) | N/A |
| Korean drama Korean-only release | ✅ passes (Original=Korean) | N/A |
| Korean+English MULTI for Korean drama | ❌ AND-logic bypass | ✅ Monitor |
| French movie (Amélie) French-only | ✅ passes (Original=French) | N/A |
| Polish/Turkish/Swedish-only | ✅ Unwanted Languages | N/A |

---

## Layer 2: Grab Monitor Script

**File:** `automation/scripts/grab-monitor.sh`  
**Compat copy:** `scripts/grab-monitor.sh`

### Inputs
- Sonarr API: `http://localhost:8989/sonarr/api/v3/history` (eventType=grabbed, last 5 minutes)
- Radarr API: `http://localhost:7878/radarr/api/v3/history` (eventType=grabbed, last 5 minutes)
- Series/movie original language from respective API

### Logic per grab

```
for each recent grab:
  series = get series/movie (to find originalLanguage.id)
  allowed = {originalLanguage.id, 1, 3, 34/37}  # EN, ES, LAT (app-specific ID)
  parsed_langs = release.languages (from grab data)
  if any(lang NOT in allowed for lang in parsed_langs):
    → remove download from Transmission (DELETE /api/torrent/{hash})
    → blacklist in Sonarr/Radarr (POST /api/v3/blacklist/{id})
    → log + Discord notify
```

### Parsed language caveat
Sonarr detects release language from the **release title** at grab time, not from audio tracks. MULTI, VFF, FRENCH, TRUEFRENCH keywords are parsed as French. Dual audio tags parsed as two languages. If Sonarr fails to detect (Unknown), the monitor skips it (CFs already scored it).

### State tracking
Use a SQLite state file (`.grab-monitor-state/seen.db`) to avoid re-processing the same grab ID on subsequent cron runs.

### Cron
Every 3 minutes — fast enough to cancel before significant data is downloaded:
```
*/3 * * * * /path/to/grab-monitor.sh
```

### Notification format (Discord)
```
🚫 GRAB BLOCKED — Language violation
Series: Rooster (English)
Release: Rooster.S01E01.MULTI.1080p.WEB.X264-HiggsBoson
Detected: French, English
Allowed: English (Original), English, Spanish, Spanish (Latino)
Violation: French
Action: Removed from Transmission, blacklisted
```

---

## Affected Files

| File | Change |
|------|--------|
| Sonarr CF ID 10 "NOT MY LANGS" | Remove JAP(8) spec |
| Sonarr CF ID 28 "Unwanted Languages" | Remove 10 language IDs with original-language content |
| Radarr CF ID 10 "NOT MY LANGS" | No change (already lean) |
| Radarr CF ID 21 "Unwanted Languages" | Remove 10 language IDs with original-language content |
| `automation/scripts/grab-monitor.sh` | New file |
| `scripts/grab-monitor.sh` | New compat copy |
| `automation/configs/crontab.env-sourced` | Add `*/3 * * * *` entry |
| `quality_profiles.md` (memory) | Update whitelist docs |

---

## Verification

1. **CF changes**: Query Sonarr/Radarr APIs, confirm JAP removed from "NOT MY LANGS", confirm removed languages gone from "Unwanted Languages"
2. **Manual search test**: In Sonarr, search Rooster S01E01 manually — MULTI releases should now score negative (via Layer 2 blacklist from initial bad grab) or be removed immediately
3. **Anime test**: Manual search for Attack on Titan — Japanese releases should score positive and pass
4. **French movie test**: Manual search for Amélie in Radarr — French releases should pass
5. **Grab monitor test**: Run script manually with `--dry-run`, verify it would have caught the Rooster MULTI grabs
6. **Cron test**: Trigger a known-bad grab, wait ≤3 min, verify removal + Discord notification

---

## Notes / Gotchas

- Sonarr and Radarr use **different language IDs** for Hindi (Sonarr=27, Radarr=26) and Thai (Sonarr=32, Radarr=28) — the removal lists differ.
- Korean(21) has the SAME ID in both — simplifies that part.
- Radarr "Unwanted Languages" has IDs up to 56; Sonarr only up to 46.
- The grab monitor must not use `while read` loops with `curl` calls (stdin consumption pitfall) — use arrays.
- Script must use `</dev/null` on all curl/sqlite calls inside loops.
- State DB path: `.grab-monitor-state/` (follow naming convention like `.transcode-state-media/`)
