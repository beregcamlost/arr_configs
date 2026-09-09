#!/usr/bin/env python3
"""Barre las carpetas que los *arr ya no reconocen y que no tienen video.

EL PROBLEMA (2026-09-09): al borrar un titulo -- a mano, o por streaming_checker --
se van los .mkv pero quedan el .nfo y el poster. Emby sigue mostrando la serie, ya
vacia, en su tile; y el bibliotecario no la ve, porque solo mira titulos que Radarr
o Sonarr todavia tienen en su base.

QUE BORRA: solo carpetas que cumplen LAS TRES:
  1. el *arr la reporta en unmappedFolders (o sea: ningun titulo suyo vive ahi),
  2. no contiene un solo archivo de video, en ningun nivel,
  3. lleva mas de GRACIA_HORAS sin tocarse (una descarga a medio importar no cuenta).
Si tiene video, se reporta y NO se toca: eso es una pelicula que perdio su ficha,
no basura, y la decide una persona.
"""
import argparse, json, os, pathlib, shutil, time, urllib.parse, urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
LOG = pathlib.Path("/config/berenstuff/automation/logs/orphan_folders.log")
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm", ".wmv", ".mpg", ".mpeg", ".iso"}
GRACIA_HORAS = 24
TOPE = 25          # el mismo criterio que el bibliotecario: nada de barridos masivos


def log(msg):
    linea = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(linea, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(linea + "\n")


def arr_get(url, key, ruta):
    req = urllib.request.Request(url.rstrip("/") + ruta, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def emby_post(ruta, **params):
    params["api_key"] = os.environ["EMBY_API_KEY"]
    url = "%s%s?%s" % (os.environ["EMBY_URL"].rstrip("/"), ruta, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status


def huerfanas():
    """Lo que Radarr y Sonarr declaran como carpeta sin dueño."""
    fuera = []
    for url, key in (("RADARR_URL", "RADARR_KEY"), ("SONARR_URL", "SONARR_KEY")):
        for raiz in arr_get(os.environ[url], os.environ[key], "/api/v3/rootfolder"):
            if not raiz.get("accessible"):
                log("  aviso: raiz inaccesible, la salto: %s" % raiz["path"])
                continue
            for f in raiz.get("unmappedFolders") or []:
                fuera.append(f["path"])
    return fuera


def tiene_video(d):
    for r, _, ficheros in os.walk(d):
        for f in ficheros:
            if pathlib.Path(f).suffix.lower() in VIDEO:
                return True
    return False


def edad_horas(d):
    mas_nuevo = max((os.path.getmtime(os.path.join(r, f))
                     for r, _, fs in os.walk(d) for f in fs), default=os.path.getmtime(d))
    return (time.time() - max(mas_nuevo, os.path.getmtime(d))) / 3600.0


def main():
    ap = argparse.ArgumentParser(description="Borra carpetas huerfanas sin video.")
    ap.add_argument("--apply", action="store_true", help="borrar (por defecto: simulacro)")
    ap.add_argument("--gracia", type=float, default=GRACIA_HORAS)
    args = ap.parse_args()

    candidatas, con_video = [], []
    for p in huerfanas():
        d = pathlib.Path(p)
        # cinturon: solo dentro de /media, y solo un nivel bajo la raiz del estante
        if not str(d).startswith(MEDIA_ROOT + "/") or len(d.relative_to(MEDIA_ROOT).parts) != 2:
            log("  [OMITIDA] fuera de sitio, no la toco: %s" % d)
            continue
        if not d.is_dir():
            continue
        if tiene_video(d):
            con_video.append(d)
            continue
        h = edad_horas(d)
        if h < args.gracia:
            log("  [ESPERA] %s (tocada hace %.1f h, gracia %.0f h)" % (d.name, h, args.gracia))
            continue
        candidatas.append(d)

    for d in con_video:
        log("  [REVISAR] %s tiene video pero ningun *arr la reclama: la dejo" % d)
    log("huerfanas sin video: %s; con video (solo aviso): %s" % (len(candidatas), len(con_video)))
    if len(candidatas) > TOPE:
        log("  tope de seguridad: %s candidatas, borro %s esta vez" % (len(candidatas), TOPE))
        candidatas = candidatas[:TOPE]

    borradas = 0
    for d in candidatas:
        resto = sorted(f.name for f in d.rglob("*") if f.is_file())[:6]
        if not args.apply:
            log("  [AVISO] borraria %s (%s)" % (d, ", ".join(resto) or "vacia"))
            continue
        try:
            shutil.rmtree(d)
            borradas += 1
            log("  [BORRADA] %s (%s)" % (d, ", ".join(resto) or "vacia"))
        except Exception as e:
            log("  [FALLO] %s: %s" % (d, e))

    if borradas:
        # Emby guarda el item de la serie aunque ya no queden archivos: sin este
        # refresco el tile sigue mostrando una serie vacia.
        log("  refresco de Emby: HTTP %s" % emby_post("/Library/Refresh"))
    if not args.apply:
        log("(simulacro: nada borrado. Repetir con --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
