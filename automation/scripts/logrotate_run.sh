#!/usr/bin/env bash
# logrotate_run.sh — User-mode logrotate for automation/logs/
# Runs logrotate with a per-user state file so no /etc/ or sudo needed.
#
# Cron entry (add to /config/cron-backup-20260430-020702/crontab.txt when re-enabling):
#   15 3 * * * /bin/bash /config/berenstuff/automation/scripts/logrotate_run.sh >> /config/berenstuff/automation/logs/logrotate.log 2>&1
set -euo pipefail

readonly CONF="/config/berenstuff/automation/logrotate.conf"
readonly STATE="/config/berenstuff/automation/.logrotate.state"
readonly LOG_DIR="/config/berenstuff/automation/logs"

mkdir -p "$LOG_DIR"

if [[ ! -f "$CONF" ]]; then
  printf '%s [logrotate_run] ERROR: config not found: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CONF" >&2
  exit 1
fi

printf '%s [logrotate_run] starting\n' "$(date '+%Y-%m-%d %H:%M:%S')"
/usr/sbin/logrotate -s "$STATE" "$CONF"
exit_code=$?
printf '%s [logrotate_run] done (exit=%d)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$exit_code"
exit "$exit_code"
