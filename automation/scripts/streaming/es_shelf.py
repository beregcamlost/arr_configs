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

EL 8-SEP-2026 ESTO SE PROBÓ COMO COLECCIÓN Y SE VOLVIÓ ATRÁS. Una colección
referencia el mismo ítem, así que no duplica nada; lo que no da es tile, se abre
desde dentro de Películas. Beren lo vio y lo rechazó en el acto: "la cagué, quiero
tener separados las películas, de las películas en español, las animadas y las
anime... lamentablemente serían 8 tiles pero bueno lo prefiero así". En Emby un
tile ES una biblioteca y una biblioteca necesita rutas propias, o sea que el ítem
duplicado es el precio del tile. El precio ya está pagado: la copia no sale al
buscar (ExcludeFromSearch, aquí abajo) ni en Novedades (shelf_visibility.py excluye
sola cualquier biblioteca montada sobre /virtual, así que ese arreglo ya no se
puede volver a pisar como pasó el 6-sep).

EL 9-SEP-2026 EL ESTANTE DE IDIOMA PASÓ DE ATAJO A EXCLUSIVO. Beren volvió a ver
*Moana* dos veces seguidas en la misma fila del home ("veo moana repetida de nuevo,
está en dos tiles, ¿qué pasó pues?") y eligió la salida de fondo: si una película tiene
audio español, vive en *Películas en Español* y NO en *Películas*. Es la misma regla que
ya gobernaba animación y anime (EXCLUSIVAS, aquí abajo), aplicada ahora al catch-all.

Se hace SIN mover un archivo, con el árbol simétrico al de idioma: *Películas* deja de
publicarse sobre /media/movies y pasa a publicarse sobre /virtual/movies-rest, que son
los enlaces de lo que NO tiene audio español. Así las dos bibliotecas son disjuntas por
construcción -- no hay fila, cliente ni sección donde el título pueda salir dos veces --
y /media sigue intacto para Radarr, Bazarr, el librarian y los cron de transcode, que es
lo que hace este arreglo reversible con una llamada.

El precio, aceptado al elegirlo: cambiar la raíz de una biblioteca re-crea sus ItemId, o
sea que el "visto" de los 34 usuarios se salva antes con userdata_rel.py (indexa por ruta
RELATIVA, la única identidad que sobrevive al cambio de árbol) y se devuelve después.

Nada de esto toca /APPBOX_DATA/storage/media, que es lo único que miran el
librarian, Radarr/Sonarr, Bazarr y los cron de transcode.
"""
import json, os, pathlib, sys, time, urllib.parse, urllib.request

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

# Guardia (10-sep-2026): si un estante VIVO pierde de golpe mas de CAIDA_TITULOS titulos
# Y mas de CAIDA_FRACCION de ellos, el script no toca NADA y avisa. Una caida brusca es casi
# siempre que el script dejo de VER los titulos, no que desaparecieran: asi se
# autodestruyeron los dos estantes de idioma el 10-sep a las 05:50Z (ver title_dir). Y
# tambien pasa, en pequeño, justo despues de re-crear items: hasta que el escaneo les lee
# las pistas de audio, has_es_audio() los ve "sin español". El dia normal cambia 1-3
# titulos. Desmontar re-crea miles de items y borra los intros de Series. Si la caida es
# real, se aplica a mano con --forzar.
CAIDA_TITULOS = 5
CAIDA_FRACCION = 0.10

# Las bibliotecas que crea este script nacen con el estandar de la casa para previews e
# intros: se generan (Enable*), pero NUNCA durante el escaneo (*DuringLibraryScan). Una
# biblioteca nueva trae los defaults de Emby, y un escaneo de miles de episodios con
# deteccion de intros activa fue lo que dejo 42 series sin caratula el 9-sep.
OPCIONES_CASA = {
    "ExcludeFromSearch": True,
    "EnableChapterImageExtraction": True,
    "ExtractChapterImagesDuringLibraryScan": False,
    "ThumbnailImagesIntervalSeconds": 10,
    "SaveLocalThumbnailSets": True,
    "EnableMarkerDetection": True,
    "EnableMarkerDetectionDuringLibraryScan": False,
}

# Categorias EXCLUSIVAS: no prestan titulos al estante de idioma. Beren, al recuperar
# el tile de Animacion (8-sep-2026): "la idea es que no se repita, lo que va en
# animacion es solo de animacion, ya no deberia salir ni en peliculas ni en peliculas
# en español". Poco despues extendio la misma regla al anime ("saca esos 4 tambien"),
# que eran los ultimos titulos que se veian por partida doble. Para el resto el estante
# de idioma sigue siendo un ATAJO, que es como el mismo lo decidio horas antes: una peli
# live-action en español esta en Peliculas Y en Peliculas en Español, porque "tener audio
# español" es disponibilidad y no categoria. Aqui van las carpetas de /media cuyo tile
# manda sobre esa regla; en la practica, todas las que tienen tile menos el catch-all.
EXCLUSIVAS = {"moviesanimated", "moviesanime", "moviesdonghua", "moviesaeni",
              "tvanimated", "tvanime", "tvdonghua", "tvaeni"}
# 9-sep-2026: la regla estaba escrita para las dos mitades pero solo aplicada a
# las carpetas de peliculas, asi que Series en Español seguia mostrando anime
# (Beren: "por que series en español tiene animes?"). Ahora presta solo el
# catch-all de cada mitad: movies y tv.

# El complemento del catch-all: lo que NO se fue al estante de idioma. La biblioteca
# ancha se publica sobre este arbol en vez de sobre /media, que es lo que hace que las
# dos sean disjuntas. Si el estante de idioma no llega a MIN_TITLES no hay nada que
# restar y la biblioteca vuelve sola a /media (convergencia, no estado a mano).
RESTO = {
    "movies-rest": {"lib": "Movies", "carpeta": "movies", "shelf": "es-movies"},
    "tv-rest":     {"lib": "Series", "carpeta": "tv",     "shelf": "es-tv"},
}

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
    """La carpeta del titulo y el estante donde vive, leidos de su ruta REAL.

    Se resuelve el enlace (10-sep-2026). Desde que el estante es exclusivo, las
    bibliotecas se publican sobre los arboles /virtual y NINGUN item tiene ruta /media:
    sin realpath esta funcion no reconocia nada, wanted() salia vacio y el cron de las
    05:50Z retiro los dos estantes de idioma y devolvio las bibliotecas a /media. Al dia
    siguiente los habria vuelto a crear, y asi en vaiven, re-creando ~2400 items por vuelta.
    """
    path = os.path.realpath(path) if path else path
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
            d, shelf = title_dir(it.get("Path"))
            if not d or shelf in EXCLUSIVAS:
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
    """Crea la biblioteca si falta y la deja con OPCIONES_CASA (converge en cada corrida)."""
    cur = library(spec["name"])
    created = False
    if cur is None:
        api("POST", "/Library/VirtualFolders", None, Name=spec["name"],
            CollectionType=spec["type"], Paths=str(VIRTUAL_ROOT / sub),
            RefreshLibrary="false")
        cur = library(spec["name"])
        created = True
    opts = dict(cur["LibraryOptions"])
    if any(opts.get(k) != v for k, v in OPCIONES_CASA.items()):
        opts.update(OPCIONES_CASA)   # objeto COMPLETO: un POST parcial pisa lo ausente
        api("POST", "/Library/VirtualFolders/LibraryOptions",
            {"Id": cur["Id"], "LibraryOptions": opts})
    return cur["Id"], created


def drop_library(name):
    cur = library(name)
    if cur:
        api("POST", "/Library/VirtualFolders/Delete", {"Id": cur["Id"], "RefreshLibrary": False})
        return True
    return False


def folders_de(carpeta):
    """Las carpetas de titulo que hay hoy en /media/<carpeta>."""
    raiz = pathlib.Path(MEDIA_ROOT, carpeta)
    if not raiz.is_dir():
        return {}
    return {d.name: d for d in raiz.iterdir() if d.is_dir()}


def set_root(lib, poner, quitar):
    """Deja la biblioteca publicada sobre `poner` y le retira `quitar`.

    Se añade antes de quitar y sin escanear en medio: entre las dos llamadas la
    biblioteca tiene las dos raices, pero como nadie escanea todavia no llega a
    duplicar nada. Al reves -- quitar primero -- la dejaria un instante sin rutas.
    """
    loc = list(lib["Locations"])
    cambio = False
    if poner not in loc:
        api("POST", "/Library/VirtualFolders/Paths",
            {"Id": lib["Id"], "PathInfo": {"Path": poner}, "RefreshLibrary": False})
        cambio = True
    if quitar in loc:
        api("POST", "/Library/VirtualFolders/Paths/Delete",
            {"Id": lib["Id"], "Path": quitar, "RefreshLibrary": False})
        cambio = True
    return cambio


def sync_resto(sub, spec, en_idioma):
    """Publica la biblioteca ancha sobre el complemento del estante de idioma.

    en_idioma vacio (estante bajo el minimo o retirado) significa que no hay nada que
    restar: la biblioteca vuelve a /media y el arbol de enlaces se vacia. Asi el script
    converge solo en los dos sentidos y nunca deja una biblioteca a medio mudar.
    """
    lib = library(spec["lib"])
    if lib is None:
        print(f"{spec['lib']:24}      AVISO: no existe la biblioteca en Emby")
        return False
    media = str(pathlib.Path(MEDIA_ROOT, spec["carpeta"]))
    virtual = str(VIRTUAL_ROOT / sub)
    if not en_idioma:
        added, removed = sync_links(sub, {})
        movida = set_root(lib, poner=media, quitar=virtual)
        if movida:
            print(f"{spec['lib']:24}      sin estante de idioma que restar -> vuelve a /media")
        return bool(added or removed or movida)
    resto = {n: d for n, d in folders_de(spec["carpeta"]).items() if n not in en_idioma}
    added, removed = sync_links(sub, resto)
    movida = set_root(lib, poner=virtual, quitar=media)
    print(f"{spec['lib']:24} {len(resto):4} titulos  (+{added} -{removed})"
          f"  = {spec['carpeta']} menos los {len(en_idioma)} del estante de idioma"
          f"{'  [biblioteca mudada al complemento]' if movida else ''}")
    return bool(added or removed or movida)


def plan_grande(targets):
    """True si esta corrida va a crear/retirar un estante o mover la raiz de una biblioteca.

    Esas operaciones re-crean cientos o miles de items de golpe, y el 10-sep dos refrescos
    concurrentes sobre la misma carpeta (el refresco propio del estante recien creado + el
    global, y el vigilante de Emby que se disparo en pleno escaneo) crearon cada item DOS
    veces: misma ruta, mismo guid, dos carpetas padre. Emby no los limpia solo.
    """
    for sub, spec in SHELVES.items():
        if (library(spec["name"]) is not None) != (len(targets[sub]) >= MIN_TITLES):
            return True
    for sub, spec in RESTO.items():
        vivo = len(targets[spec["shelf"]]) >= MIN_TITLES
        quiere = str(VIRTUAL_ROOT / sub) if vivo else str(pathlib.Path(MEDIA_ROOT, spec["carpeta"]))
        lib = library(spec["lib"])
        if lib and quiere not in lib["Locations"]:
            return True
    return False


def vigilante(on):
    """Enciende/apaga el monitor en tiempo real de las bibliotecas que toca este script."""
    for nombre in [s["name"] for s in SHELVES.values()] + [s["lib"] for s in RESTO.values()]:
        v = library(nombre)
        if v and v["LibraryOptions"].get("EnableRealtimeMonitor", True) != on:
            opts = dict(v["LibraryOptions"])
            opts["EnableRealtimeMonitor"] = on
            api("POST", "/Library/VirtualFolders/LibraryOptions",
                {"Id": v["Id"], "LibraryOptions": opts})


def escaneo(estado_previo_visto=False, tope_s=3 * 3600):
    """Espera a que el escaneo global arranque y termine (o al tope)."""
    t0 = time.time()
    while time.time() - t0 < tope_s:
        t = next(x for x in api("GET", "/ScheduledTasks") if x["Name"] == "Scan media library")
        if t["State"] != "Idle":
            estado_previo_visto = True
        elif estado_previo_visto or time.time() - t0 > 120:
            return True
        time.sleep(30)
    return False


def main():
    uid = admin_id()
    targets = wanted(uid)
    forzar = "--forzar" in sys.argv
    for sub, spec in SHELVES.items():
        raiz = VIRTUAL_ROOT / sub
        antes = sum(1 for p in raiz.iterdir() if p.is_symlink()) if raiz.is_dir() else 0
        ahora = len(targets[sub])
        if antes >= MIN_TITLES and antes - ahora > max(CAIDA_TITULOS, antes * CAIDA_FRACCION) and not forzar:
            print(f"AVISO {spec['name']}: {antes} -> {ahora} titulos de golpe. No toco NADA:"
                  f" casi siempre es que el script dejo de ver los titulos. Si es real: --forzar")
            return 3
    grande = plan_grande(targets)
    if grande:
        print("corrida grande (estante nuevo/retirado o raiz movida): vigilante OFF y UN solo escaneo")
        vigilante(False)
    try:
        return _aplicar(targets, grande)
    finally:
        if grande:
            vigilante(True)
            print("vigilante ON")


def _aplicar(targets, grande):
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
        if created and grande:
            vigilante(False)   # un estante recien creado nace con el vigilante de Emby prendido
        changed = changed or bool(added or removed or created)
        print(f"{spec['name']:24} {len(want):4} titulos  (+{added} -{removed})"
              f"{'  [estante creado]' if created else ''}")
        if (added or removed or created) and not grande:
            api("POST", f"/Items/{lib_id}/Refresh", None, Recursive="true",
                ImageRefreshMode="Default", MetadataRefreshMode="Default")
    # El complemento va DESPUES de los estantes de idioma: resta lo que aquellos se
    # acaban de llevar, asi que leerlo antes lo dejaria un dia por detras.
    raiz_movida = False
    for sub, spec in RESTO.items():
        vivo = library(SHELVES[spec["shelf"]]["name"]) is not None
        raiz_movida = sync_resto(sub, spec, targets[spec["shelf"]] if vivo else {}) or raiz_movida
    changed = changed or raiz_movida
    if changed and (grande or raiz_movida):
        # UN solo escaneo global, y esperamos a que termine antes de devolver el vigilante:
        # cualquier refresco concurrente sobre las mismas carpetas duplica items.
        # Cambiar la RAIZ re-crea los items: el "visto" se devuelve con userdata_rel.py.
        api("POST", "/Library/Refresh", {})
        print("escaneo unico lanzado; esperando a que termine...", flush=True)
        print("escaneo terminado" if escaneo() else "AVISO: el escaneo no termino antes del tope")
    else:
        print("escaneo lanzado" if changed else "sin cambios")


if __name__ == "__main__":
    sys.exit(main())
