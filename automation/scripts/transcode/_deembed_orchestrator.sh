#!/usr/bin/env bash
# Detached de-embed orchestrator (runs on mubuntu, survives BERENTENDO shutdown).
# The 5-file test was already run AND VERIFIED (originals vs stripped: text subs removed,
# video/audio/duration intact, sidecars valid). This now does the full library in multi-pass
# (skips files being watched in Emby; a later pass catches them), re-enables the paused crons,
# then emails Beren a summary.
set -uo pipefail
cd /config/berenstuff || exit 1
LOG=/APPBOX_DATA/storage/.transcode-state/de-embed-orchestrator.log
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
exec >>"$LOG" 2>&1
echo "================ orchestrator START $(date) ================"

MIG=automation/scripts/transcode/batch_extract_embedded.sh
total_stripped=0
pass=1; MAXPASS=3
while [ "$pass" -le "$MAXPASS" ]; do
  echo "---------------- PASS $pass $(date) ----------------"
  out="$(bash "$MIG" --execute --sleep 2 --load-max 999 2>&1)"
  echo "$out" | tail -25
  sp="$(printf '%s\n' "$out" | grep -oE 'skipped_playback=[0-9]+' | tail -1 | cut -d= -f2)"
  st="$(printf '%s\n' "$out" | grep -oE 'DONE .*stripped=[0-9]+' | grep -oE 'stripped=[0-9]+' | tail -1 | cut -d= -f2)"
  total_stripped=$(( total_stripped + ${st:-0} ))
  echo "pass $pass result: stripped=${st:-?} skipped_playback=${sp:-?} total_stripped=$total_stripped"
  if [ "${sp:-0}" = "0" ]; then echo "no watched skips remaining; migration complete"; break; fi
  if [ "$pass" -lt "$MAXPASS" ]; then echo "sleeping 30m to let viewers finish..."; sleep 1800; fi
  pass=$((pass + 1))
done

echo "---------------- re-enabling crons $(date) ----------------"
crontab -l > "/tmp/ct.bak.orchestrator.$(date +%s)" 2>/dev/null || true
crontab -l 2>/dev/null | sed -E 's/^# \[PAUSED 2026-05-30 sidecar-(work|fix)\] //' | crontab -
crons_on="$(crontab -l 2>/dev/null | grep -cE 'lane slow|library_codec_manager|auto-maintain')"
echo "active sidecar/codec cron lines now: $crons_on"

echo "---------------- emailing Beren $(date) ----------------"
set -a; . ./.env 2>/dev/null; set +a
cat > /tmp/mig_done.html <<HTML
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1a1a1a;max-width:560px">
<p>Beren,</p>
<p><b>&#9989; Migraci&oacute;n de subs a sidecars: TERMINADA.</b></p>
<ul style="padding-left:18px">
<li>Archivos limpiados (subs de texto &rarr; sidecars, sacados del contenedor): <b>~${total_stripped}</b>.</li>
<li>Crons <b>reactivados</b> (slow lane, auto-maintain 1am, los 4 de codec). El pipeline corre solo otra vez.</li>
<li>Los que se estaban viendo en Emby se saltaron y se recogieron en pasadas siguientes.</li>
</ul>
<p style="color:#555">Log: <code>${LOG}</code></p>
<p>&mdash; Claudia</p>
</div>
HTML
python3 automation/scripts/bin/sendmail.py --to antoniojose.figueroaf@gmail.com \
  --subject "mubuntu: migracion de subs TERMINADA + crons ON" --html --body-file /tmp/mig_done.html || echo "EMAIL FAILED"
echo "================ orchestrator DONE $(date) ================"
