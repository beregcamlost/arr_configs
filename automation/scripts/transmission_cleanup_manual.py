#!/usr/bin/env python3
"""transmission_cleanup_manual.py — borra de Transmission los torrents agregados a mano
(sin categoria radarr/sonarr) cuyo video ya fue importado por Radarr o Sonarr.

Radarr/Sonarr solo limpian los torrents que ellos mismos descargaron (tienen su hash).
Un torrent metido a mano en la web de Transmission e importado por "Manual Import"
queda como cascaron: 100%, parado, y en su carpeta solo basura del tracker.

Regla para borrar (las tres a la vez):
  1. percentDone == 1 y sin label / fuera de las carpetas de categoria de Arr
  2. algun droppedPath del historial de importaciones de Radarr o Sonarr cae dentro
     de la carpeta del torrent (mapeando /torrents/completed -> ruta local)
  3. el importedPath correspondiente existe en la biblioteca
Ademas avisa (sin tocar) los torrents estancados: <100% y sin progreso mas de --stale-days.

Uso: transmission_cleanup_manual.py [--apply] [--stale-days N]
Env (de /config/berenstuff/.env): TRANSMISSION_URL/USER/PASS, RADARR_URL/KEY, SONARR_URL/KEY
"""
import argparse, base64, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

REMOTE_ROOT = "/torrents/completed"
LOCAL_ROOT = "/APPBOX_DATA/apps/transmission.vhscave.appboxes.co/torrents/completed"
ARR_CATEGORY_DIRS = ("radarr", "sonarr")
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv")

def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

def env(k):
    v = os.environ.get(k)
    if not v:
        sys.exit(f"falta {k} en el entorno")
    return v

class Transmission:
    def __init__(self):
        self.url, user, pw = env("TRANSMISSION_URL"), env("TRANSMISSION_USER"), env("TRANSMISSION_PASS")
        self.auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        self.sid = ""
    def call(self, method, arguments=None):
        body = json.dumps({"method": method, "arguments": arguments or {}}).encode()
        for _ in range(2):
            req = urllib.request.Request(self.url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", self.auth)
            if self.sid:
                req.add_header("X-Transmission-Session-Id", self.sid)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    self.sid = e.headers.get("X-Transmission-Session-Id", "")
                    continue
                raise
        raise RuntimeError("Transmission: 409 persistente")

def arr_get(base, key, path):
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def arr_imports(base, key, max_pages=10):
    """(droppedPath, importedPath) de todos los eventos downloadFolderImported (eventType 3)."""
    out = []
    for page in range(1, max_pages + 1):
        data = arr_get(base, key, f"/api/v3/history?page={page}&pageSize=250&eventType=3&sortKey=date&sortDirection=descending")
        recs = data.get("records", [])
        for r in recs:
            d = r.get("data") or {}
            if d.get("droppedPath"):
                out.append((d["droppedPath"], d.get("importedPath") or ""))
        if len(recs) < 250:
            break
    return out

def local_dir(t):
    d = t["downloadDir"].rstrip("/")
    if d.startswith(REMOTE_ROOT):
        d = LOCAL_ROOT + d[len(REMOTE_ROOT):]
    return os.path.join(d, t["name"])

def is_manual(t):
    if t.get("labels"):
        return False
    rel = t["downloadDir"].rstrip("/").split("/")[-1]
    return rel not in ARR_CATEGORY_DIRS

def leftover_videos(path):
    found = []
    for root, _, files in os.walk(path):
        for f in files:
            if f.lower().endswith(VIDEO_EXT):
                found.append(os.path.join(root, f))
    return found

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="borrar de verdad (con datos locales)")
    ap.add_argument("--stale-days", type=int, default=7)
    a = ap.parse_args()

    tr = Transmission()
    fields = ["id", "name", "status", "percentDone", "labels", "downloadDir", "hashString", "addedDate", "activityDate", "totalSize"]
    torrents = tr.call("torrent-get", {"fields": fields})["arguments"]["torrents"]
    imports = arr_imports(env("RADARR_URL"), env("RADARR_KEY")) + arr_imports(env("SONARR_URL"), env("SONARR_KEY"))

    borrar, avisos = [], []
    now = time.time()
    for t in torrents:
        if not is_manual(t):
            continue
        tdir = local_dir(t)
        if t["percentDone"] >= 1.0:
            matches = [(d, i) for d, i in imports if d.startswith(tdir + "/") or d == tdir]
            ok = [i for _, i in matches if i and os.path.exists(i)]
            if not matches:
                avisos.append(f"[SIN IMPORTAR] {t['name']} (100%, sin rastro en historial de Arr)")
                continue
            if not ok:
                avisos.append(f"[IMPORTADO PERO NO ESTA] {t['name']} -> {matches[0][1]}")
                continue
            resto = leftover_videos(tdir) if os.path.isdir(tdir) else []
            if resto:
                avisos.append(f"[QUEDA VIDEO EN LA CARPETA, NO BORRO] {t['name']}: {resto[0]}")
                continue
            borrar.append((t, ok[0]))
        else:
            idle_days = (now - max(t.get("activityDate") or 0, t.get("addedDate") or 0)) / 86400
            if idle_days >= a.stale_days:
                avisos.append(f"[ESTANCADO {idle_days:.0f} d] {t['name']} ({t['percentDone']*100:.0f}%)")

    log(f"torrents: {len(torrents)}; manuales importados a borrar: {len(borrar)}; avisos: {len(avisos)}")
    for t, imp in borrar:
        log(f"  {'BORRADO' if a.apply else 'borraria'} #{t['id']} {t['name']}  (en biblioteca: {imp})")
    for m in avisos:
        log(f"  {m}")
    if borrar and a.apply:
        tr.call("torrent-remove", {"ids": [t["id"] for t, _ in borrar], "delete-local-data": True})
        log(f"  torrent-remove enviado ({len(borrar)}), con datos locales")
    elif borrar:
        log("  (simulacro: nada borrado. Repetir con --apply)")

if __name__ == "__main__":
    main()
