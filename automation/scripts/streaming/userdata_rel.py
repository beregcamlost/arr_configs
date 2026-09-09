#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Salva y devuelve el "visto" cuando un titulo cambia de RUTA, no solo de biblioteca.

userdata_dump.py indexa por ruta absoluta, que es identidad suficiente mientras la
carpeta solo cambia de dueño. Al publicar una biblioteca sobre un arbol de enlaces
(/virtual/movies-rest en vez de /media/movies) la ruta entera cambia, asi que aqui la
identidad es la ruta RELATIVA a la raiz: "Moana (2026)/Moana (2026) WEBDL-1080p.mkv"
es la misma pelicula la vea Emby por donde la vea.

Uso:  userdata_rel.py dump <fichero>
      userdata_rel.py restore <fichero> [--apply]
"""
import json, os, sys, urllib.parse, urllib.request

U = os.environ["EMBY_URL"].rstrip("/"); K = os.environ["EMBY_API_KEY"]

# Toda raiz por la que un mismo archivo puede entrar a la biblioteca. El orden no
# importa: se prueba la mas larga primero para que "…/movies/" no se coma "…/moviesanimated/".
RAICES = ("/APPBOX_DATA/storage/media/movies/",
          "/APPBOX_DATA/storage/media/tv/",
          "/APPBOX_DATA/storage/media/tvanimated/",
          "/APPBOX_DATA/storage/virtual/movies-rest/",
          "/APPBOX_DATA/storage/virtual/tv-rest/",
          "/APPBOX_DATA/storage/virtual/es-movies/",
          "/APPBOX_DATA/storage/virtual/es-tv/")


def api(path, cuerpo=None, **q):
    q["api_key"] = K
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f"{U}{path}?{urllib.parse.urlencode(q)}", data=datos,
                                 method="POST" if datos is not None else "GET",
                                 headers={"Content-Type": "application/json"} if datos else {})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def clave(path):
    """Ruta relativa a su raiz: la identidad que sobrevive al cambio de arbol."""
    for r in sorted(RAICES, key=len, reverse=True):
        if path and path.startswith(r):
            return path[len(r):]
    return None


def estado(it):
    d = it.get("UserData") or {}
    return {"Played": bool(d.get("Played")), "PlayCount": d.get("PlayCount") or 0,
            "PlaybackPositionTicks": d.get("PlaybackPositionTicks") or 0,
            "IsFavorite": bool(d.get("IsFavorite")), "LastPlayedDate": d.get("LastPlayedDate")}


def con_marca(e):
    return e["Played"] or e["PlaybackPositionTicks"] or e["IsFavorite"] or e["PlayCount"]


def items_de(uid):
    return api(f"/Users/{uid}/Items", None, Recursive="true",
               IncludeItemTypes="Movie,Episode,Series", Fields="Path,UserData",
               Limit=100000)["Items"]


def dump(destino):
    out, total = {}, 0
    for u in api("/Users"):
        guardado = {}
        for it in items_de(u["Id"]):
            k = clave(it.get("Path") or "")
            if not k:
                continue
            e = estado(it)
            if not con_marca(e):
                continue
            # el mismo archivo puede venir por dos raices (catalogo y estante de idioma):
            # Emby sincroniza el UserData entre las copias, pero nos quedamos con el mayor.
            v = guardado.get(k)
            if v is None or (e["PlaybackPositionTicks"], e["Played"]) > (v["PlaybackPositionTicks"], v["Played"]):
                guardado[k] = e
        out[u["Id"]] = {"nombre": u["Name"], "estado": guardado}
        total += len(guardado)
        if guardado:
            print(f"  {u['Name']:<14} {len(guardado):5} registros")
    json.dump(out, open(destino, "w"))
    print(f"\ntotal: {total} registros -> {destino}")


def restore(origen, aplicar):
    copia = json.load(open(origen))
    faltan = devueltos = intactos = sin_item = 0
    for uid, info in copia.items():
        if not info["estado"]:
            continue
        por_clave = {}
        for it in items_de(uid):
            k = clave(it.get("Path") or "")
            if k:
                por_clave.setdefault(k, []).append(it)
        pendientes = []
        for k, viejo in info["estado"].items():
            hits = por_clave.get(k)
            if not hits:
                sin_item += 1
                continue
            for it in hits:
                ahora = estado(it)
                if (ahora["Played"] == viejo["Played"]
                        and ahora["PlaybackPositionTicks"] >= viejo["PlaybackPositionTicks"]
                        and ahora["IsFavorite"] == viejo["IsFavorite"]):
                    intactos += 1
                    continue
                pendientes.append((it["Id"], viejo))
        faltan += len(pendientes)
        if pendientes:
            print(f"  {info['nombre']:<14} {len(pendientes):5} por devolver")
        if not aplicar:
            continue
        for iid, viejo in pendientes:
            cuerpo = {"PlaybackPositionTicks": viejo["PlaybackPositionTicks"],
                      "PlayCount": viejo["PlayCount"], "Played": viejo["Played"],
                      "IsFavorite": viejo["IsFavorite"]}
            if viejo.get("LastPlayedDate"):
                cuerpo["LastPlayedDate"] = viejo["LastPlayedDate"]
            api(f"/Users/{uid}/Items/{iid}/UserData", cuerpo)
            devueltos += 1
    print(f"\nintactos: {intactos}   por devolver: {faltan}   devueltos: {devueltos}"
          f"   sin item en la biblioteca: {sin_item}")
    if not aplicar:
        print("(simulacro: nada escrito. Repetir con --apply)")
    return sin_item


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("dump", "restore"):
        print(__doc__)
        return 2
    if sys.argv[1] == "dump":
        dump(sys.argv[2])
        return 0
    restore(sys.argv[2], "--apply" in sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
