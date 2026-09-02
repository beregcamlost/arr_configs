#!/usr/bin/env bash
# release_stale_3090_claims.sh (2026-08-11)
# Red de seguridad para el worker del 3090 (tanda3090.py --auto): si berentendo se
# apaga a mitad de una conversion, la fila queda con claimed_by='gpu3090@<epoch>' y
# el cron de mubuntu la ignora para siempre (su query solo acepta NULL o 'mubuntu').
# Aqui se liberan los claims de mas de 2 h para que mubuntu retome el trabajo.
set -euo pipefail
DB="${CODEC_DB:-/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db}"
MAX_AGE_SEC="${CLAIM_MAX_AGE_SEC:-7200}"
[ -f "$DB" ] || exit 0
n="$(sqlite3 -cmd '.timeout 20000' "$DB" "
UPDATE conversion_plan SET claimed_by=NULL
WHERE claimed_by LIKE 'gpu3090@%'
  AND CAST(substr(claimed_by,9) AS INTEGER) < strftime('%s','now') - $MAX_AGE_SEC;
SELECT changes();")"
if [ "${n:-0}" -gt 0 ]; then
  printf '%s liberados %s claims colgados del 3090 (>%ss)\n' "$(date '+%F %T')" "$n" "$MAX_AGE_SEC"
fi
