#!/usr/bin/env bash
# lang_id.sh (2026-08-13) — identificacion de idioma del audio sin etiquetar.
#
# POR QUE: 653 archivos de la libreria tienen la pista de audio como `und`. El filtro
# nativo de Emby (AudioLanguages=spa) solo ve lo etiquetado, asi que ~la mitad de la
# libreria es invisible para cualquier filtro por idioma. Radarr/Sonarr renombran y
# borran los "Latino"/"Castellano" del release, y ffprobe no puede inferirlo.
#
# TOPOLOGIA (la misma del subtitle-translator): mubuntu guarda y extrae muestras de
# audio (I/O liviano, permitido), berentendo/3090 hace la inferencia (faster-whisper),
# y mubuntu aplica la etiqueta IN-PLACE. Si berentendo esta apagado la cola espera:
# nada se rompe.
#
# APLICACION IN-PLACE (cero reescritura, verificado 2026-08-13):
#   mkv -> mkvpropedit  |  mp4 -> MP4Box -lang  (0 bytes de delta, 0 segundos)
#   avi/ts -> NO se tocan aqui: el pipeline de codecs ya los remuxea a mp4 y les
#             inyecta la etiqueta en ese mismo paso (sin I/O extra).
set -uo pipefail

STATE_DIR="${STATE_DIR:-/APPBOX_DATA/storage/.transcode-state-media}"
DB="${CODEC_DB:-$STATE_DIR/library_codec_state.db}"
SAMPLE_DIR="${LANGID_SAMPLE_DIR:-$STATE_DIR/langid-samples}"
LOG_FILE="${LANGID_LOG:-$STATE_DIR/langid.log}"
MIN_PROB="${LANGID_MIN_PROB:-0.70}"
SAMPLE_SECS="${LANGID_SAMPLE_SECS:-30}"

log() { printf '%s [%s] %s\n' "$(date '+%F %T')" "$1" "$2" | tee -a "$LOG_FILE" >&2; }
dbq() { sqlite3 -cmd '.timeout 30000' "$DB" "$@"; }

init_schema() {
  dbq "
CREATE TABLE IF NOT EXISTS audio_lang_detect (
  media_id     INTEGER NOT NULL,
  stream_index INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  lang         TEXT,
  prob         REAL,
  claimed_by   TEXT,
  detected_at  TEXT,
  applied_at   TEXT,
  error        TEXT,
  PRIMARY KEY (media_id, stream_index)
);
CREATE INDEX IF NOT EXISTS idx_ald_status ON audio_lang_detect(status);
"
}

# Encola las pistas de audio sin idioma util. Idempotente (INSERT OR IGNORE).
cmd_enqueue() {
  init_schema
  local only_media="${1:-}"
  local filter=""
  [[ -n "$only_media" ]] && filter="AND mf.id = $only_media"
  dbq "
INSERT OR IGNORE INTO audio_lang_detect(media_id, stream_index, status)
SELECT ps.media_id, ps.stream_index, 'pending'
FROM probe_streams ps
JOIN media_files mf ON mf.id = ps.media_id
WHERE ps.stream_type = 'audio'
  AND mf.deleted_at IS NULL
  AND COALESCE(NULLIF(TRIM(ps.language), ''), 'und') = 'und'
  $filter;"
  local n
  n="$(dbq "SELECT COUNT(*) FROM audio_lang_detect WHERE status='pending';")"
  log "info" "enqueue: $n pistas pendientes"
}

# Extrae UNA muestra que concatena 5 ventanas repartidas por todo el metraje.
#
# POR QUE 5 Y NO 1: con una sola ventana la deteccion se cae con peliculas de mucha
# musica o poco dialogo. Prueba real del 2026-08-13: "A Ghost Story" (casi muda) dio
# "nn" con 0.915 de confianza, y "A Serbian Film" dio ru vs en en dos ventanas. Cinco
# ventanas + voto ponderado en el worker sobreviven a que 1-2 caigan en silencio.
#
# UNA SOLA LLAMADA A FFMPEG (5 inputs + filtro concat) en vez de 5: mubuntu tiene
# 2 vCPU compartidos y el cron de conversion corre en paralelo; 566 pistas x 5 procesos
# serian ~2800 arranques de ffmpeg.
cmd_extract() {
  local media_id="$1" stream_index="$2"
  local path
  path="$(dbq "SELECT path FROM media_files WHERE id=$media_id;")"
  [[ -n "$path" && -f "$path" ]] || { echo "ERR no_file"; return 1; }

  local dur
  dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$path" 2>/dev/null)"
  dur="${dur%%.*}"
  [[ "${dur:-0}" -gt 0 ]] || { echo "ERR no_duration"; return 1; }

  mkdir -p "$SAMPLE_DIR"
  local out="$SAMPLE_DIR/${media_id}_${stream_index}.ogg"
  rm -f "$out"

  local -a inputs=() labels=()
  local i=0 pos
  for pct in 20 35 50 65 80; do
    pos=$(( dur * pct / 100 ))
    inputs+=(-ss "$pos" -t "$SAMPLE_SECS" -i "$path")
    labels+=("[${i}:${stream_index}]")
    i=$((i + 1))
  done

  # IFS vacio para que las etiquetas queden pegadas ("[0:1][1:1]..."): con el IFS por
  # defecto se separan con espacios y ffmpeg rechaza el filtro.
  local filter joined
  joined="$(IFS=; printf '%s' "${labels[*]}")"
  filter="${joined}concat=n=${i}:v=0:a=1[out]"
  if ffmpeg -nostdin -v error -y "${inputs[@]}" \
       -filter_complex "$filter" -map "[out]" \
       -ac 1 -ar 16000 -c:a libopus -b:a 16k -f ogg "$out" 2>/dev/null && [[ -s "$out" ]]; then
    echo "OK ${i}"
    return 0
  fi
  echo "ERR extract_failed"
  return 1
}

# Aplica la etiqueta detectada al contenedor, IN-PLACE.
cmd_apply() {
  init_schema
  local limit="${1:-0}"
  local limit_sql=""
  [[ "$limit" -gt 0 ]] && limit_sql="LIMIT $limit"
  local applied=0 failed=0 skipped=0

  while IFS=$'\t' read -r media_id stream_index lang prob path container; do
    [[ -n "$media_id" ]] || continue
    if [[ ! -f "$path" ]]; then
      dbq "UPDATE audio_lang_detect SET status='failed', error='file_gone' WHERE media_id=$media_id AND stream_index=$stream_index;"
      failed=$((failed + 1)); continue
    fi

    local rc=1
    case "$container" in
      mkv)
        # mkvpropedit numera las pistas por tipo y desde 1: a1 = primera de audio.
        local a_ord
        a_ord="$(dbq "SELECT COUNT(*) FROM probe_streams WHERE media_id=$media_id AND stream_type='audio' AND stream_index <= $stream_index;")"
        mkvpropedit "$path" --edit track:a${a_ord} --set language="$lang" >/dev/null 2>&1 && rc=0
        ;;
      mp4|m4v)
        # MP4Box numera 1..N sobre TODAS las pistas (video incluido), de ahi el +1.
        local t_ord
        t_ord="$(dbq "SELECT COUNT(*) FROM probe_streams WHERE media_id=$media_id AND stream_index <= $stream_index AND stream_type IN ('video','audio','subtitle');")"
        MP4Box -lang "${t_ord}=${lang}" "$path" >/dev/null 2>&1 && rc=0
        ;;
      *)
        # avi/ts/otros: no hay herramienta de etiquetado in-place. NO se cambia el
        # status a proposito: el pipeline de codecs los remuxea a mp4 tarde o temprano
        # y entonces esta misma pasada los agarra. Marcarlos aqui los congelaria.
        skipped=$((skipped + 1)); continue
        ;;
    esac

    if [[ "$rc" -eq 0 ]]; then
      # Verificacion real: releer del archivo, no confiar en el codigo de salida.
      local now
      now="$(ffprobe -v error -select_streams "$stream_index" -show_entries stream_tags=language -of csv=p=0 "$path" 2>/dev/null | head -1)"
      if [[ "$now" == "$lang" ]]; then
        dbq "
UPDATE audio_lang_detect SET status='applied', applied_at=CURRENT_TIMESTAMP WHERE media_id=$media_id AND stream_index=$stream_index;
UPDATE probe_streams SET language='$lang' WHERE media_id=$media_id AND stream_index=$stream_index;"
        applied=$((applied + 1))
      else
        dbq "UPDATE audio_lang_detect SET status='failed', error='verify_mismatch_got_${now:-empty}' WHERE media_id=$media_id AND stream_index=$stream_index;"
        failed=$((failed + 1))
      fi
    else
      dbq "UPDATE audio_lang_detect SET status='failed', error='tool_failed' WHERE media_id=$media_id AND stream_index=$stream_index;"
      failed=$((failed + 1))
    fi
  done < <(dbq -separator $'\t' "
SELECT d.media_id, d.stream_index, d.lang, d.prob, mf.path, mf.container
FROM audio_lang_detect d JOIN media_files mf ON mf.id = d.media_id
WHERE d.status='detected' AND d.lang IS NOT NULL AND d.prob >= $MIN_PROB
  AND mf.deleted_at IS NULL
ORDER BY d.media_id $limit_sql;")

  log "info" "apply: aplicados=$applied fallidos=$failed diferidos=$skipped"
  echo "applied=$applied failed=$failed deferred=$skipped"
}

cmd_status() {
  init_schema
  echo "--- estado de la cola de idioma ---"
  dbq -separator $'\t' "SELECT status, COUNT(*) FROM audio_lang_detect GROUP BY status ORDER BY 2 DESC;"
  echo "--- idiomas detectados ---"
  dbq -separator $'\t' "SELECT lang, COUNT(*), ROUND(AVG(prob),3) FROM audio_lang_detect WHERE lang IS NOT NULL GROUP BY lang ORDER BY 2 DESC;"
}

case "${1:-}" in
  enqueue) shift; cmd_enqueue "$@" ;;
  extract) shift; cmd_extract "$@" ;;
  apply)   shift; cmd_apply "$@" ;;
  status)  shift; cmd_status "$@" ;;
  *) echo "uso: $0 {enqueue [media_id]|extract <media_id> <stream_index>|apply [limit]|status}" >&2; exit 2 ;;
esac
