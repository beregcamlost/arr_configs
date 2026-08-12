#!/usr/bin/env bash
# prune_3090_backups.sh (2026-08-11, a pedido de Beren: podar cada 7 dias)
# Borra los originales pre-conversion que deja tanda3090.py en
# /APPBOX_DATA/storage/.transcode-3090-backups, PERO solo cuando puede comprobar
# que el archivo convertido existe en la biblioteca y esta sano:
#   1. el backup tiene mas de GRACE_DAYS dias
#   2. existe una fila viva en media_files con el mismo nombre base
#   3. ese archivo existe en disco y ffprobe le ve video h264 + audio aac
# Si algo no cuadra, NO borra y lo registra. Nunca borra a ciegas por edad.
set -uo pipefail
BAK_DIR="${BAK_DIR:-/APPBOX_DATA/storage/.transcode-3090-backups}"
DB="${CODEC_DB:-/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db}"
GRACE_DAYS="${GRACE_DAYS:-7}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

[ -d "$BAK_DIR" ] || exit 0
ts() { date '+%F %T'; }
sqesc() { printf '%s' "$1" | sed "s/'/''/g"; }

deleted=0 kept=0 freed=0
while IFS= read -r bak; do
  [ -f "$bak" ] || continue
  name="$(basename "$bak")"
  stem="${name%.*}"
  cur="$(sqlite3 -cmd '.timeout 20000' "$DB" \
    "SELECT path FROM media_files WHERE deleted_at IS NULL AND path LIKE '%/$(sqesc "$stem").%' LIMIT 1;" 2>/dev/null)"
  if [ -z "$cur" ]; then
    echo "$(ts) KEEP (sin fila en media_files): $name"; kept=$((kept+1)); continue
  fi
  if [ ! -f "$cur" ]; then
    echo "$(ts) KEEP (el convertido no esta en disco): $cur"; kept=$((kept+1)); continue
  fi
  codecs="$(ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 "$cur" 2>/dev/null)"
  if ! grep -q '^h264,video' <<<"$codecs" || ! grep -q '^aac,audio' <<<"$codecs"; then
    echo "$(ts) KEEP (convertido no verifica h264+aac): $cur"; kept=$((kept+1)); continue
  fi
  sz="$(stat -c '%s' "$bak" 2>/dev/null || echo 0)"
  if [ "$DRY_RUN" = "1" ]; then
    echo "$(ts) [DRY] borraria ($((sz/1048576)) MB): $name"
  else
    rm -f "$bak" && echo "$(ts) DELETE ($((sz/1048576)) MB): $name"
  fi
  deleted=$((deleted+1)); freed=$((freed+sz))
done < <(find "$BAK_DIR" -type f -mtime +"$GRACE_DAYS" 2>/dev/null | sort)

find "$BAK_DIR" -type d -empty -delete 2>/dev/null
printf '%s resumen: %s borrados (%s MB liberados), %s conservados, grace=%sd%s\n' \
  "$(ts)" "$deleted" "$((freed/1048576))" "$kept" "$GRACE_DAYS" \
  "$([ "$DRY_RUN" = 1 ] && echo ' [DRY-RUN]')"
