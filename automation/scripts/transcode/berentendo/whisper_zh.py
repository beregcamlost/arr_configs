#!/usr/bin/env python3
"""Traduce audio NO-ingles a .en.srt con faster-whisper large-v3 (task=translate) en la 3090.

Para episodios cuyo audio no es ni ingles ni espanol (donghua chino, anime coreano) y
que no tienen subtitulo en ningun proveedor. Whisper SOLO sabe traducir hacia el ingles
(task="translate"), asi que el camino completo es de dos saltos:

    audio zh  --whisper translate-->  .en.srt  --campeon CT2 en->es-->  .es.srt

El idioma de origen se AUTODETECTA a proposito: la etiqueta del contenedor miente
(4 Cut Hero viene marcado "chi" siendo coreano). Se registra lo que detecto para poder
auditarlo despues.

El .en.srt se sube a mubuntu y ademas queda una copia local numerada + manifiesto, que
es lo que consume el paso 2 (srt_en_es.py). Reanudable: si la copia local ya existe, se
salta.

DECODIFICACION POR LOTES (2026-08-31). La 3090 se quedaba al 36-42% con 6.7 GB de 24 y
215 W de 350: el cuello no era la tarjeta sino que la decodificacion va palabra a palabra.
Lanzar DOS procesos a la vez no gano nada -- 2.07 min por episodio en los dos casos, solo
que cada uno tardaba el doble. Lo que si funciona es BatchedInferencePipeline, que mete
varias ventanas del MISMO audio en el lote:

    secuencial  1.9 min/episodio  288 cues
    batch_size=8  0.35 min        284 cues  (-1.4%, mismos tiempos y frases)

Medido sobre un episodio ya transcrito por el camino viejo, y ademas con los otros dos
procesos compitiendo. 5.4x. WHISPER_BATCH=0 vuelve al camino secuencial.
"""
import ctypes
import glob
import os
import subprocess
import sys
import time

# Mismas libs de CUDA 12 del venv de CT2 precargadas con ctypes: LD_LIBRARY_PATH no
# basta, el modelo carga bien y revienta al computar. Ver whisper_es.py.
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
TMP = "/tmp/whisper-staging"
LOCAL_EN = os.environ.get("WHISPER_OUT_DIR", "/mnt/d/emby/fixwork/in_tr3")
MANIFIESTO = os.environ.get("WHISPER_MANIFIESTO", "/mnt/d/emby/whisper-staging/zh_manifiesto.tsv")
LOG = os.environ.get("WHISPER_LOG", "/mnt/d/emby/whisper-staging/whisper_zh.log")

APOS = chr(39)
NL = chr(10)
TAB = chr(9)


def log(msg):
    linea = time.strftime("%H:%M:%S ") + msg
    print(linea, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(linea + NL)


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


# El guard existe para que la tanda se pare sola si Beren se pone a jugar. Con lotes la
# tanda usa mas VRAM ella misma, asi que el umbral se ajusta por entorno: el 2026-08-31
# una prueba propia lo disparo y paro los dos workers en marcha.
GPU_UTIL_MAX = int(os.environ.get("GPU_UTIL_MAX", "25"))
GPU_MEM_MAX_MB = int(os.environ.get("GPU_MEM_MAX_MB", "8000"))


def gpu_ocupada():
    """Si Beren volvio a jugar, la tanda se para sola. +3 GB por el propio Whisper."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
        u, m = [int(x.strip()) for x in r.stdout.strip().splitlines()[0].split(",")]
    except Exception:
        return False, 0, 0
    return (u >= GPU_UTIL_MAX and m >= GPU_MEM_MAX_MB + 3000), u, m


def sh(p):
    """Cita para el shell remoto. Los nombres traen apostrofos y parentesis."""
    return APOS + p.replace(APOS, APOS + chr(92) + APOS + APOS) + APOS


def sacar_audio(remoto, destino):
    """ffmpeg corre EN mubuntu y manda el flac por el tubo de ssh: no viaja el video."""
    cmd = ("ffmpeg -nostdin -v error -i %s -vn -sn -ac 1 -ar 16000 -c:a flac -f flac -"
           % sh(remoto))
    with open(destino, "wb") as fh:
        p = subprocess.run(["ssh", MUB, cmd], stdout=fh, stderr=subprocess.PIPE, timeout=1800)
    if p.returncode != 0 or os.path.getsize(destino) < 100000:
        raise RuntimeError("ffmpeg remoto fallo: %s" % p.stderr.decode()[:200])


BATCH = int(os.environ.get("WHISPER_BATCH", "8"))

MAX_SEG = 6.5
MAX_HUECO = 1.5
MAX_CHARS = 84
CORTE = ".?!"


def reagrupar(segs):
    """De segmentos de Whisper a cues legibles usando las marcas por palabra."""
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
            # No cortar delante de una contraccion inglesa: el hueco cae DENTRO de la
            # palabra ("I" / "'m going to marry you") y parte la frase en dos cues sin
            # sentido. Visto en Sword Saint S01E01, cue 27-28.
            contraccion = w.word.strip().startswith((APOS, chr(8217)))
            if buf and fin is not None and (w.start - fin) > MAX_HUECO and not contraccion:
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
    return pegar_contracciones([c for c in cues if c[2]])


def pegar_contracciones(cues):
    """Pega a la cue anterior la que arranque con una contraccion.

    Evitar el corte por hueco no alcanza: cualquiera de los otros tres cortes
    (duracion, caracteres, fin de frase) puede caer igual entre "don" y "'t", que
    Whisper devuelve como dos palabras. Esto los barre todos.
    """
    salida = []
    for ini, fin, txt in cues:
        if salida and txt.startswith((APOS, chr(8217))):
            salida[-1] = (salida[-1][0], fin, salida[-1][2] + txt)
            continue
        salida.append((ini, fin, txt))
    return salida


def main():
    lista = sys.argv[1]
    limite_min = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    os.makedirs(TMP, exist_ok=True)
    os.makedirs(LOCAL_EN, exist_ok=True)

    from faster_whisper import WhisperModel, BatchedInferencePipeline
    mp = ruta_modelo()
    log("cargando modelo %s" % os.path.basename(mp))
    modelo = WhisperModel(mp, device="cuda", compute_type="float16")
    lote = BatchedInferencePipeline(model=modelo) if BATCH else None
    log("modelo listo%s" % (" (lotes de %d)" % BATCH if BATCH else " (secuencial)"))

    rutas = [l.strip() for l in open(lista, encoding="utf-8") if l.strip()]
    log("%d episodios en la lista" % len(rutas))
    arranque = time.time()
    hechos = fallos = saltados = 0
    manifiesto = []

    for i, rel in enumerate(rutas, 1):
        idx = "%03d" % i
        base = rel.rsplit(".", 1)[0]
        copia_local = os.path.join(LOCAL_EN, idx + ".en.srt")
        manifiesto.append((idx, base))
        nombre = os.path.basename(base)

        if os.path.exists(copia_local) and os.path.getsize(copia_local) > 100:
            saltados += 1
            continue
        if limite_min and (time.time() - arranque) / 60 >= limite_min:
            log("STOP: se acabo la ventana de %s min" % limite_min)
            break

        ocupada, u, m = gpu_ocupada()
        if ocupada:
            log("STOP: la GPU subio a %d%% con %d MB - la tarjeta esta en uso" % (u, m))
            break

        t0 = time.time()
        remoto = RAIZ + rel
        destino_remoto = RAIZ + base + ".en.srt"
        audio = os.path.join(TMP, "audio.flac")
        srt = os.path.join(TMP, "salida.srt")
        try:
            sacar_audio(remoto, audio)
            t_audio = time.time() - t0

            if lote is not None:
                segs, info = lote.transcribe(
                    audio, task="translate", batch_size=BATCH, vad_filter=True,
                    word_timestamps=True,
                )
            else:
                segs, info = modelo.transcribe(
                    audio, task="translate", beam_size=5, vad_filter=True,
                    condition_on_previous_text=False, word_timestamps=True,
                )
            cues = reagrupar(segs)
            n = 0
            with open(srt, "w", encoding="utf-8", newline=NL) as fh:
                for ini, fin, txt in cues:
                    n += 1
                    fh.write("%d" % n + NL + ts(ini) + " --> " + ts(fin)
                             + NL + txt + NL + NL)
            if n < 20:
                raise RuntimeError("solo %d cues, sospechoso" % n)

            # La copia local va DESPUES de tener el srt completo: es el marcador de
            # reanudacion del paso 1 y la entrada del paso 2.
            subprocess.run(["cp", srt, copia_local], check=True, timeout=120)
            subprocess.run(["scp", "-q", srt, "%s:/tmp/whisper_out.srt" % MUB],
                           check=True, timeout=300)
            subprocess.run(["ssh", MUB, "mv /tmp/whisper_out.srt %s && chmod 664 %s"
                            % (sh(destino_remoto), sh(destino_remoto))],
                           check=True, timeout=120)
            hechos += 1
            log("[%d/%d] OK %s(%.2f) %d cues en %.1f min (audio %.0fs, dur %.0f min) %s"
                % (i, len(rutas), info.language, info.language_probability, n,
                   (time.time() - t0) / 60, t_audio, info.duration / 60, nombre[:44]))
        except Exception as exc:
            fallos += 1
            log("[%d/%d] FALLO %s: %s" % (i, len(rutas), nombre[:40], str(exc)[:150]))
        finally:
            for f in (audio, srt):
                if os.path.exists(f):
                    os.remove(f)

    with open(MANIFIESTO, "w", encoding="utf-8", newline=NL) as fh:
        for idx, base in manifiesto:
            fh.write(idx + TAB + base + NL)

    log("FIN: %d hechos, %d fallos, %d ya estaban, en %.1f min"
        % (hechos, fallos, saltados, (time.time() - arranque) / 60))
    return 0 if fallos == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
