#!/usr/bin/env python3
"""Transcribe audio ESPANOL a .es.srt con faster-whisper large-v3 en la 3090.

Para episodios doblados al latino que no tienen subtitulo en ningun proveedor. Como el
audio YA es espanol, no hay traduccion de por medio: es transcripcion directa, que es
el mejor caso posible en calidad y en tiempos.

El audio se saca de mubuntu por ssh en streaming (16 kHz mono flac, ~20 MB) en vez de
bajar el video entero (~700 MB). El .es.srt se devuelve por scp.

Reanudable: si el destino ya existe en mubuntu, se salta. Se puede matar y relanzar.
"""
import ctypes
import glob
import os
import subprocess
import sys
import time

# Las libs de CUDA 12 viven en los paquetes pip del venv de CT2, no en el sistema ni en
# el venv de whisper. LD_LIBRARY_PATH exportado desde el shell NO basta aqui: el modelo
# CARGA bien y revienta despues, al computar ("libcublas.so.12 is not found"). Precargarlas
# con ctypes antes de importar faster_whisper si funciona, porque quedan en el proceso.
for _pat in ("cublas", "cudnn", "cuda_nvrtc"):
    for _so in sorted(glob.glob(
            "/mnt/d/emby/ct2-venv/lib/python3.12/site-packages/nvidia/%s/lib/*.so*" % _pat)):
        try:
            ctypes.CDLL(_so, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass

MUB = "mubuntu"
RAIZ = "/APPBOX_DATA/storage/media/"
MODELO = "/mnt/d/emby/whisper-models/models--Systran--faster-whisper-large-v3/snapshots"
# Los temporales van a disco NATIVO de WSL, no a /mnt/d. En DrvFs el .srt recien
# escrito no era visible para el proceso scp ("stat local: No such file or
# directory") por el cacheo de metadatos entre procesos. En ext4 no pasa.
TMP = "/tmp/whisper-staging"
LOG = "/mnt/d/emby/whisper-staging/whisper_es.log"


def log(msg):
    linea = time.strftime("%H:%M:%S ") + msg
    print(linea, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(linea + "\n")


def ruta_modelo():
    for d in sorted(os.listdir(MODELO)):
        p = os.path.join(MODELO, d)
        if os.path.isfile(os.path.join(p, "model.bin")):
            return p
    raise SystemExit("no encontre el snapshot del modelo en " + MODELO)


def ts(seg):
    ms = int(round(seg * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


GPU_UTIL_MAX = 25
GPU_MEM_MAX_MB = 8000


def gpu_ocupada():
    """Mismos umbrales que tanda3090.py. Si Beren volvio a jugar, la tanda se para sola.

    Se le suman 3 GB al umbral de memoria porque el propio Whisper ocupa la tarjeta:
    sin ese descuento se auto-detectaria como "alguien esta jugando".
    """
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
        u, m = [int(x.strip()) for x in r.stdout.strip().splitlines()[0].split(",")]
    except Exception:
        return False, 0, 0
    return (u >= GPU_UTIL_MAX and m >= GPU_MEM_MAX_MB + 3000), u, m


def existe_remoto(ruta):
    r = subprocess.run(["ssh", MUB, "test -f %s && echo si || echo no" % sh(ruta)],
                       capture_output=True, text=True, timeout=60)
    return r.stdout.strip() == "si"


def sh(p):
    return "'" + p.replace("'", "'\\''") + "'"


def sacar_audio(remoto, destino):
    """ffmpeg corre EN mubuntu y manda el flac por el tubo de ssh: no viaja el video."""
    cmd = ("ffmpeg -nostdin -v error -i %s -vn -sn -ac 1 -ar 16000 -c:a flac -f flac -"
           % sh(remoto))
    with open(destino, "wb") as fh:
        p = subprocess.run(["ssh", MUB, cmd], stdout=fh, stderr=subprocess.PIPE, timeout=1800)
    if p.returncode != 0 or os.path.getsize(destino) < 100000:
        raise RuntimeError("ffmpeg remoto fallo: %s" % p.stderr.decode()[:200])


MAX_SEG = 6.5      # segundos por cue
MAX_HUECO = 1.5    # silencio que rompe la cue en vez de quedar dentro
MAX_CHARS = 84     # dos lineas de 42, el estandar de subtitulado
CORTE = ".?!"


def reagrupar(segs):
    """De segmentos de Whisper a cues legibles, usando las marcas por palabra.

    Corta al llegar al limite de duracion o de caracteres, y prefiere cortar tras un
    punto o signo de cierre. Un segmento sin palabras se deja tal cual.
    """
    cues = []
    ini = fin = None
    buf = []
    for s in segs:
        palabras = getattr(s, "words", None)
        if not palabras:
            t = s.text.strip()
            if t:
                cues.append((s.start, s.end, t))
            continue
        for w in palabras:
            # Corte por HUECO: si entre la palabra anterior y esta hay mas de MAX_HUECO
            # de silencio, la cue se cierra antes de tragarselo. Sin esto salia una cue
            # de 89 s que abarcaba el opening entero (visto en S01E02).
            if buf and fin is not None and (w.start - fin) > MAX_HUECO:
                cues.append((ini, fin, "".join(buf).strip()))
                ini = fin = None
                buf = []
            if ini is None:
                ini = w.start
            buf.append(w.word)
            fin = w.end
            texto = "".join(buf).strip()
            if (fin - ini) >= MAX_SEG or len(texto) >= MAX_CHARS or (
                    texto[-1:] in CORTE and len(texto) > 12):
                cues.append((ini, fin, texto))
                ini = fin = None
                buf = []
    if buf and ini is not None:
        cues.append((ini, fin, "".join(buf).strip()))
    return [c for c in cues if c[2]]


def main():
    lista = sys.argv[1]
    limite_min = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    os.makedirs(TMP, exist_ok=True)

    from faster_whisper import WhisperModel
    mp = ruta_modelo()
    log("cargando modelo %s" % os.path.basename(mp))
    modelo = WhisperModel(mp, device="cuda", compute_type="float16")
    log("modelo listo")

    rutas = [l.strip() for l in open(lista, encoding="utf-8") if l.strip()]
    log("%d episodios en la lista" % len(rutas))
    arranque = time.time()
    hechos = fallos = saltados = 0

    for i, rel in enumerate(rutas, 1):
        if limite_min and (time.time() - arranque) / 60 >= limite_min:
            log("STOP: se acabo la ventana de %s min" % limite_min)
            break
        remoto = RAIZ + rel
        base = rel.rsplit(".", 1)[0]
        destino_remoto = RAIZ + base + ".es.srt"
        nombre = os.path.basename(base)

        ocupada, u, m = gpu_ocupada()
        if ocupada:
            log("STOP: la GPU subio a %d%% con %d MB - la tarjeta esta en uso" % (u, m))
            break

        if existe_remoto(destino_remoto):
            saltados += 1
            continue

        t0 = time.time()
        audio = os.path.join(TMP, "audio.flac")
        srt = os.path.join(TMP, "salida.srt")
        try:
            sacar_audio(remoto, audio)
            t_audio = time.time() - t0

            segs, info = modelo.transcribe(
                audio, language="es", beam_size=5, vad_filter=True,
                # Sin esto Whisper se engancha repitiendo la ultima frase en los
                # silencios largos, que en anime son muchos.
                condition_on_previous_text=False,
                # Sin word_timestamps el VAD junta el silencio del opening con la
                # primera frase y sale una cue de 78 s (visto en S01E01). Con las
                # marcas por palabra se reagrupa en cues de duracion humana.
                word_timestamps=True,
            )
            cues = reagrupar(segs)
            n = 0
            with open(srt, "w", encoding="utf-8", newline=chr(10)) as fh:
                for ini, fin, txt in cues:
                    n += 1
                    fh.write("%d" % n + chr(10) + ts(ini) + " --> " + ts(fin)
                             + chr(10) + txt + chr(10) + chr(10))
            if n < 20:
                raise RuntimeError("solo %d cues, sospechoso" % n)

            # Los nombres traen apostrofos ("You're. Kyosuke") y espacios: se sube a
            # un nombre neutro y se mueve con comillas del lado remoto.
            subprocess.run(["scp", "-q", srt, "%s:/tmp/whisper_out.srt" % MUB],
                           check=True, timeout=300)
            subprocess.run(["ssh", MUB, "mv /tmp/whisper_out.srt %s && chmod 664 %s"
                            % (sh(destino_remoto), sh(destino_remoto))],
                           check=True, timeout=120)
            hechos += 1
            log("[%d/%d] OK %d cues en %.1f min (audio %.0fs, dur %.0f min) %s"
                % (i, len(rutas), n, (time.time() - t0) / 60, t_audio,
                   info.duration / 60, nombre[:52]))
        except Exception as exc:
            fallos += 1
            log("[%d/%d] FALLO %s: %s" % (i, len(rutas), nombre[:40], str(exc)[:150]))
        finally:
            for f in (audio, srt):
                if os.path.exists(f):
                    os.remove(f)

    log("FIN: %d hechos, %d fallos, %d ya estaban, en %.1f min"
        % (hechos, fallos, saltados, (time.time() - arranque) / 60))
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
