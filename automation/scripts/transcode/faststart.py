#!/usr/bin/env python3
"""faststart.py (2026-09-10) — pone el indice (moov) de los mp4 al principio, sin recodificar.

EL PROBLEMA: 1321 de 1614 mp4/m4v/mov de la biblioteca (1.66 TB) tienen el indice al
FINAL del archivo. Para empezar a reproducir por HTTP, el cliente tiene que ir a buscarlo
al final antes de mostrar el primer cuadro: en conexiones lentas eso es arranque lento.
Nuestros scripts ya escriben con +faststart; estos llegan asi de la descarga, porque el
hook de importacion solo remuxea MKV->MP4 y deja pasar los mp4 nativos.

COMO: ffmpeg -c copy -movflags +faststart a una carpeta temporal FUERA de la biblioteca
(mismo disco), verificacion (mismas pistas, misma duracion, indice adelante, tamaño
+-1%), se conservan permisos y FECHA de modificacion, y reemplazo atomico. Pilotos del
10-sep (Rick and Morty S01E04 con intro marcado; 1978 via el arbol /virtual, vista por
dos usuarios): Emby no noto NADA -- mismo item, mismas fechas, mismos intros, mismo
visto, mismo preview. El tamaño queda identico, asi que el pipeline de codecs tampoco.

SE SALTA: archivos con hardlink (siguen sembrando: reescribirlos duplicaria el espacio),
tocados hace menos de 24 h, en cola o en curso en el pipeline de codecs (los va a
reescribir el), o que alguien esta viendo. Se pausa si Emby esta escaneando. Tres fallos
seguidos cortan la corrida.

Uso: faststart.py [--apply] [--max-min 90] [--max-gb 0] [--max-files 0] [--chicos-primero]
"""
import argparse
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
TMPDIR = "/APPBOX_DATA/storage/.faststart-tmp"
CODEC_DB = "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
HECHOS = "/config/berenstuff/automation/logs/faststart_hechos.jsonl"
EXT = (".mp4", ".m4v", ".mov")
GRACIA_H = 24
MARGEN_DISCO = 50e9

SQL_EN_CURSO = "select current_file from pipeline_state where current_file is not null and current_file <> ?"
SQL_EN_COLA = """select mf.path from conversion_plan cp join media_files mf on mf.id = cp.media_id
                 where cp.eligible = 1 and mf.deleted_at is null"""


def log(m):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + m, flush=True)


def emby(path, **q):
    q["api_key"] = os.environ["EMBY_API_KEY"]
    url = os.environ["EMBY_URL"].rstrip("/") + "/emby" + path + "?" + urllib.parse.urlencode(q)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read())


def moov_al_final(p):
    """True si el moov va despues del mdat; False si va antes; None si no se pudo leer."""
    try:
        with open(p, "rb") as h:
            off, tam, orden = 0, os.path.getsize(p), []
            while off < tam and len(orden) < 16:
                hd = h.read(8)
                if len(hd) < 8:
                    break
                sz, t = struct.unpack(">I4s", hd)
                if sz == 1:
                    sz = struct.unpack(">Q", h.read(8))[0]
                elif sz == 0:
                    sz = tam - off
                if sz < 8:
                    return None
                orden.append(t)
                if b"moov" in orden and b"mdat" in orden:
                    break
                off += sz
                h.seek(off)
        if b"moov" in orden and b"mdat" in orden:
            return orden.index(b"moov") > orden.index(b"mdat")
    except OSError:
        pass
    return None


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name:format=duration", "-of", "json", p],
                       capture_output=True, text=True, timeout=300)
    d = json.loads(r.stdout or "{}")
    # Solo video/audio/subtitulos: los mp4 de WEBRip traen pistas "data" (bin_data: timecode,
    # metadatos del muxer) que ffmpeg -c copy no re-empaqueta. Compararlas hacia fallar la
    # verificacion de 18 archivos por corrida (12-sep-2026) sin que faltara nada reproducible.
    return ([(s.get("codec_type"), s.get("codec_name")) for s in d.get("streams", [])
             if s.get("codec_type") in ("video", "audio", "subtitle")],
            float(d.get("format", {}).get("duration") or 0))


def ocupados_codec():
    """Rutas que el pipeline de codecs esta convirtiendo o tiene en cola."""
    s = set()
    try:
        c = sqlite3.connect(f"file:{CODEC_DB}?mode=ro", uri=True, timeout=10)
        for (p,) in c.execute(SQL_EN_CURSO, ("",)):
            s.add(os.path.realpath(p))
        for (p,) in c.execute(SQL_EN_COLA):
            s.add(os.path.realpath(p))
    except sqlite3.Error as e:
        log(f"AVISO: no pude leer la base de codecs ({e}); no me salto nada por ese motivo")
    return s


def viendo():
    ids = [x["NowPlayingItem"]["Id"] for x in emby("/Sessions", ActiveWithinSeconds=180)
           if x.get("NowPlayingItem")]
    if not ids:
        return set()
    items = emby("/Items", Ids=",".join(ids), Fields="Path")["Items"]
    return {os.path.realpath(i["Path"]) for i in items if i.get("Path")}


def avisar_emby(p):
    """Refresco de validacion del item de Emby que apunta a este archivo.

    El indice adelante cambia el tamaño unos bytes y, como se conserva el mtime a
    proposito (si no, la pelicula saltaria a "Novedades"), Emby no se entera solo: 13
    peliculas quedaron con el MediaSource desactualizado el 11-sep. Library/Media/Updated
    "Modified" tampoco, ni ValidationOnly (probado 12-sep): solo Items/{id}/Refresh
    FullRefresh (sin ReplaceAll) relee el archivo; por item, no compite con el vigilante.
    Los items viven en /virtual (enlaces), asi que se busca por titulo y se compara la
    ruta real.
    """
    try:
        titulo = os.path.basename(os.path.dirname(p))
        cab = {"X-Emby-Token": os.environ["EMBY_API_KEY"]}
        base = os.environ["EMBY_URL"].rstrip("/") + "/emby"
        q = urllib.parse.urlencode({"SearchTerm": titulo.split(" (")[0], "Recursive": "true",
                                    "IncludeItemTypes": "Movie,Episode", "Fields": "Path", "Limit": 200})
        with urllib.request.urlopen(urllib.request.Request(base + "/Items?" + q, headers=cab), timeout=60) as r:
            items = json.loads(r.read()).get("Items", [])
        ids = [it["Id"] for it in items if it.get("Path") and os.path.realpath(it["Path"]) == os.path.realpath(p)]
        for i in ids:
            req = urllib.request.Request(
                base + f"/Items/{i}/Refresh?MetadataRefreshMode=FullRefresh&ImageRefreshMode=Default&ReplaceAllMetadata=false&ReplaceAllImages=false",
                method="POST", headers=cab)
            urllib.request.urlopen(req, timeout=60).read()
        if not ids:
            log(f"  aviso a Emby: no encontre el item de {titulo} (lo recogera el escaneo)")
    except Exception as e:   # no fatal: el archivo ya quedo bien en disco
        log(f"  aviso a Emby fallo (no fatal): {e}")


def escaneando():
    return next(t for t in emby("/ScheduledTasks") if t["Name"] == "Scan media library")["State"] != "Idle"


def candidatos():
    limite = time.time() - GRACIA_H * 3600
    out, salto = [], {"hardlink": 0, "reciente": 0, "ilegible": 0}
    for dp, _dn, fn in os.walk(MEDIA_ROOT):
        for f in fn:
            if not f.lower().endswith(EXT):
                continue
            p = os.path.join(dp, f)
            m = moov_al_final(p)
            if m is None:
                salto["ilegible"] += 1
                continue
            if not m:
                continue
            st = os.stat(p)
            if st.st_nlink > 1:
                salto["hardlink"] += 1
            elif st.st_mtime > limite:
                salto["reciente"] += 1
            else:
                out.append((p, st.st_size))
    return out, salto


def procesar(p):
    st = os.stat(p)
    if shutil.disk_usage(TMPDIR).free < st.st_size * 1.1 + MARGEN_DISCO:
        return "sin espacio en disco"
    tmp = os.path.join(TMPDIR, f"{st.st_ino}.faststart.tmp")
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", p, "-map", "0", "-c", "copy",
                            "-movflags", "+faststart", "-f", "mp4", "-y", tmp],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            return "ffmpeg: " + r.stderr.strip()[-150:]
        a, b = probe(p), probe(tmp)
        tam = os.path.getsize(tmp)
        if a[0] != b[0] or abs(a[1] - b[1]) > 0.1 or moov_al_final(tmp) is not False \
                or abs(tam - st.st_size) > st.st_size * 0.01:
            return f"verificacion: pistas {a[0] == b[0]} dur {a[1]:.2f}->{b[1]:.2f} tamaño {st.st_size}->{tam}"
        if os.stat(p).st_mtime_ns != st.st_mtime_ns:
            return "el original cambio mientras tanto"
        os.chmod(tmp, st.st_mode & 0o7777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, p)
        avisar_emby(p)
        return "ok"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="reescribir (por defecto: simulacro)")
    ap.add_argument("--max-min", type=float, default=90, help="tope de minutos por corrida")
    ap.add_argument("--max-gb", type=float, default=0, help="tope de GB por corrida (0 = sin tope)")
    ap.add_argument("--max-files", type=int, default=0, help="tope de archivos (0 = sin tope)")
    ap.add_argument("--chicos-primero", action="store_true")
    a = ap.parse_args()
    os.makedirs(TMPDIR, exist_ok=True)
    for f in os.listdir(TMPDIR):  # restos de una corrida cortada
        q = os.path.join(TMPDIR, f)
        if f.endswith(".faststart.tmp") and time.time() - os.path.getmtime(q) > 3600:
            os.remove(q)
    cand, salto = candidatos()
    ocup = ocupados_codec()
    cola = [(p, s) for p, s in cand if os.path.realpath(p) not in ocup]
    cola.sort(key=lambda x: x[1] if a.chicos_primero else x[0])
    salto["pipeline_codecs"] = len(cand) - len(cola)
    log(f"sin faststart: {len(cand) + salto['hardlink'] + salto['reciente']} | en cola: {len(cola)}"
        f" ({sum(s for _, s in cola) / 1e12:.2f} TB) | saltados: {salto}")
    if not a.apply:
        log("(simulacro: nada tocado. Repetir con --apply)")
        return 0
    t0, gb, hechos, fallos, seguidos = time.time(), 0.0, 0, 0, 0
    for p, s in cola:
        if (time.time() - t0) / 60 > a.max_min or (a.max_gb and gb >= a.max_gb) \
                or (a.max_files and hechos >= a.max_files):
            break
        espera = 0
        while escaneando() and espera < 1800:
            if espera == 0:
                log("Emby esta escaneando: pauso")
            time.sleep(60)
            espera += 60
        if escaneando():
            log("el escaneo sigue tras 30 min: corto la corrida")
            break
        rel = p[len(MEDIA_ROOT) + 1:]
        if os.path.realpath(p) in viendo():
            log(f"  lo estan viendo, queda para otra vez: {rel}")
            continue
        t1 = time.time()
        res = procesar(p)
        if res == "ok":
            hechos += 1
            gb += s / 1e9
            seguidos = 0
            with open(HECHOS, "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    "path": p, "size": s}) + "\n")
            log(f"  ok {s / 1e9:6.2f} GB {time.time() - t1:5.1f}s {rel}")
        else:
            fallos += 1
            seguidos += 1
            log(f"  FALLO ({res}): {rel}")
            if seguidos >= 3:
                log("3 fallos seguidos: corto la corrida para revisar")
                break
        time.sleep(1)
    log(f"corrida: {hechos} ok ({gb:.1f} GB), {fallos} fallos, {(time.time() - t0) / 60:.1f} min")
    return 0 if seguidos < 3 else 4


if __name__ == "__main__":
    sys.exit(main())
