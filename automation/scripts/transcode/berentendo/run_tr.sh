#!/usr/bin/env bash
# Traduce los .en.srt de fixwork/in_tr a .es.srt en fixwork/out_tr usando el campeon
# CT2 en la 3090. Las libs de CUDA vienen dentro del venv (paquetes nvidia-*-cu12),
# no del sistema, asi que hay que ponerlas en LD_LIBRARY_PATH a mano.
set -euo pipefail

VENV=/mnt/d/emby/ct2-venv
NV="$VENV/lib/python3.12/site-packages/nvidia"

LIBS=""
for d in "$NV"/*/lib; do
  [ -d "$d" ] && LIBS="$LIBS:$d"
done
export LD_LIBRARY_PATH="${LIBS#:}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
echo "LD_LIBRARY_PATH tiene $(echo "$LD_LIBRARY_PATH" | tr ':' '\n' | grep -c nvidia) rutas nvidia"

cd /mnt/d/emby/fixwork
find in_tr -name '*.srt' -print0 | xargs -0 "$VENV/bin/python" srt_en_es.py --dir-salida out_tr
echo "--- resultado ---"
ls -la out_tr
