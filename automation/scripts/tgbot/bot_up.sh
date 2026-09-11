#!/bin/bash
# Vigilante del bot de Telegram (pipeflix_bot.py): start|stop|restart|status
# cron: cada 5 min `start` (no hace nada si ya corre) + @reboot.
set -u
REPO=/config/berenstuff
PY=/config/.venv-tgbot/bin/python
BOT=$REPO/automation/scripts/tgbot/pipeflix_bot.py
LOG=$REPO/automation/logs/telegram_bot.log
PID=/tmp/pipeflix_bot.pid

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

start() {
  if running; then [ "${1:-}" = "-v" ] && echo "ya corre (pid $(cat "$PID"))"; return 0; fi
  set -a; . "$REPO/.env"; set +a
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "$(date -Is) sin TELEGRAM_BOT_TOKEN en .env; no arranco" >> "$LOG"; return 1
  fi
  cd "$REPO/automation/scripts" || return 1
  PYTHONPATH=$REPO/automation/scripts setsid nohup "$PY" "$BOT" >> "$LOG" 2>&1 < /dev/null &
  echo "$(date -Is) arrancando (pid $!)" >> "$LOG"
  for _ in $(seq 1 30); do running && break; sleep 1; done  # Telegram tarda ~20 s en iniciar
  running && echo "arrancado (pid $(cat "$PID"))" || { echo "no arranco, mira $LOG"; tail -5 "$LOG"; return 1; }
}

stop() {
  if running; then kill "$(cat "$PID")"; sleep 2; running && kill -9 "$(cat "$PID")"; echo "detenido"; fi
  rm -f "$PID"
}

case "${1:-start}" in
  start)   start "${2:-}" ;;
  stop)    stop ;;
  restart) stop; start -v ;;
  status)  running && echo "corre (pid $(cat "$PID"))" || { echo "parado"; exit 1; } ;;
  *) echo "uso: $0 start|stop|restart|status"; exit 2 ;;
esac
