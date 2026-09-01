#!/usr/bin/env bash
# Paso 1 del camino de dos saltos: audio no-ingles -> .en.srt con whisper task=translate.
set -uo pipefail
VENV=/mnt/d/emby/whisper-venv
NV="/mnt/d/emby/ct2-venv/lib/python3.12/site-packages/nvidia"
LIBS=""
for d in "$NV"/*/lib; do
  [ -d "$d" ] && LIBS="$LIBS:$d"
done
export LD_LIBRARY_PATH="${LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
LISTA="${1:-/mnt/d/emby/whisper-staging/zh_whisper.txt}"
VENTANA="${2:-90}"
echo "=== arranque $(date +%H:%M:%S) | ventana ${VENTANA} min ==="
"$VENV/bin/python" /mnt/d/emby/whisper-staging/whisper_zh.py "$LISTA" "$VENTANA"
rc=$?
echo "=== fin $(date +%H:%M:%S) rc=$rc ==="
exit $rc
