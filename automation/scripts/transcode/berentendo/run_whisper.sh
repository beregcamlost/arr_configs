#!/usr/bin/env bash
# Transcribe con faster-whisper large-v3 en la 3090. Las libs de CUDA vienen dentro del
# venv (paquetes nvidia-*-cu12), no del sistema: sin esto, "libcudnn/libcublas not found".
set -uo pipefail

VENV=/mnt/d/emby/whisper-venv
# El venv de whisper NO trae los paquetes nvidia-*-cu12; el de CT2 si, y son las
# mismas libs de CUDA 12 (ambos usan ctranslate2 4.8.x). Sin esto el modelo CARGA
# bien pero revienta al computar: "libcublas.so.12 is not found".
NV="/mnt/d/emby/ct2-venv/lib/python3.12/site-packages/nvidia"
LIBS=""
for d in "$NV"/*/lib; do
  [ -d "$d" ] && LIBS="$LIBS:$d"
done
export LD_LIBRARY_PATH="${LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

LISTA="${1:-/mnt/d/emby/whisper-staging/hh.txt}"
VENTANA="${2:-26}"

echo "=== arranque $(date +%H:%M:%S) | ventana ${VENTANA} min ==="
"$VENV/bin/python" /mnt/d/emby/whisper-staging/whisper_es.py "$LISTA" "$VENTANA"
rc=$?
echo "=== fin $(date +%H:%M:%S) rc=$rc ==="
exit $rc
