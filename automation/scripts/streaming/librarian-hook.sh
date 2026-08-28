#!/bin/bash
# Invocado por Radarr/Sonarr como Custom Script tras importar.
# Carga las credenciales y le pasa el evento al bibliotecario.
set -u
source /config/berenstuff/.env
exec /usr/bin/python3 /config/berenstuff/automation/scripts/streaming/librarian.py on-import --apply
