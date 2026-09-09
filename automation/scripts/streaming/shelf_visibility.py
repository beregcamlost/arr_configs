#!/usr/bin/env python3
"""shelf_visibility.py (2026-08-31) — un estante se gana su sitio en el home.

EL PROBLEMA: cada categoria nueva era un tile mas en "Mis medios", y las categorias
crecen a ritmos muy distintos. Medido el 2026-08-31: donghua son 7 titulos en 14 meses
(uno cada dos meses) contra ~4 anime al mes. Dos tiles permanentes por 7 titulos cuestan
mas atencion de la que devuelven, y borrar la categoria no es la respuesta: clasificar
en disco no cuesta nada y el dia que crezca queremos el estante ya armado.

LA REGLA: la carpeta en disco SIEMPRE existe; el TILE aparece solo cuando el estante
llega a MIN_TITULOS. Por debajo, el estante se oculta del home y su carpeta se cuelga
del estante padre, que es donde uno lo buscaria (el donghua dentro de Anime, no suelto).
Es el mismo criterio de es_shelf.py, que ya retira el estante de idioma flaco.

POR QUE OCULTAR Y NO BORRAR LA BIBLIOTECA: borrarla cambia su Guid, y el Guid es lo que
usan OrderedViews (el orden del home) y MyMediaExcludes en los 34 usuarios. Ocultar con
MyMediaExcludes deja el Guid intacto: el dia que el estante crece vuelve a su sitio en el
orden, sin rehacer nada. Ademas es reversible con una llamada.

NO TOCA LOS ESTANTES DE IDIOMA (Peliculas en Español y compañia). Esos los gobierna
es_shelf.py con su propio minimo, y existen porque Beren los pidio explicitamente: son
una busqueda que el hace, no una subdivision del catalogo.

Uso:  shelf_visibility.py [--apply] [--min N]
"""
import argparse
import json
import os
import sys
import urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
VIRTUAL_ROOT = "/APPBOX_DATA/storage/virtual"
MIN_TITULOS = 20

# (tile en Emby, carpeta en /media, donde se cuelga cuando es flaco)
# La cadena sube hasta encontrar un padre visible; el catch-all nunca se oculta.
ESTANTES = [
    ("Donghua Movies", "moviesdonghua", "Anime Movies"),
    ("Anime Movies", "moviesanime", "Animation Movies"),
    ("Animation Movies", "moviesanimated", "Movies"),
    ("Donghua Series", "tvdonghua", "Anime Series"),
    ("Anime Series", "tvanime", "Animated Series"),
    ("Animated Series", "tvanimated", "Series"),
]
CATCH_ALL = ("Movies", "Series")

# Estantes que NUNCA llevan tile, por decision y no por tamaño. Su carpeta se cuelga
# del padre, asi que no se pierde nada: se entra por el estante de arriba.
# 8-sep-2026, en dos pasos: primero cayeron los dos de animacion ("quitemos los tiles
# de animacion, lo animado va en peliculas o series"), y al verlo Beren recupero el de
# peliculas: "dame el tile para animacion pero solo para peliculas, no quiero las
# series". Cinco series animadas no llenan un estante; 45 peliculas si.
SIEMPRE_DENTRO = {"Animated Series"}


def api(base, key, ruta, cuerpo=None, metodo=None):
    url = base.rstrip("/") + ruta + ("&" if "?" in ruta else "?") + "api_key=" + key
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=datos, method=metodo or ("POST" if datos else "GET"))
    if datos:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        cuerpo_resp = r.read()
        return json.loads(cuerpo_resp) if cuerpo_resp else None


def cuenta_titulos(carpeta):
    ruta = os.path.join(MEDIA_ROOT, carpeta)
    try:
        return sum(1 for n in os.listdir(ruta) if os.path.isdir(os.path.join(ruta, n)))
    except OSError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="aplicar (por defecto: simulacro)")
    ap.add_argument("--min", type=int, default=MIN_TITULOS)
    args = ap.parse_args()

    base = os.environ["EMBY_URL"]
    key = os.environ["EMBY_API_KEY"]
    libs = {v["Name"]: v for v in api(base, key, "/Library/VirtualFolders")}

    # Los estantes de idioma son symlinks (/storage/virtual) hacia los mismos archivos:
    # path distinto = item distinto en Emby, asi que su fila en "Novedades" repite la del
    # catalogo (Beren, 2026-09-06 y otra vez el 08 cuando este script piso el arreglo).
    # El tile debe seguir visible, asi que van fuera de Novedades SIEMPRE, sin tocar
    # MyMediaExcludes. Se deduce de la ruta: una library nueva basada en symlinks queda
    # cubierta sin que nadie tenga que acordarse.
    sin_novedades = {v["Guid"] for v in libs.values()
                     if any(l.startswith(VIRTUAL_ROOT) for l in (v.get("Locations") or []))}

    conteo = {nombre: cuenta_titulos(carpeta) for nombre, carpeta, _ in ESTANTES}
    visible = {nombre: nombre not in SIEMPRE_DENTRO and n >= args.min
               for nombre, n in conteo.items()}
    for nombre in CATCH_ALL:
        visible[nombre] = True

    def destino(nombre):
        """El primer ancestro visible: donde debe colgarse un estante flaco."""
        for n, _c, padre in ESTANTES:
            if n == nombre:
                return padre if visible.get(padre, True) else destino(padre)
        return None

    print("estante            titulos  minimo=%d  tile" % args.min)
    cambios = []
    for nombre, carpeta, _padre in ESTANTES:
        lib = libs.get(nombre)
        if not lib:
            print("  %-18s AVISO: no existe la biblioteca en Emby" % nombre)
            continue
        ruta = os.path.join(MEDIA_ROOT, carpeta)
        dest = destino(nombre) if not visible[nombre] else None
        print("  %-18s %4d      %s%s" % (
            nombre, conteo[nombre], "si" if visible[nombre] else "NO",
            "" if visible[nombre] else "  -> dentro de " + str(dest)))

        # 1) la carpeta cuelga del padre SOLO mientras el estante este oculto. En cuanto
        #    tiene tile propio, se la quitamos a todos los demas: Beren los quiere
        #    disjuntos ("y que esos tiles no se mezclen por amor a cristo", 8-sep-2026),
        #    o sea que Peliculas ya no es el cajon que contiene tambien animacion y anime.
        #    Esto RE-CREA los items con otro ItemId, asi que el "visto" se salva antes con
        #    userdata_dump.py y se devuelve con userdata_restore.py, indexado por ruta.
        for otro_nombre, otro in libs.items():
            if otro_nombre == nombre:
                continue
            tiene = ruta in otro["Locations"]
            debe = (not visible[nombre]) and otro_nombre == dest
            if tiene and not debe:
                cambios.append(("quitar-ruta", otro, ruta, otro_nombre))
            elif debe and not tiene:
                cambios.append(("añadir-ruta", otro, ruta, otro_nombre))
        # 2) el tile
        cambios.append(("tile", lib, visible[nombre], nombre))

    usuarios = api(base, key, "/Users")
    ocultar = {c[1]["Guid"] for c in cambios if c[0] == "tile" and c[2] is False}
    mostrar = {c[1]["Guid"] for c in cambios if c[0] == "tile" and c[2] is True}
    # Las filas de "Novedades" van con los tiles: una biblioteca que no se ve tampoco
    # tiene por que empujar su propia fila en el home. Lo que entra sigue apareciendo,
    # porque la biblioteca ancha contiene a la estrecha.
    por_usuario = []
    for u in usuarios:
        cfg = u.get("Configuration", {})
        ex = list(cfg.get("MyMediaExcludes") or [])
        nuevo = [g for g in ex if g not in mostrar]
        nuevo += [g for g in ocultar if g not in nuevo]
        latest = list(cfg.get("LatestItemsExcludes") or [])
        latest_nuevo = nuevo + [g for g in sin_novedades if g not in nuevo]
        if sorted(nuevo) != sorted(ex) or sorted(latest) != sorted(latest_nuevo):
            por_usuario.append((u, cfg, nuevo, latest_nuevo))

    rutas = [c for c in cambios if c[0] in ("quitar-ruta", "añadir-ruta")]
    print("\nrutas a mover: %d   usuarios a actualizar: %d" % (len(rutas), len(por_usuario)))
    for accion, lib, ruta, nombre in rutas:
        print("  %s: %s %s %s" % (accion, os.path.basename(ruta),
                                  "<-" if accion == "añadir-ruta" else "x", nombre))
    if not args.apply:
        print("\n(simulacro: nada aplicado. Repetir con --apply)")
        return 0

    for accion, lib, ruta, _nombre in rutas:
        if accion == "añadir-ruta":
            api(base, key, "/Library/VirtualFolders/Paths",
                {"Id": lib["Id"], "PathInfo": {"Path": ruta}, "RefreshLibrary": False})
        else:
            api(base, key, "/Library/VirtualFolders/Paths/Delete",
                {"Id": lib["Id"], "Path": ruta, "RefreshLibrary": False})
    for u, cfg, nuevo, latest_nuevo in por_usuario:
        cfg = dict(cfg)
        cfg["MyMediaExcludes"] = nuevo
        cfg["LatestItemsExcludes"] = latest_nuevo
        api(base, key, "/Users/%s/Configuration" % u["Id"], cfg)
    if rutas:
        api(base, key, "/Library/Refresh", {})
    print("aplicado: %d rutas, %d usuarios%s" % (
        len(rutas), len(por_usuario), ", escaneo de biblioteca lanzado" if rutas else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
