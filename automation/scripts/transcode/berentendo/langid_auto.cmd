@echo off
REM Worker de identificacion de idioma en el 3090 (2026-08-13).
REM --auto respeta el guard de GPU: si Beren esta jugando, la corrida se salta.
REM Un .cmd y no el comando directo en la tarea: evita el infierno de comillas anidadas
REM de schtasks (misma leccion que Emby-3090-Transcode).
wsl -d Ubuntu-24.04 -- bash /mnt/d/emby/langid/lid_run.sh --auto --limit 120 >> D:\emby\langid\auto.log 2>&1
