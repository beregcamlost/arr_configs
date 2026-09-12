#!/bin/bash
# Estante de idioma al instante (12-sep-2026). Radarr/Sonarr lo llaman como Custom
# Script al importar. Antes lo recien importado solo se enlazaba en el cron de las
# 05:50Z, o sea que una pelicula bajada a las 15:00 no existia en Emby hasta el dia
# siguiente (Beren: "hay peliculas que no refrescaron o no se agregaron").
#
# Espera un minuto (que terminen el import, el enqueue del codec y los hermanos del
# mismo pack) y corre es_shelf.py bajo el MISMO flock del cron: si ya hay una corrida
# en marcha, esta se salta y el cron de respaldo (*/15) recoge lo que falte. Se
# desprende con setsid: nunca bloquea al *arr ni muere con el.
set -u
LOG=/config/berenstuff/automation/logs/es_shelf.log
EV="${radarr_eventtype:-${sonarr_eventtype:-}}"
case "$EV" in
  Test) echo "es_shelf_hook ok"; exit 0 ;;
  Download|Upgrade|ImportComplete|"") ;;
  *) exit 0 ;;
esac
TITULO="${radarr_movie_title:-${sonarr_series_title:-?}}"
export EV TITULO
setsid nohup bash -c 'sleep 60; set -a; . /config/berenstuff/.env; set +a; echo "== $(date -u +%FT%TZ) hook $EV: $TITULO"; exec /usr/bin/flock -n /tmp/es_shelf.lock /usr/bin/python3 /config/berenstuff/automation/scripts/streaming/es_shelf.py' >> "$LOG" 2>&1 < /dev/null &
exit 0
