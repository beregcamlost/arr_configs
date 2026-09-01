#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Estantes de idioma: vistas transversales que no mueven un solo archivo.

El catálogo (Películas, Animación, Anime, Donghua) es una taxonomía: carpetas
físicas y disjuntas, un título vive en una sola. El idioma en que PUEDES verlo
no es una categoría sino una disponibilidad, y corta a través de todas: una
película japonesa doblada al latino tiene que seguir viviendo en Anime para
quien la ve en japonés.

La solución, medida en vivo contra la biblioteca real el 31-ago-2026: un árbol
de ENLACES SIMBÓLICOS fuera de /media, publicado como biblioteca aparte.

  * Emby le da un ItemId propio al enlace, pero sincroniza el UserData con el
    original por provider id: el "visto" y el minuto exacto viajan en los dos
    sentidos, también episodio por episodio.
  * ExcludeFromSearch evita que la copia salga doble al buscar.
  * MediaSources sigue en 1, así que el reproductor no ofrece dos versiones.
  * Beren prefiere películas y series en estantes distintos, así que cada
    combinación lleva su CollectionType. Emby soporta bibliotecas mixtas
    (probado), y si algún día quiere la mitad de tiles, es cambiar SHELVES.

Nada de esto toca /APPBOX_DATA/storage/media, que es lo único que miran el
librarian, Radarr/Sonarr, Bazarr y los cron de transcode.
"""
import json, os, pathlib, sys, urllib.parse, urllib.request

VIRTUAL_ROOT = pathlib.Path("/APPBOX_DATA/storage/virtual")
MEDIA_ROOT = "/APPBOX_DATA/storage/media"

# Emby etiqueta el audio latino de varias formas segun de donde vino el archivo.
ES_LANGS = {"spa", "es", "esp", "es-es", "es-419", "es-mx", "es-la",
            "spanish", "castilian", "lat", "latin"}

SHELVES = {
    "es-movies": {"name": "Películas en Español", "kind": "Movie",   "type": "movies"},
    "es-tv":     {"name": "Series en Español",    "kind": "Episode", "type": "tvshows"},
}

# Estantes que existieron y se fusionaron en el de arriba (31-ago-2026, decision de
# Beren al ver el home: "sigue siendo mucho"). Separar animacion de imagen real DENTRO
# del eje de idioma abria cuatro tiles para responder una sola pregunta -- "¿que puedo
# ver en español?" -- y la respuesta no cambia por si el dibujo es animado. El eje de
# categoria sigue intacto en /media, que es donde importa.
# Se listan para que el script converja solo: si la biblioteca vieja sigue publicada, la
# retira, y limpia su arbol de enlaces.
RETIRADOS = {"Animación en Español": "es-movies-anim",
             "Series Animadas en Español": "es-tv-anim"}

# Por debajo de esto un estante no se gana un tile propio: lo que hay se
# encuentra igual en el estante de arriba, y una fila larga de carpetas en el
# home cuesta mas de lo que valen unos pocos titulos. Es la unica palanca que
# frena el crecimiento cuando cada idea se abre en pelicula y serie.
MIN_TITLES = 8

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


def title_dir(path):
    """La carpeta del titulo y el estante donde vive, leidos de su ruta."""
    if not path or not path.startswith(MEDIA_ROOT + "/"):
        return None, None
    rest = path[len(MEDIA_ROOT) + 1:].split("/")
    if len(rest) < 2:
        return None, None
    return pathlib.Path(MEDIA_ROOT, rest[0], rest[1]), rest[0]


def wanted(uid):
    """{clave de estante: {carpeta: ruta}} para todo lo que tiene audio en espanol."""
    out = {k: {} for k in SHELVES}
    index = {s["kind"]: k for k, s in SHELVES.items()}
    for kind in ("Movie", "Episode"):
        items = api("GET", f"/Users/{uid}/Items", Recursive="true", IncludeItemTypes=kind,
                    Fields="MediaStreams,Path", Limit=50000)["Items"]
        for it in items:
            if not has_es_audio(it):
                continue
            d, _shelf = title_dir(it.get("Path"))
            if not d:
                continue
            out[index[kind]][d.name] = d
    return out


def sync_links(sub, targets):
    root = VIRTUAL_ROOT / sub
    root.mkdir(parents=True, exist_ok=True)
    have = {p.name: p for p in root.iterdir()}
    added = removed = 0
    for name, dest in targets.items():
        link = root / name
        if name in have and link.is_symlink() and os.readlink(link) == str(dest):
            continue
        if name in have:
            link.unlink()
        link.symlink_to(dest)
        added += 1
    for name, link in have.items():
        if name not in targets and link.is_symlink():
            link.unlink()
            removed += 1
    return added, removed


def library(name):
    return next((v for v in api("GET", "/Library/VirtualFolders") if v["Name"] == name), None)


def ensure_library(sub, spec):
    """Crea la biblioteca si falta y deja ExcludeFromSearch puesto."""
    cur = library(spec["name"])
    created = False
    if cur is None:
        api("POST", "/Library/VirtualFolders", None, Name=spec["name"],
            CollectionType=spec["type"], Paths=str(VIRTUAL_ROOT / sub),
            RefreshLibrary="false")
        cur = library(spec["name"])
        created = True
    opts = dict(cur["LibraryOptions"])
    if not opts.get("ExcludeFromSearch"):
        opts["ExcludeFromSearch"] = True
        api("POST", "/Library/VirtualFolders/LibraryOptions",
            {"Id": cur["Id"], "LibraryOptions": opts})
    return cur["Id"], created


def drop_library(name):
    cur = library(name)
    if cur:
        api("POST", "/Library/VirtualFolders/Delete", {"Id": cur["Id"], "RefreshLibrary": False})
        return True
    return False


def main():
    uid = admin_id()
    targets = wanted(uid)
    changed = False
    for nombre, sub in RETIRADOS.items():
        if drop_library(nombre):
            changed = True
            print(f"{nombre:24}      fusionado en el estante de arriba, biblioteca retirada")
        sync_links(sub, {})
    for sub, spec in SHELVES.items():
        want = targets[sub]
        if len(want) < MIN_TITLES:
            gone = drop_library(spec["name"])
            changed = changed or gone
            sync_links(sub, {})
            print(f"{spec['name']:24} {len(want):4} titulos  -> bajo el minimo de {MIN_TITLES}"
                  f"{', estante retirado' if gone else ', sin estante'}")
            continue
        added, removed = sync_links(sub, want)
        lib_id, created = ensure_library(sub, spec)
        changed = changed or bool(added or removed or created)
        print(f"{spec['name']:24} {len(want):4} titulos  (+{added} -{removed})"
              f"{'  [estante creado]' if created else ''}")
        if added or removed or created:
            api("POST", f"/Items/{lib_id}/Refresh", None, Recursive="true",
                ImageRefreshMode="Default", MetadataRefreshMode="Default")
    print("escaneo lanzado" if changed else "sin cambios")


if __name__ == "__main__":
    sys.exit(main())
