# intake_webhook — Intake-time subtitle language detection

## What it does and why

`intake_webhook.py` is a tiny HTTP receiver (stdlib-only, no external deps) that
catches mis-labeled subtitles **at download time** rather than during the daily
SQM slow-lane scan.

When Bazarr downloads a subtitle it fires a POST webhook to this receiver.  The
receiver runs `sub_lang_sniff.py` on the file immediately.  If the content
language does not match the expected language the subtitle is **quarantined** —
renamed to `<filename>.rejected.srt` — and a Discord alert is posted.  If the
result is MATCH, UNCERTAIN, or an IO error the file passes through untouched.

This is a complement to SQM, not a replacement.  SQM still catches subtitles
that bypass this receiver (e.g., subtitles already in the library before the
receiver was deployed, or files downloaded while the receiver was down).

## Files

| File | Purpose |
|------|---------|
| `automation/scripts/subtitles/intake_webhook.py` | HTTP server (stdlib only) |
| `automation/scripts/subtitles/intake_webhook_run.sh` | Managed launcher with restart loop |
| `automation/logs/intake_webhook.log` | Combined request + verdict log |

## How to wire up Bazarr

1. Open Bazarr → **Settings → Notifications**
2. Click **+** to add a new notification agent → choose **Custom Webhook**
3. Set **Name**: `intake-langdetect`
4. Set **URL**: `http://127.0.0.1:8765/bazarr`
5. Set **Method**: POST
6. Under **Events**, enable: **On Subtitle Download** only
7. Leave payload as default (Bazarr sends JSON automatically)
8. Click **Test** — you should see `{"action": "passed", ...}` or similar in the Bazarr UI
9. Save

Bazarr sends a JSON body that includes `path` (the subtitle file path on disk).
The receiver accepts any of: `subtitle_path`, `subtitlePath`, `srt_path`, `path`, `subtitle`.

### Payload language field

Bazarr also sends a `language` field (e.g., `"es"`, `"en"`).  The receiver reads
this automatically — no custom payload template needed.  If the field is absent
it falls back to `INTAKE_WEBHOOK_DEFAULT_LANG` (default: `es`).

## Enabling the daemon (do after Bazarr is wired up)

The cron entry is in `automation/configs/crontab.bak` as a TODO comment.
To activate:

1. Confirm the receiver works by running the smoke test below
2. Edit the live crontab: `crontab -e`
3. Add the entry from `crontab.bak` (the line under `# TODO: enable after Bazarr webhook config is wired up`)
4. Save — cron will start the receiver within 5 minutes

## Manual smoke test

```bash
# Terminal 1 — start the receiver in foreground
bash /config/berenstuff/automation/scripts/subtitles/intake_webhook_run.sh

# Terminal 2 — test health endpoint
curl -s http://127.0.0.1:8765/health
# Expected: {"ok": true, "version": "1.0", "uptime_sec": N}

# Test with a real SRT file (replace path with an actual file)
curl -s -X POST http://127.0.0.1:8765/bazarr \
  -H 'Content-Type: application/json' \
  -d '{"subtitle_path": "/path/to/subtitle.es.srt", "language": "es"}'
# Expected: {"action": "passed", "verdict": "OK lang=es p=0.987"}  (or similar)

# Test rejection path — send a mismatched language expectation
curl -s -X POST http://127.0.0.1:8765/bazarr \
  -H 'Content-Type: application/json' \
  -d '{"subtitle_path": "/path/to/english.srt", "language": "es"}'
# Expected: {"action": "rejected", "reason": "WRONG_LANG ...", "quarantined_as": "..."}

# Ctrl-C to stop the receiver
```

## Monitoring

```bash
# Follow live log
tail -f /config/berenstuff/automation/logs/intake_webhook.log

# Count rejections in the last 24h
grep WRONG_LANG /config/berenstuff/automation/logs/intake_webhook.log | grep "$(date +%Y-%m-%d)"

# Check if receiver is up
curl -sf http://127.0.0.1:8765/health && echo UP || echo DOWN
```

`pipeline_health.sh` also checks the `/health` endpoint every 15 minutes and
issues a WARN if the receiver is down (once it is deployed and expected to be
running).

## Tuning thresholds

The detection thresholds are inherited from `sub_lang_sniff.py` and controlled
via environment variables (set in `/config/berenstuff/.env`):

| Env var | Default | Meaning |
|---------|---------|---------|
| `SUB_LANG_SNIFF_EXPECTED_MIN` | `0.70` | Min probability to call a subtitle MATCH |
| `SUB_LANG_SNIFF_WRONG_MIN` | `0.60` | Min probability of an other-language to call WRONG_LANG |
| `INTAKE_WEBHOOK_PORT` | `8765` | Port the receiver listens on |
| `INTAKE_WEBHOOK_DEFAULT_LANG` | `es` | Fallback expected language when payload has no language field |

To raise the bar (fewer false positives), increase `SUB_LANG_SNIFF_EXPECTED_MIN`
toward `0.85`.  To lower it (catch more mismatches), decrease
`SUB_LANG_SNIFF_WRONG_MIN` toward `0.50`.

## Architecture

```
Bazarr downloads subtitle
        |
        v
POST /bazarr  (JSON: {"path": "...", "language": "es"})
        |
        v
intake_webhook.py
        |
        +-- sub_lang_sniff.py <srt_path> <lang>
        |         |
        |    exit 0 (OK)  ->  pass through, 200 {"action": "passed"}
        |    exit 1 (WRONG_LANG) -> rename to .rejected.srt
        |         |                  Discord alert
        |         |                  200 {"action": "rejected"}
        |    exit 2 (UNCERTAIN)  ->  pass through, 200 {"action": "passed"}
        |    exit 3 (IO error)   ->  log warning, pass through
        v
SQM daily scan (safety net for anything that slipped through)
```
