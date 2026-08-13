#!/usr/bin/env bash
# Lanzador del worker de idioma. ctranslate2 4.8 hace dlopen de libcublas/libcudnn en
# tiempo de import, asi que LD_LIBRARY_PATH tiene que estar puesto ANTES de arrancar
# python: exportarlo desde dentro del proceso no sirve.
set -uo pipefail
VENV=/root/lid-venv
PY="$VENV/bin/python"
SITE="$("$PY" -c 'import site; print(site.getsitepackages()[0])')"
NV_LIBS="$(find "$SITE/nvidia" -name 'lib' -type d 2>/dev/null | tr '\n' ':')"
export LD_LIBRARY_PATH="${NV_LIBS}${LD_LIBRARY_PATH:-}"
exec "$PY" /mnt/d/emby/langid/langid_worker.py "$@"
