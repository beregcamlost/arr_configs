#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""El orden de los tiles en el home, igual para los 34 usuarios.

Beren lo pidio por parejas (8-sep-2026): peliculas, peliculas en español y
anime -- y lo mismo para series. Live TV al final. Lo que no esta en la lista
(Donghua, Collections, Playlists) va detras y de todas formas esta oculto.

OrderedViews va por GUID de biblioteca, no por Id numerico, y el GUID cambia si la
biblioteca se borra y se vuelve a crear: por eso esto se recalcula y no se guarda.
"""
import json, os, sys, urllib.parse, urllib.request

U = os.environ["EMBY_URL"].rstrip("/"); K = os.environ["EMBY_API_KEY"]
APLICAR = "--apply" in sys.argv
ORDEN = ["Películas", "Películas en Español", "Anime",
         "Series", "Series en Español", "Series Anime"]

def api(path, cuerpo=None, **q):
    q["api_key"] = K
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f"{U}{path}?{urllib.parse.urlencode(q)}",
                                 data=datos, method="POST" if datos is not None else "GET",
                                 headers={"Content-Type": "application/json"} if datos else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

libs = {v["Name"]: v["Guid"] for v in api("/Library/VirtualFolders")}
falta = [n for n in ORDEN if n not in libs]
if falta:
    print("AVISO: no existen en Emby: " + ", ".join(falta))
cabeza = [libs[n] for n in ORDEN if n in libs]
print("orden: " + " > ".join(n for n in ORDEN if n in libs))

cambiados = 0
for u in api("/Users"):
    cfg = dict(u.get("Configuration") or {})
    resto = [g for g in (cfg.get("OrderedViews") or []) if g not in cabeza]
    nuevo = cabeza + resto
    if nuevo == (cfg.get("OrderedViews") or []):
        continue
    cambiados += 1
    if APLICAR:
        cfg["OrderedViews"] = nuevo
        api(f"/Users/{u['Id']}/Configuration", cfg)
print(f"usuarios {'reordenados' if APLICAR else 'por reordenar'}: {cambiados}")
if not APLICAR:
    print("(simulacro: nada escrito. Repetir con --apply)")
