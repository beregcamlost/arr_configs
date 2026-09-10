#!/usr/bin/env python3
"""bif_huerfanos.py (2026-09-10) — barre los previews (.bif) cuyo video ya no existe.

EL PROBLEMA: Emby guarda el preview de cada video junto al archivo
(<base>-320-10.bif, SaveLocalThumbnailSets). Cuando el video se va, el .bif se queda.
El 10-sep habia 344 (0.76 GB): 322 de mejoras de calidad de Radarr/Sonarr y de
TEMPORALES del pipeline (.subtmp, .collisiontmp) a los que Emby alcanzo a sacarles
preview mientras existian, y 22 en carpetas que ya no tenian video.

QUE BORRA: solo archivos <base>-<ancho>-<intervalo>.bif que cumplen TODO:
  1. ningun video hermano tiene ese mismo nombre base,
  2. ningun item vivo de Emby apunta a ese video (se sigue el enlace: las bibliotecas se
     publican sobre los arboles /virtual y el archivo real vive en /media),
  3. lleva mas de GRACIA_HORAS sin tocarse (un temporal a medio renombrar no cuenta),
  4. la corrida entera no pasa de TOPE. Si pasa, no borra NADA y avisa: un barrido masivo
     casi siempre es que algo dejo de ver los videos, no que desaparecieran.
Si Emby no responde, tampoco borra nada. Cada corrida con --apply deja un manifiesto.

Uso:  bif_huerfanos.py [--apply]
"""
import json, os, re, sys, time, urllib.parse, urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
MANIFIESTOS = "/config/berenstuff/automation/backups/bif-huerfanos"
VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".webm", ".mpg", ".mpeg", ".wmv",
         ".flv", ".vob", ".iso", ".strm", ".m2ts", ".mts", ".divx", ".ogm", ".3gp"}
BIF = re.compile(r"-\d+-\d+\.bif$", re.I)
GRACIA_HORAS = 24
TOPE = 200


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


def videos_de_emby():
    """Rutas reales (sin extension) de todo video que Emby tiene vivo."""
    q = urllib.parse.urlencode({"api_key": os.environ["EMBY_API_KEY"], "Recursive": "true",
                                "IncludeItemTypes": "Movie,Episode", "Fields": "Path", "Limit": 100000})
    url = os.environ["EMBY_URL"].rstrip("/") + "/emby/Items?" + q
    with urllib.request.urlopen(url, timeout=300) as r:
        items = json.loads(r.read())["Items"]
    return {os.path.splitext(os.path.realpath(i["Path"]))[0] for i in items if i.get("Path")}


def main():
    aplicar = "--apply" in sys.argv
    limite = time.time() - GRACIA_HORAS * 3600
    candidatos = []
    for dp, _dn, fn in os.walk(MEDIA_ROOT):
        bases = {os.path.splitext(f)[0] for f in fn if os.path.splitext(f)[1].lower() in VIDEO}
        for f in fn:
            if not BIF.search(f) or BIF.sub("", f) in bases:
                continue
            p = os.path.join(dp, f)
            if os.path.getmtime(p) < limite:
                candidatos.append(p)
    try:
        vivos = videos_de_emby()
    except Exception as e:
        log(f"Emby no responde ({e}): no borro nada")
        return 2
    choques = [p for p in candidatos if BIF.sub("", p) in vivos]
    huerfanos = [p for p in candidatos if p not in choques]
    peso = sum(os.path.getsize(p) for p in huerfanos)
    log(f"huerfanos: {len(huerfanos)} ({peso / 1e6:.1f} MB) | con item vivo en Emby (no se tocan): {len(choques)}")
    for p in huerfanos[:5]:
        log("  " + p[len(MEDIA_ROOT) + 1:])
    if len(huerfanos) > TOPE:
        log(f"AVISO: {len(huerfanos)} pasa el tope de {TOPE}. No borro nada: revisar a mano")
        return 3
    if not aplicar:
        log("(simulacro: nada borrado. Repetir con --apply)")
        return 0
    if huerfanos:
        os.makedirs(MANIFIESTOS, exist_ok=True)
        man = os.path.join(MANIFIESTOS, time.strftime("%Y%m%d_%H%M%S") + ".json")
        with open(man, "w") as f:
            json.dump(huerfanos, f, indent=0)
        for p in huerfanos:
            os.remove(p)
        log(f"borrados {len(huerfanos)} -> manifiesto {man}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
