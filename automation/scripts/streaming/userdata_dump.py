#!/usr/bin/env python3
"""Salva el estado de reproduccion de los 34 usuarios indexado por RUTA de archivo.
La ruta sobrevive a que Emby re-cree el item con otro ItemId."""
import json, os, sys, urllib.parse, urllib.request

U = os.environ["EMBY_URL"].rstrip("/"); K = os.environ["EMBY_API_KEY"]
SALIDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/userdata_backup.json"
# Carpetas que van a cambiar de dueño al separar los tiles
AFECTADAS = ("/APPBOX_DATA/storage/media/moviesanimated/",
             "/APPBOX_DATA/storage/media/moviesanime/",
             "/APPBOX_DATA/storage/media/moviesdonghua/",
             "/APPBOX_DATA/storage/media/tvanimated/",
             "/APPBOX_DATA/storage/media/tvanime/",
             "/APPBOX_DATA/storage/media/tvdonghua/")

def api(path, **q):
    q["api_key"] = K
    with urllib.request.urlopen(f"{U}{path}?{urllib.parse.urlencode(q)}", timeout=300) as r:
        return json.loads(r.read())

usuarios = api("/Users")
out = {}
total = 0
for u in usuarios:
    items = api(f"/Users/{u['Id']}/Items", Recursive="true",
                IncludeItemTypes="Movie,Episode,Series", Fields="Path,UserData",
                Limit=100000)["Items"]
    guardado = {}
    for it in items:
        p = it.get("Path") or ""
        if not p.startswith(AFECTADAS):
            continue
        d = it.get("UserData") or {}
        if d.get("Played") or d.get("PlaybackPositionTicks") or d.get("IsFavorite") \
           or d.get("PlayCount"):
            guardado[p] = {"Played": bool(d.get("Played")),
                           "PlayCount": d.get("PlayCount") or 0,
                           "PlaybackPositionTicks": d.get("PlaybackPositionTicks") or 0,
                           "IsFavorite": bool(d.get("IsFavorite")),
                           "LastPlayedDate": d.get("LastPlayedDate")}
    out[u["Id"]] = {"nombre": u["Name"], "estado": guardado}
    total += len(guardado)
    if guardado:
        print(f"  {u['Name']:<14} {len(guardado):5} registros")
json.dump(out, open(SALIDA, "w"))
print(f"\ntotal: {total} registros de {len(usuarios)} usuarios -> {SALIDA}")
