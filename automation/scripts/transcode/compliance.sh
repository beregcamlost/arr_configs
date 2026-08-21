#!/usr/bin/env bash
# compliance.sh - FASE 2: la unica fuente de verdad sobre conformidad.
# Contrato: conforme / encolado / trabajando / aparcado / fallido
# Todo lo demas (Emby, pipeline_health, correo diario) lee de aqui.
set -euo pipefail

STATE_DIR="${STATE_DIR:-/APPBOX_DATA/storage/.transcode-state-media}"
DB="${COMPLIANCE_DB:-$STATE_DIR/library_codec_state.db}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="$SCRIPT_DIR/compliance_schema.sql"
MEDIA_ROOT="/APPBOX_DATA/storage/media"

die() { echo "error: $*" >&2; exit 1; }
db()  { sqlite3 -cmd ".timeout 30000" "$DB" "$@"; }
sq()  { echo "$1" | sed "s/'/''/g"; }

usage() {
  cat <<'USAGE'
uso: compliance <comando> [args]

  init                           crea la tabla de exenciones, la de calibracion y la vista
  status <patron>                estado de los archivos cuya ruta contenga <patron>
  summary                        conteo por estado
  queue                          lo que esta encolado o trabajando, con ETA
  stuck [horas]                  lo que lleva demasiado sin cerrar (default 6 h)
  calibrate                      recalcula la ETA desde el historico real de conversion_runs
  exempt-add <patron> <motivo>   marca como exento permanente todo lo que matchee
  exempt-list                    lista las exenciones
  exempt-rm <patron>             quita la exencion
USAGE
}

need_view() {
  local n
  n="$(db "SELECT COUNT(*) FROM sqlite_master WHERE name='compliance_state';")"
  [[ "$n" == "1" ]] || die "la vista compliance_state no existe todavia; corre: compliance init"
}

cmd_init() {
  [[ -f "$SCHEMA" ]] || die "no encuentro el esquema en $SCHEMA"
  db < "$SCHEMA"
  echo "esquema aplicado sobre $DB"
  db "SELECT 'vista compliance_state: ' || COUNT(*) || ' filas' FROM compliance_state;"
}

cmd_summary() {
  need_view
  echo "== estado de la biblioteca =="
  sqlite3 -header -column "$DB" "
    SELECT estado, COUNT(*) AS archivos, ROUND(SUM(gb),1) AS gb
    FROM compliance_state GROUP BY estado
    ORDER BY CASE estado WHEN 'fallido' THEN 1 WHEN 'trabajando' THEN 2
                         WHEN 'encolado' THEN 3 WHEN 'aparcado' THEN 4 ELSE 5 END;"
  echo ""
  echo "== por motivo =="
  sqlite3 -header -column "$DB" "
    SELECT estado, motivo, COUNT(*) AS n FROM compliance_state
    GROUP BY estado, motivo ORDER BY n DESC LIMIT 20;"
}

cmd_status() {
  need_view
  local pat="${1:-}"
  [[ -n "$pat" ]] || die "falta el patron de ruta"
  local like="%$(sq "$pat")%"
  local n
  n="$(db "SELECT COUNT(*) FROM compliance_state WHERE path LIKE '$like';")"
  if [[ "$n" == "0" ]]; then
    echo "sin coincidencias para: $pat"
    return 1
  fi
  db -separator '|' "
    SELECT estado, motivo, COALESCE(clase,'-'), COALESCE(eta_min,''), COALESCE(desde,''), path
    FROM compliance_state WHERE path LIKE '$like' ORDER BY path;" |
  while IFS='|' read -r estado motivo clase eta desde path; do
    local extra=""
    [[ -n "$eta" ]] && extra="  [eta ${eta} min, $clase]"
    echo "${estado}  ${path#"$MEDIA_ROOT"/}${extra}"
    echo "    motivo=${motivo}  desde=${desde:-?}"
  done
}

cmd_queue() {
  need_view
  sqlite3 -header -column "$DB" "
    SELECT estado, clase, motivo, ROUND(eta_min,1) AS eta_min, gb,
           REPLACE(path,'$MEDIA_ROOT/','') AS archivo
    FROM compliance_state WHERE estado IN ('encolado','trabajando')
    ORDER BY CASE estado WHEN 'trabajando' THEN 0 ELSE 1 END, eta_min;"
  echo ""
  db "SELECT 'total encolado+trabajando: ' || COUNT(*) ||
             '   ETA agregada: ' || COALESCE(ROUND(SUM(eta_min),0),0) || ' min'
      FROM compliance_state WHERE estado IN ('encolado','trabajando');"
}

cmd_stuck() {
  need_view
  local h="${1:-6}"
  sqlite3 -header -column "$DB" "
    SELECT estado, motivo, ROUND((julianday('now')-julianday(desde))*24.0,1) AS horas,
           REPLACE(path,'$MEDIA_ROOT/','') AS archivo
    FROM compliance_state
    WHERE estado IN ('encolado','trabajando','fallido')
      AND desde IS NOT NULL
      AND (julianday('now')-julianday(desde))*24.0 > $h
    ORDER BY horas DESC;"
}

cmd_calibrate() {
  need_view
  # ETA desde el historico REAL. No se puede saber a posteriori si una corrida vieja fue
  # copy-only o transcode (conversion_plan.reason se resetea al terminar), asi que se usa
  # la distribucion de min/GB: el cuartil rapido calibra 'rapido' (remux y de-embed, que
  # son -c copy y van por I/O) y la mediana global calibra 'largo'. Ventana de 45 dias
  # para no arrastrar la era anterior del pipeline.
  local tmp
  tmp="$(mktemp)"
  db -separator '|' "
    SELECT ROUND(((julianday(cr.end_ts)-julianday(cr.start_ts))*1440.0) /
                 MAX(COALESCE(m.size_bytes,0)/1073741824.0, 0.05), 3) AS min_per_gb
    FROM conversion_runs cr JOIN media_files m ON m.id=cr.media_id
    WHERE cr.status='swapped' AND cr.end_ts IS NOT NULL AND cr.start_ts IS NOT NULL
      AND julianday('now')-julianday(cr.end_ts) <= 45
      AND (julianday(cr.end_ts)-julianday(cr.start_ts))*1440.0 BETWEEN 0.2 AND 600
    ORDER BY min_per_gb;" > "$tmp"
  local n
  n="$(wc -l < "$tmp")"
  [[ "$n" -ge 10 ]] || { rm -f "$tmp"; die "solo $n corridas utiles en 45 dias; no calibro con tan poco"; }
  local p25 p50
  p25="$(awk -v n="$n" 'NR==int(n*0.25)+1 {print; exit}' "$tmp")"
  p50="$(awk -v n="$n" 'NR==int(n*0.50)+1 {print; exit}' "$tmp")"
  rm -f "$tmp"
  db <<SQL
INSERT INTO compliance_calib(clase,min_per_gb,min_floor,muestras,calc_ts)
VALUES('rapido',$p25,1.0,$n,CURRENT_TIMESTAMP)
ON CONFLICT(clase) DO UPDATE SET min_per_gb=excluded.min_per_gb, min_floor=excluded.min_floor,
  muestras=excluded.muestras, calc_ts=excluded.calc_ts;
INSERT INTO compliance_calib(clase,min_per_gb,min_floor,muestras,calc_ts)
VALUES('largo',$p50,3.0,$n,CURRENT_TIMESTAMP)
ON CONFLICT(clase) DO UPDATE SET min_per_gb=excluded.min_per_gb, min_floor=excluded.min_floor,
  muestras=excluded.muestras, calc_ts=excluded.calc_ts;
SQL
  echo "calibrado sobre $n corridas exitosas de los ultimos 45 dias:"
  sqlite3 -header -column "$DB" "SELECT clase, min_per_gb, min_floor, muestras, calc_ts FROM compliance_calib;"
}

cmd_exempt_add() {
  local pat="${1:-}" motivo="${2:-}"
  [[ -n "$pat" && -n "$motivo" ]] || die "uso: compliance exempt-add <patron> <motivo>"
  local like="%$(sq "$pat")%"
  local mot
  mot="$(sq "$motivo")"
  local n
  n="$(db "SELECT COUNT(*) FROM media_files WHERE deleted_at IS NULL AND path LIKE '$like';")"
  [[ "$n" -gt 0 ]] || die "sin coincidencias para: $pat"
  db "INSERT OR REPLACE INTO compliance_exempt(media_id,path,motivo)
      SELECT id, path, '$mot' FROM media_files WHERE deleted_at IS NULL AND path LIKE '$like';"
  echo "exentos $n archivos por: $motivo"
}

cmd_exempt_list() {
  sqlite3 -header -column "$DB" "
    SELECT motivo, COUNT(*) AS n, MIN(added_ts) AS desde FROM compliance_exempt
    GROUP BY motivo ORDER BY n DESC;"
}

cmd_exempt_rm() {
  local pat="${1:-}"
  [[ -n "$pat" ]] || die "uso: compliance exempt-rm <patron>"
  local like="%$(sq "$pat")%"
  local n
  n="$(db "SELECT COUNT(*) FROM compliance_exempt WHERE path LIKE '$like';")"
  db "DELETE FROM compliance_exempt WHERE path LIKE '$like';"
  echo "quitadas $n exenciones"
}

[[ -f "$DB" ]] || die "no encuentro la base en $DB"
case "${1:-}" in
  init)         shift; cmd_init "$@" ;;
  status)       shift; cmd_status "$@" ;;
  summary)      shift; cmd_summary "$@" ;;
  queue)        shift; cmd_queue "$@" ;;
  stuck)        shift; cmd_stuck "$@" ;;
  calibrate)    shift; cmd_calibrate "$@" ;;
  exempt-add)   shift; cmd_exempt_add "$@" ;;
  exempt-list)  shift; cmd_exempt_list "$@" ;;
  exempt-rm)    shift; cmd_exempt_rm "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
