#!/usr/bin/env python3
"""Devuelve el estado guardado por userdata_dump.py despues de que Emby re-cree los
items con otro ItemId. La ruta del archivo es la unica identidad que sobrevive.
Sin --apply solo informa que faltaria devolver."""
import json, os, sys, urllib.parse, urllib.request

U = os.environ["EMBY_URL"].rstrip("/"); K = os.environ["EMBY_API_KEY"]
ENTRADA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/userdata_pre.json"
APLICAR = "--apply" in sys.argv

def api(path, cuerpo=None, **q):
    q["api_key"] = K
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f"{U}{path}?{urllib.parse.urlencode(q)}",
                                 data=datos, method="POST" if datos is not None else "GET",
                                 headers={"Content-Type": "application/json"} if datos else {})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

copia = json.load(open(ENTRADA))
faltan = devueltos = intactos = sin_item = 0
for uid, info in copia.items():
    if not info["estado"]:
        continue
    items = api(f"/Users/{uid}/Items", None, Recursive="true",
                IncludeItemTypes="Movie,Episode,Series", Fields="Path,UserData",
                Limit=100000)["Items"]
    por_ruta = {it.get("Path"): it for it in items if it.get("Path")}
    pendientes = []
    for ruta, viejo in info["estado"].items():
        it = por_ruta.get(ruta)
        if not it:
            sin_item += 1
            continue
        ahora = it.get("UserData") or {}
        igual = (bool(ahora.get("Played")) == viejo["Played"]
                 and (ahora.get("PlaybackPositionTicks") or 0) >= viejo["PlaybackPositionTicks"]
                 and bool(ahora.get("IsFavorite")) == viejo["IsFavorite"])
        if igual:
            intactos += 1
            continue
        pendientes.append((it["Id"], ruta, viejo))
    faltan += len(pendientes)
    if pendientes:
        print(f"  {info['nombre']:<14} {len(pendientes):5} por devolver")
    if not APLICAR:
        continue
    for iid, ruta, viejo in pendientes:
        cuerpo = {"PlaybackPositionTicks": viejo["PlaybackPositionTicks"],
                  "PlayCount": viejo["PlayCount"], "Played": viejo["Played"],
                  "IsFavorite": viejo["IsFavorite"]}
        if viejo.get("LastPlayedDate"):
            cuerpo["LastPlayedDate"] = viejo["LastPlayedDate"]
        api(f"/Users/{uid}/Items/{iid}/UserData", cuerpo)
        devueltos += 1

print(f"\nintactos: {intactos}   por devolver: {faltan}   devueltos: {devueltos}"
      f"   sin item en la biblioteca: {sin_item}")
print("(simulacro: nada escrito. Repetir con --apply)" if not APLICAR else "")
