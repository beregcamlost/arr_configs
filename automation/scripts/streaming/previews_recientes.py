#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Previews (.bif) de lo recien agregado, en ventanas cortas (12-sep-2026).

Emby genera los previews en UNA pasada diaria a las 07:00Z (decision del 10-sep: nunca
durante el escaneo, que fue lo que tumbo mubuntu el 9-sep). Eso deja sin preview a lo que
entra durante el dia hasta la madrugada siguiente (Beren: "hell mode no tiene preview").

Emby no deja pedir el preview de UN item, pero si arrancar y CANCELAR su tarea, y cada
.bif terminado queda en disco. Asi que esto corre cada 30 min y, si hay videos recientes
sin preview, arranca la tarea y la cancela a los VENTANA_MIN minutos: un episodio suelto
sale en la primera ventana; una temporada de 12 sale en dos o tres ventanas repartidas por
el dia, nunca de golpe. La pasada diaria de las 07:00Z sigue igual y cierra lo que quede.

Guardas: no arranca si hay escaneo, deteccion de intros o la propia tarea corriendo, ni con
la carga por encima de LOAD_GUARD_THRESHOLD. Espera REPOSO_MIN desde el import para que la
cola de codecs ya haya reescrito el archivo (el preview se hace sobre el definitivo).

  --pendientes   solo imprime cuantos videos recientes siguen sin preview (informe diario)
"""
import json, os, pathlib, sys, time, urllib.request

MEDIA = pathlib.Path("/APPBOX_DATA/storage/media")
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm", ".mov"}
TAREA = "Video preview thumbnail extraction"
OCUPADAS = {"Scan media library", "Detect Episode Intros", TAREA}
RECIENTE_H = 24        # que tan atras se mira: lo mas viejo ya lo hizo la pasada diaria
REPOSO_MIN = 20        # minutos desde el import antes de tocar el archivo
VENTANA_MIN = 15       # cuanto se deja correr la tarea antes de cancelarla
PASO_S = 15            # cada cuanto se mira si termino sola

U = os.environ["EMBY_URL"].rstrip("/")
K = os.environ["EMBY_API_KEY"]
UMBRAL = float(os.environ.get("LOAD_GUARD_THRESHOLD", "90"))


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def api(method, path):
    req = urllib.request.Request(U + "/emby" + path, method=method,
                                 headers={"X-Emby-Token": K, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def tareas():
    return {t["Name"]: t for t in api("GET", "/ScheduledTasks")}


def sin_preview(p):
    return not any(p.parent.glob(p.stem + "-*.bif"))


def pendientes():
    ahora = time.time()
    out = []
    for p in MEDIA.rglob("*"):
        if p.suffix.lower() not in VIDEO_EXT or not p.is_file():
            continue
        try:
            edad = ahora - p.stat().st_mtime
        except OSError:
            continue
        if REPOSO_MIN * 60 <= edad <= RECIENTE_H * 3600 and sin_preview(p):
            out.append(p)
    return out


def main():
    pend = pendientes()
    if "--pendientes" in sys.argv:
        print(len(pend))
        return 0
    if not pend:
        return 0            # nada que hacer: sin ruido en el log

    carga = float(open("/proc/loadavg").read().split()[0])
    if carga > UMBRAL:
        log(f"{len(pend)} sin preview, pero carga {carga:.0f} > {UMBRAL:.0f}: espero")
        return 0
    t = tareas()
    ocupada = [n for n in OCUPADAS if n in t and t[n].get("State") != "Idle"]
    if ocupada:
        log(f"{len(pend)} sin preview, pero corre '{ocupada[0]}': espero")
        return 0

    muestra = ", ".join(p.name[:50] for p in pend[:3]) + (" ..." if len(pend) > 3 else "")
    log(f"{len(pend)} video(s) recientes sin preview: {muestra}")
    tid = t[TAREA]["Id"]
    api("POST", f"/ScheduledTasks/Running/{tid}")
    t0 = time.time()
    cancelada = False
    while time.time() - t0 < VENTANA_MIN * 60:
        time.sleep(PASO_S)
        if tareas()[TAREA].get("State") == "Idle":
            break
    else:
        api("DELETE", f"/ScheduledTasks/Running/{tid}")
        cancelada = True
    hechos = sum(1 for p in pend if not sin_preview(p))
    if cancelada:
        cola = "tarea cancelada, el resto en la proxima ventana"
    elif hechos:
        cola = "la tarea termino sola"
    else:
        # Emby solo genera el preview de lo que nunca proceso: si el .bif se borro a mano
        # no lo rehace (probado 12-sep). Lo pendiente de verdad cae en la pasada diaria.
        cola = "Emby termino sin generar nada (ya lo daba por procesado)"
    log(f"ventana de {(time.time() - t0) / 60:.1f} min: {hechos} de {len(pend)} con preview; {cola}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
