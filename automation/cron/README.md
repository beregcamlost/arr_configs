# automation/cron/

This directory contains structured documentation for every mubuntu cron job.

## jobs.yml

`jobs.yml` is the single source of truth for cron job metadata. It records
schedule, command, purpose, state DB, log path, failure behaviour, and
dependencies for every job in the pipeline.

### How to read jobs.yml

Each entry has these fields:

| Field | Meaning |
|---|---|
| `id` | Unique identifier — matches the lock/log name pattern |
| `schedule` | Standard cron expression |
| `command` | Full command as it appears in the crontab |
| `purpose` | Plain-English description of what the job does |
| `runtime_typical` | Expected wall-clock duration under normal conditions |
| `timeout_hard` | Hard deadline before cron fires again (usually = flock prevents overlap) |
| `state_db` | SQLite state database this job reads or writes (`none` if stateless) |
| `lock` | flock file path (`none` if no lock) |
| `log` | Where stdout/stderr go |
| `on_failure` | What happens if the job exits non-zero |
| `depends_on` | External services, env vars, or other jobs this one needs |
| `safe_to_disable` | Whether disabling this cron breaks anything else immediately |
| `enabled` | Present and `false` only for disabled/commented-out jobs |

### Why jobs.yml exists

The crontab itself is a flat list of shell commands — it has no metadata,
no rationale, and no dependency graph. `jobs.yml` captures that knowledge
so that any engineer (or future Claude agent) can understand the pipeline
without reverse-engineering 19 cron lines.

It also provides a machine-readable basis for a future cron-installer script
that could generate the crontab from this YAML (not yet implemented — see
below).

## Installing the crontab

The active crontab is not currently installed (crons are stopped as of
2026-04-29). The backup lives at:

```
/config/cron-backup-20260430-020702/crontab.txt
```

To re-enable all crons, install that file:

```bash
crontab /config/cron-backup-20260430-020702/crontab.txt
```

Verify:

```bash
crontab -l
```

## Future: generate crontab from jobs.yml

A future `cron_install.sh` script could parse `jobs.yml` and write the
crontab automatically, ensuring jobs.yml and the live crontab stay in sync.
For now, any schedule change must be made in **both** `jobs.yml` and the
backup crontab file, then re-installed manually.
