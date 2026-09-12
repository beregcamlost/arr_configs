#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Salud de la biblioteca de Emby: lo que Beren fue detectando a ojo el 11/12-sep-2026,
chequeado solo cada 15 min (pipeline_health.sh) y resumido en el informe diario.

  1. duplicados       dos items con la misma ruta (refresco concurrente con el vigilante)
  2. sin entrar       video en /media con mas de 30 min y sin item en Emby (el hook o el
                      cron de es_shelf fallaron, o Emby no lo vio)
  3. sin caratula     item sin imagen principal con mas de 2 h (DateLastRefreshed no sirve:
                      queda null en todo lo que vino de nfo local)
                      (biblioteca sin proveedores, TMDB caido, etc.)
  4. sin subs es      pelicula/episodio de 12 h a 7 dias sin subtitulo en espanol y sin
                      audio en espanol (la release no traia fuente, Bazarr no encontro)
  5. faststart        FALLO en el log de las ultimas 24 h

Salida por defecto: una linea "OK|WARN|ALARM: texto" por chequeo (para pipeline_health).
  --json   resumen en JSON para compliance_daily_report.py
"""
import json, os, pathlib, sys, time, urllib.parse, urllib.request

MEDIA = pathlib.Path("/APPBOX_DATA/storage/media")
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm", ".mov"}
ES = {"spa", "es", "esp", "es-es", "es-419", "es-mx", "es-la", "spanish", "castilian", "lat", "latin"}
LOG_FASTSTART = "/config/berenstuff/automation/logs/faststart.log"

SIN_ENTRAR_MIN = 30          # minutos de gracia para que el hook/cron enlace lo nuevo
SIN_CARATULA_H = 2           # horas antes de reclamar por caratula/refresco
SIN_SUBS_H = 12              # horas antes de reclamar por subs en espanol
SIN_SUBS_MAX_D = 7           # mas viejo que esto ya no es "reciente"

U = os.environ["EMBY_URL"].rstrip("/")
K = os.environ["EMBY_API_KEY"]


def api(path, **q):
    url = U + "/emby" + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, headers={"X-Emby-Token": K, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def ts(s):
    """'2026-09-11T15:11:37.0000000Z' -> epoch. Emby lo saca de la fecha del archivo."""
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except (TypeError, ValueError):
        return 0


def corto(p, n=60):
    return pathlib.Path(p).name[:n]


def revisar():
    ahora = time.time()
    r = {"duplicados": [], "sin_entrar": [], "sin_caratula": [], "sin_subs": [], "faststart": 0}

    items = api("/Items", Recursive="true", IncludeItemTypes="Movie,Episode,Series",
                Fields="Path,ImageTags,DateLastRefreshed,DateCreated", Limit=50000)["Items"]

    # 1. duplicados por ruta
    vistos = {}
    for it in items:
        p = it.get("Path")
        if p and it["Type"] != "Series":
            vistos.setdefault(p, []).append(it["Id"])
    r["duplicados"] = [corto(p) for p, ids in vistos.items() if len(ids) > 1]

    # 2. en disco pero sin item: se compara por ruta REAL (los items viven en /virtual)
    reales = set()
    for p in vistos:
        try:
            reales.add(os.path.realpath(p))
        except OSError:
            pass
    for v in MEDIA.rglob("*"):
        if v.suffix.lower() not in VIDEO_EXT or not v.is_file():
            continue
        try:
            edad = ahora - v.stat().st_mtime
        except OSError:
            continue
        if SIN_ENTRAR_MIN * 60 <= edad <= 48 * 3600 and str(v) not in reales:
            r["sin_entrar"].append(v.name[:60])

    # 3. sin caratula o nunca refrescado
    for it in items:
        if ahora - ts(it.get("DateCreated", "")) < SIN_CARATULA_H * 3600:
            continue
        if not (it.get("ImageTags") or {}).get("Primary"):
            r["sin_caratula"].append(it.get("Name", "?")[:60])

    # 4. sin subs en espanol (solo lo reciente: pide MediaStreams solo de esos)
    recientes = [it for it in items if it["Type"] != "Series"
                 and SIN_SUBS_H * 3600 <= ahora - ts(it.get("DateCreated", "")) <= SIN_SUBS_MAX_D * 86400]
    for i in range(0, len(recientes), 100):
        lote = recientes[i:i + 100]
        det = api("/Items", Ids=",".join(it["Id"] for it in lote), Fields="MediaStreams,Path")["Items"]
        for it in det:
            streams = it.get("MediaStreams") or []
            audio_es = any(s.get("Type") == "Audio" and (s.get("Language") or "").lower() in ES for s in streams)
            sub_es = any(s.get("Type") == "Subtitle" and (s.get("Language") or "").lower() in ES for s in streams)
            if not audio_es and not sub_es:
                r["sin_subs"].append(corto(it.get("Path") or it.get("Name", "?")))

    # 5. faststart: FALLO en las ultimas 24 h
    try:
        corte = time.strftime("%Y-%m-%d %H:%M", time.localtime(ahora - 86400))
        with open(LOG_FASTSTART, encoding="utf-8", errors="replace") as f:
            r["faststart"] = sum(1 for l in f if "FALLO" in l and l[:16] >= corte)
    except OSError:
        pass
    return r


def lineas(r):
    out = []
    def fila(sev, n, texto, ejemplos=()):
        ej = ("; p.ej. " + ", ".join(list(ejemplos)[:3])) if n and ejemplos else ""
        out.append(f"{sev if n else 'OK'}: {texto}{ej}")
    fila("ALARM", len(r["duplicados"]), f"{len(r['duplicados'])} ruta(s) duplicada(s) en Emby", r["duplicados"])
    fila("WARN", len(r["sin_entrar"]), f"{len(r['sin_entrar'])} video(s) en disco sin entrar a Emby (>{SIN_ENTRAR_MIN} min)", r["sin_entrar"])
    fila("WARN", len(r["sin_caratula"]), f"{len(r['sin_caratula'])} item(s) sin caratula (>{SIN_CARATULA_H} h)", r["sin_caratula"])
    fila("WARN", len(r["sin_subs"]), f"{len(r['sin_subs'])} reciente(s) sin subs ni audio en espanol (>{SIN_SUBS_H} h)", r["sin_subs"])
    fila("WARN", r["faststart"], f"{r['faststart']} FALLO(s) de faststart en 24 h")
    return out


def main():
    r = revisar()
    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print("\n".join(lineas(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
