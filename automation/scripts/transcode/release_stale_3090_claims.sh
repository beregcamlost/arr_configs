#!/usr/bin/env bash
# release_stale_3090_claims.sh (2026-08-11; ampliado 2026-09-01)
# Red de seguridad para el worker del 3090 (tanda3090.py --auto): si berentendo se
# apaga a mitad de una conversion, la fila queda con claimed_by='gpu3090@<epoch>' y
# el cron de mubuntu la ignora para siempre (su query solo acepta NULL o 'mubuntu').
# Aqui se liberan los claims de mas de 2 h para que mubuntu retome el trabajo.
#
# --- 2026-09-01: los dos agujeros que dejaron "Toy Story 5" pending 10 dias -------
# (1) esto solo miraba conversion_plan, pero audio_lang_detect usa EL MISMO token de
#     claim (langid_worker.py: "gpu3090@%d" % time.time()) y su worklist() exige
#     "claimed_by IS NULL" => un claim colgado ahi no lo soltaba nadie: permanente.
# (2) nadie cerraba las filas de langid cuyo archivo ya no existe. Cuando Radarr
#     reemplaza un release, media_files queda con deleted_at y la fila se vuelve
#     invisible para el worker (worklist filtra "mf.deleted_at IS NULL") pero SIGUE
#     contando como 'pending' en el resumen. Mismo patron que el barrido de
#     huerfanos de conversion_plan (2026-08-12, 201 fantasmas), ahora para langid.
#     Seguridad: nos apoyamos en deleted_at, que ya es la salida del barrido con
#     guard ORPHAN_SWEEP_MAX_PCT, asi que un montaje caido no dispara esto.
set -euo pipefail
DB="${CODEC_DB:-/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db}"
MAX_AGE_SEC="${CLAIM_MAX_AGE_SEC:-7200}"
[ -f "$DB" ] || exit 0

sq() { sqlite3 -cmd '.timeout 20000' "$DB" "$1"; }

# 1) claims colgados en conversion_plan (el comportamiento original)
n="$(sq "
UPDATE conversion_plan SET claimed_by=NULL
WHERE claimed_by LIKE 'gpu3090@%'
  AND CAST(substr(claimed_by,9) AS INTEGER) < strftime('%s','now') - $MAX_AGE_SEC;
SELECT changes();")"
if [ "${n:-0}" -gt 0 ]; then
  printf '%s liberados %s claims colgados del 3090 (>%ss)\n' "$(date '+%F %T')" "$n" "$MAX_AGE_SEC"
fi

# 2) claims colgados en audio_lang_detect (mismo token, mismo riesgo)
a="$(sq "
UPDATE audio_lang_detect SET claimed_by=NULL
WHERE status='pending' AND claimed_by LIKE 'gpu3090@%'
  AND CAST(substr(claimed_by,9) AS INTEGER) < strftime('%s','now') - $MAX_AGE_SEC;
SELECT changes();")"
if [ "${a:-0}" -gt 0 ]; then
  printf '%s liberados %s claims colgados de langid (>%ss)\n' "$(date '+%F %T')" "$a" "$MAX_AGE_SEC"
fi

# 3) filas de langid cuyo archivo ya no existe -> file_gone
#    (misma convencion de estado que usa lang_id.sh cuando el fichero desaparece)
g="$(sq "
UPDATE audio_lang_detect SET status='failed', error='file_gone', claimed_by=NULL
WHERE status='pending'
  AND media_id IN (SELECT id FROM media_files WHERE deleted_at IS NOT NULL);
SELECT changes();")"
if [ "${g:-0}" -gt 0 ]; then
  printf '%s cerradas %s filas de langid sin archivo (file_gone)\n' "$(date '+%F %T')" "$g"
fi
