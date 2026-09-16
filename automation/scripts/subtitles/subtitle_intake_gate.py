#!/usr/bin/env python3
"""Capa 0 de intake para subtitulos ES (2026-07-04).

Puntua cada .es.srt modificado recientemente (proveedores de Bazarr, extraccion
embebida o traduccion) con los detectores estructurales de scan2 (scan2_lib.py,
copia sincronizada del canonico en berentendo D:\\emby\\subtitle-pipeline\\
overnight\\scan2.py). Si el archivo sale RED/SEVERE y hay fuente EN al lado, lo
renombra a .es.srt.intake-rejected-<stamp> y pide a Bazarr un scan-disk de la
serie/pelicula (2026-09-16): translator.py (lane del media_pipeline) lee
missing_subtitles de Bazarr, y sin el scan-disk Bazarr seguia creyendo que el
ES existia (Reacher S04E08, 12 h sin ES). Si a las 3 h no hay reposicion,
avisa una vez por log + Discord (ALERT_MISSING_ES).

Salvaguardas:
  - keep-local (manifests de Sonarr/Radarr + manuales) JAMAS se toca.
  - 1 solo rechazo automatico por archivo: si la reposicion tambien sale
    RED/SEVERE se loguea ESCALATE y se deja en paz (evita bucles con Bazarr).
  - Sin EN hermano no hay rechazo (no habria con que retraducir) — solo log.

Uso: subtitle_intake_gate.py [--hours 26] [--dry-run] [--verbose]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import scan2_lib  # noqa: E402

PATH_PREFIX = "/APPBOX_DATA/storage/media"
STATE_DIR = "/APPBOX_DATA/storage/.subtitle-intake-state"
STATE_FILE = os.path.join(STATE_DIR, "intake_gate_state.json")
KEEP_LOCAL = os.path.join(STATE_DIR, "keep_local.json")


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


ENV_FILE = "/config/berenstuff/.env"
PENDING_ALERT_HOURS = 3.0   # ES rechazado y aun sin reposicion -> alerta (una vez)


def load_env(path=ENV_FILE):
    """Lee las lineas 'export K=V' del .env del pipeline (sin ejecutar nada)."""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export ") and "=" in line:
                    k, v = line[7:].split("=", 1)
                    env[k.strip()] = v.split(" #")[0].strip().strip("'\"")
    except Exception as e:
        log("WARN: no pude leer %s: %s" % (path, e))
    return env


def bazarr_scan_disk(env, video_base):
    """Pide a Bazarr un scan-disk de la serie/pelicula del archivo rechazado.

    Sin esto Bazarr sigue creyendo que el ES existe (missing_subtitles=[]) y
    translator.py --since, que consulta ESA tabla, nunca ve el hueco: Reacher
    S04E08 estuvo 12 h sin ES tras el rechazo de las 09:00 (2026-09-16).
    """
    import sqlite3
    import urllib.request
    url, key, db = env.get("BAZARR_URL"), env.get("BAZARR_API_KEY"), env.get("BAZARR_DB_RO") or env.get("BAZARR_DB")
    if not (url and key and db):
        return "sin BAZARR_URL/API_KEY/DB en .env"
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        prefix = video_base + "."
        row = conn.execute("SELECT sonarrSeriesId FROM table_episodes WHERE substr(path,1,?)=? LIMIT 1",
                           (len(prefix), prefix)).fetchone()
        if row:
            query = "/api/series?seriesid=%d&action=scan-disk" % row[0]
        else:
            row = conn.execute("SELECT radarrId FROM table_movies WHERE substr(path,1,?)=? LIMIT 1",
                               (len(prefix), prefix)).fetchone()
            if not row:
                conn.close()
                return "no encontrado en Bazarr DB"
            query = "/api/movies?radarrid=%d&action=scan-disk" % row[0]
        conn.close()
        # Bazarr 1.6: PATCH con query params (POST /api/series/action responde 405)
        req = urllib.request.Request(url.rstrip("/") + query, method="PATCH",
                                     headers={"X-API-KEY": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return "bazarr scan-disk %s http=%d" % (query, r.status)
    except Exception as e:
        return "bazarr scan-disk FALLO: %s" % e


def discord(env, msg):
    hook = env.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return False
    try:
        import urllib.request
        req = urllib.request.Request(hook, data=json.dumps({"content": msg[:1900]}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "intake-gate"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        log("WARN: discord fallo: %s" % e)
        return False


def check_pending_rejections(state, env, dry_run):
    """Rechazados cuya reposicion no llego: si el .es.srt sigue sin existir tras
    PENDING_ALERT_HOURS, avisa UNA vez (log + Discord) en vez de callar para siempre."""
    n = 0
    for path, rec in state.items():
        if rec.get("action") != "rejected" or rec.get("alerted") or os.path.exists(path):
            continue
        try:
            when = time.mktime(time.strptime(rec.get("when", ""), "%Y-%m-%d %H:%M"))
        except Exception:
            continue
        age_h = (time.time() - when) / 3600.0
        if age_h < PENDING_ALERT_HOURS:
            continue
        msg = ("ALERT_MISSING_ES %s: rechazado hace %.1f h y el translator no lo ha repuesto "
               "(revisar lane translation / Bazarr)" % (os.path.basename(path), age_h))
        log(msg)
        if not dry_run:
            rec["alerted"] = True
            discord(env, ":rotating_light: intake-gate — " + msg)
        n += 1
    return n


def recent_es_files(prefix, hours):
    r = subprocess.run(
        ["find", prefix, "-name", "*.es.srt", "-mmin", "-%d" % int(hours * 60)],
        capture_output=True, text=True, timeout=600)
    return [l for l in r.stdout.splitlines() if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=26.0,
                    help="ventana de 'reciente' (default 26 h; cron */30 re-ve todo)")
    ap.add_argument("--path-prefix", default=PATH_PREFIX)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    os.makedirs(STATE_DIR, exist_ok=True)
    state = load_json(STATE_FILE, {})
    keep_doc = load_json(KEEP_LOCAL, {"paths": []})
    keep = [e["path"].rstrip("/") + "/" for e in keep_doc.get("paths", [])]
    if not keep:
        log("WARN: manifiesto keep-local vacio o ausente (%s) — sin exenciones" % KEEP_LOCAL)

    env = load_env()
    n_alert = check_pending_rejections(state, env, a.dry_run)
    files = recent_es_files(a.path_prefix, a.hours)
    stamp = time.strftime("%Y%m%d-%H%M")
    n_ok = n_rej = n_esc = n_skip = n_keep = n_qe = 0

    for path in files:
        if any(path.startswith(k) for k in keep):
            n_keep += 1
            continue
        # QE manda sobre capa 0: si existe <path>.qe-rejected-* es que la capa 2
        # (CometKiwi) ya comparo este sub contra el champion y ESTE gano —
        # rechazarlo por heuristicas estructurales desharia esa decision
        # (leccion: Grease/Hachi/Lie to Me, reverts QE del 2026-07-03).
        # glob.escape: los corchetes de nombres tipo "[Spanish]"/"[x264 AAC]" son
        # wildcards de glob y sin escape el marcador no matchea (bug Hachi 07-04)
        if glob.glob(glob.escape(path) + ".qe-rejected-*"):
            n_qe += 1
            if a.verbose:
                log("QE_LOCKED %s (capa 2 eligio este sub)" % os.path.basename(path))
            continue
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        if time.time() - st.st_mtime < 600:
            continue  # ventana de asentamiento: puede ser un batch en curso
                      # (subredo/translator escribiendo); el proximo run lo ve
        sig = "%d:%d" % (int(st.st_mtime), st.st_size)
        rec = state.get(path, {})
        if rec.get("sig") == sig:
            n_skip += 1
            continue

        base = path[:-7]
        en_path = next((base + s for s in (".en.srt", ".en.sdh.srt")
                        if os.path.exists(base + s)), None)
        try:
            es_text = open(path, encoding="utf-8", errors="replace").read()
            en_text = (open(en_path, encoding="utf-8", errors="replace").read()
                       if en_path else None)
            en_mtime = os.stat(en_path).st_mtime if en_path else None
            sev, flags, ncues, off = scan2_lib.score(
                es_text, en_text, es_mtime=st.st_mtime, en_mtime=en_mtime)
        except Exception as e:
            log("ERROR puntuando %s: %s" % (os.path.basename(path), e))
            continue

        rec.update({"sig": sig, "sev": sev, "flags": flags,
                    "when": time.strftime("%Y-%m-%d %H:%M")})
        name = os.path.basename(path)

        if sev in ("RED", "SEVERE"):
            if not en_path:
                rec["action"] = "needs_en"
                log("NEEDS_EN [%s] %s — %s (sin EN, no se puede retraducir)"
                    % (sev, name, ",".join(flags)))
            elif rec.get("rejected_count", 0) >= 1:
                rec["action"] = "escalate"
                n_esc += 1
                log("ESCALATE [%s] %s — %s (la reposicion tambien salio mal; "
                    "revisar a mano)" % (sev, name, ",".join(flags)))
            else:
                rec["rejected_count"] = rec.get("rejected_count", 0) + 1
                rec["action"] = "rejected"
                n_rej += 1
                if a.dry_run:
                    log("DRY-RUN rechazaria [%s] %s — %s" % (sev, name, ",".join(flags)))
                else:
                    os.rename(path, path + ".intake-rejected-" + stamp)
                    log("REJECTED [%s] %s — %s -> translator lo rehace del EN"
                        % (sev, name, ",".join(flags)))
                    log("  %s" % bazarr_scan_disk(env, base))
        else:
            rec["action"] = "ok"
            n_ok += 1
            if a.verbose:
                log("OK [%s] %s %s" % (sev, name, ",".join(flags)))

        state[path] = rec

    if not a.dry_run:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=0)
    log("resumen: recientes=%d ok=%d rechazados=%d escalados=%d keep_local=%d "
        "qe_locked=%d sin_cambio=%d alertas_pendientes=%d%s"
        % (len(files), n_ok, n_rej, n_esc, n_keep, n_qe, n_skip, n_alert,
           " (DRY-RUN)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
