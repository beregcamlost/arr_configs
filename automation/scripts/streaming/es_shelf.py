#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Estantes de idioma: una colección, no una copia.

El catálogo (Películas, Animación, Anime, Donghua) es una taxonomía: carpetas
físicas y disjuntas, un título vive en una sola. El idioma en que PUEDES verlo
no es una categoría sino una disponibilidad, y corta a través de todas: una
película japonesa doblada al latino tiene que seguir viviendo en Anime para
quien la ve en japonés.

HASTA EL 8-SEP-2026 ESTO SE RESOLVÍA CON ENLACES SIMBÓLICOS: un árbol fuera de
/media publicado como biblioteca aparte. Funcionaba, pero Emby le da un ItemId
propio al enlace, y ese ítem duplicado se asomaba una y otra vez por sitios
distintos: primero en "Novedades" (arreglado a mano el 6-sep, y pisado al día
siguiente por shelf_visibility.py), después en la lista de reproducidos. Cada
superficie nueva de la interfaz era un parche nuevo. Beren, viendo Your Name
repetida: "mata los symlinks, que los tiles no dupliquen nada".

AHORA EL ESTANTE ES UNA COLECCIÓN. Una colección referencia el MISMO ítem, así
que no hay nada que deduplicar, en ninguna pantalla, ni ahora ni cuando Emby
añada una pantalla más. Se paga con la puerta de entrada: una colección no da
tile propio en el home, se abre desde dentro de Películas / Series (las 48
colecciones de saga ya viven ahí). El tile no se puede tener sin duplicar: en
Emby un tile ES una biblioteca, y una biblioteca necesita rutas propias.

El "visto" no se perdió al migrar: Emby sincronizaba el UserData de la copia
con el del original por provider id. Auditado antes de borrar sobre los 34
usuarios y los 1049 ítems enlazados: 0 casos de estado que viviera sólo en la
copia (/tmp/es_riesgo.json, 8-sep-2026).

Nada de esto toca /APPBOX_DATA/storage/media, que es lo único que miran el
librarian, Radarr/Sonarr, Bazarr y los cron de transcode.
"""
import json, os, shutil, sys, urllib.parse, urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
VIRTUAL_ROOT = "/APPBOX_DATA/storage/virtual"

# Emby etiqueta el audio latino de varias formas segun de donde vino el archivo.
ES_LANGS = {"spa", "es", "esp", "es-es", "es-419", "es-mx", "es-la",
            "spanish", "castilian", "lat", "latin"}

# El estante mira EPISODIOS pero agrupa por SERIE: una serie entra si algun
# episodio suyo tiene audio en español (es como se buscaba con los enlaces, que
# tambien enlazaban la carpeta de la serie entera).
SHELVES = {
    "Películas en Español": {"kind": "Movie"},
    "Series en Español": {"kind": "Episode"},
}

# Las bibliotecas de enlaces que sustituye este script. Se listan para que la
# migracion converja sola: mientras alguna siga publicada, se retira.
SYMLINK_LIBS = ["Películas en Español", "Series en Español",
                "Animación en Español", "Series Animadas en Español"]

U = os.environ["EMBY_URL"].rstrip("/")
K = os.environ["EMBY_API_KEY"]


def api(method, path, body=None, **q):
    q["api_key"] = K
    data = json.dumps(body).encode() if body is not None else None
    head = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(f"{U}{path}?{urllib.parse.urlencode(q)}",
                                 method=method, data=data, headers=head)
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return r.status


def admin_id():
    return [u for u in api("GET", "/Users")
            if u.get("Policy", {}).get("IsAdministrator")][0]["Id"]


def has_es_audio(item):
    return any(s.get("Type") == "Audio" and (s.get("Language") or "").lower() in ES_LANGS
               for s in (item.get("MediaStreams") or []))


def wanted(uid):
    """{nombre de estante: {item_id}} para todo lo que tiene audio en español."""
    out = {}
    for nombre, spec in SHELVES.items():
        items = api("GET", f"/Users/{uid}/Items", Recursive="true",
                    IncludeItemTypes=spec["kind"], Fields="MediaStreams,Path",
                    Limit=50000)["Items"]
        ids = set()
        for it in items:
            # Los enlaces viejos siguen indexados hasta que se retire su
            # biblioteca: nunca son el ítem que va a la colección.
            if (it.get("Path") or "").startswith(VIRTUAL_ROOT):
                continue
            if not has_es_audio(it):
                continue
            ids.add(it["SeriesId"] if spec["kind"] == "Episode" else it["Id"])
        out[nombre] = ids
    return out


def collection(nombre):
    r = api("GET", "/Items", IncludeItemTypes="BoxSet", Recursive="true", Limit=1000)
    return next((b for b in r["Items"] if b["Name"] == nombre), None)


def sync_collection(nombre, quiere):
    col = collection(nombre)
    if col is None:
        col = api("POST", "/Collections", None, Name=nombre,
                  Ids=",".join(sorted(quiere)), IsLocked="false")
        print(f"{nombre:24} {len(quiere):4} títulos  [colección creada]")
        return True
    cid = col["Id"]
    tiene = {c["Id"] for c in api("GET", "/Items", ParentId=cid, Limit=10000)["Items"]}
    faltan, sobran = quiere - tiene, tiene - quiere
    for lote, ruta in ((faltan, f"/Collections/{cid}/Items"),
                       (sobran, f"/Collections/{cid}/Items/Delete")):
        ids = sorted(lote)
        for i in range(0, len(ids), 100):
            api("POST", ruta, None, Ids=",".join(ids[i:i + 100]))
    print(f"{nombre:24} {len(quiere):4} títulos  (+{len(faltan)} -{len(sobran)})")
    return bool(faltan or sobran)


def retirar_symlinks():
    """Borra las bibliotecas de enlaces y su árbol. Idempotente."""
    libs = {v["Name"]: v for v in api("GET", "/Library/VirtualFolders")
            if any(l.startswith(VIRTUAL_ROOT) for l in (v.get("Locations") or []))}
    for nombre in SYMLINK_LIBS:
        lib = libs.get(nombre)
        if lib:
            api("POST", "/Library/VirtualFolders/Delete",
                {"Id": lib["Id"], "RefreshLibrary": False})
            print(f"{nombre:24}      biblioteca de enlaces retirada")
    if os.path.isdir(VIRTUAL_ROOT):
        for sub in sorted(os.listdir(VIRTUAL_ROOT)):
            shutil.rmtree(os.path.join(VIRTUAL_ROOT, sub))
            print(f"{'':24}      árbol de enlaces borrado: {sub}")


def main():
    uid = admin_id()
    quiere = wanted(uid)
    for nombre in SHELVES:
        sync_collection(nombre, quiere[nombre])
    retirar_symlinks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
