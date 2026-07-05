#!/usr/bin/env python3
"""Capa 0 de intake para subtitulos ES (2026-07-04).

Puntua cada .es.srt modificado recientemente (proveedores de Bazarr, extraccion
embebida o traduccion) con los detectores estructurales de scan2 (scan2_lib.py,
copia sincronizada del canonico en berentendo D:\\emby\\subtitle-pipeline\\
overnight\\scan2.py). Si el archivo sale RED/SEVERE y hay fuente EN al lado, lo
renombra a .es.srt.intake-rejected-<stamp>: translator.py (lane del
media_pipeline) ve el ES faltante y lo rehace desde el EN en <=30 min.

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
        "qe_locked=%d sin_cambio=%d%s" % (len(files), n_ok, n_rej, n_esc, n_keep,
                                          n_qe, n_skip, " (DRY-RUN)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
