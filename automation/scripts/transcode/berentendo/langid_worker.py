#!/usr/bin/env python3
"""langid_worker.py (2026-08-13) — identificacion de idioma en el 3090.

Topologia (la misma que tanda3090.py): mubuntu guarda los medios y la DB; berentendo
hace la inferencia. Este worker reclama filas pendientes, le pide a mubuntu que extraiga
2 muestras de audio, se las trae, corre faster-whisper y escribe el resultado de vuelta.

Si berentendo esta apagado no pasa nada: las filas quedan pendientes y mubuntu libera
los claims viejos (release_stale_3090_claims.sh cubre el prefijo gpu3090@).

LIMITE CONOCIDO: Whisper devuelve "es" tanto para latino como para castellano. No
distingue el doblaje; para eso haria falta un clasificador de acento. La etiqueta que
escribimos es `spa`, que es lo que necesita el filtro de Emby.
"""
import argparse
import os
import shlex
import subprocess
import sys
import time

HOST = "mubuntu"
RDB = "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
LANG_ID_SH = "/config/berenstuff/automation/scripts/transcode/lang_id.sh"
SAMPLE_DIR = "/APPBOX_DATA/storage/.transcode-state-media/langid-samples"
LOCAL_TMP = "/tmp/langid_samples"
MODEL_SIZE = os.environ.get("LANGID_MODEL", "small")
MIN_PROB = float(os.environ.get("LANGID_MIN_PROB", "0.70"))

# Whisper devuelve ISO 639-1; los contenedores quieren ISO 639-2/B.
ISO2 = {
    "es": "spa", "en": "eng", "ja": "jpn", "pt": "por", "fr": "fra", "de": "deu",
    "it": "ita", "ru": "rus", "ko": "kor", "zh": "zho", "nl": "nld", "pl": "pol",
    "tr": "tur", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "he": "heb",
    "ar": "ara", "hi": "hin", "th": "tha", "cs": "ces", "hu": "hun", "el": "ell",
    "ro": "ron", "uk": "ukr", "vi": "vie", "id": "ind", "ca": "cat", "eu": "eus",
}


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# Multiplexado de conexion: medido el 2026-08-13, cada ssh nuevo a mubuntu cuesta
# ~2.8 s solo de handshake, y el worker hace ~6 llamadas por pista = ~17 s de pura
# latencia contra ~3 s de trabajo real. Con ControlMaster se reutiliza UNA sesion y
# las llamadas siguientes bajan a decenas de milisegundos.
MUX = ["-o", "ControlMaster=auto",
       "-o", "ControlPath=/tmp/langid-mux-%r@%h:%p",
       "-o", "ControlPersist=900"]


def ssh(cmd, timeout=300):
    """Corre un comando en mubuntu y devuelve (rc, stdout)."""
    p = subprocess.run(["ssh"] + MUX + [HOST, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip()


def dbq(sql, write=False):
    """Consulta la DB de mubuntu por ssh. El timeout alto es porque el cron de
    conversion toma el lock de escritura durante los swaps."""
    cmd = "sqlite3 -cmd '.timeout 30000' %s %s" % (shlex.quote(RDB), shlex.quote(sql))
    rc, out = ssh(cmd)
    if rc != 0:
        log("  ! sqlite rc=%s: %s" % (rc, out[:200]))
    return out


def worklist(limit):
    sql = ("SELECT d.media_id || '|' || d.stream_index FROM audio_lang_detect d "
           "JOIN media_files mf ON mf.id = d.media_id "
           "WHERE d.status='pending' AND d.claimed_by IS NULL AND mf.deleted_at IS NULL "
           "ORDER BY d.media_id LIMIT %d;" % limit)
    out = dbq(sql)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if "|" in line:
            a, b = line.split("|", 1)
            if a.isdigit() and b.strip().lstrip("-").isdigit():
                items.append((int(a), int(b)))
    return items


def claim(media_id, stream_index):
    tok = "gpu3090@%d" % int(time.time())
    sql = ("UPDATE audio_lang_detect SET claimed_by='%s' "
           "WHERE media_id=%d AND stream_index=%d AND claimed_by IS NULL; "
           "SELECT changes();" % (tok, media_id, stream_index))
    return dbq(sql, write=True).strip().endswith("1")


def unclaim(media_id, stream_index):
    dbq("UPDATE audio_lang_detect SET claimed_by=NULL WHERE media_id=%d AND stream_index=%d;"
        % (media_id, stream_index), write=True)


def finish(media_id, stream_index, lang, prob, status="detected", error=None):
    err = "NULL" if error is None else "'%s'" % str(error).replace("'", "")[:200]
    lang_sql = "NULL" if lang is None else "'%s'" % lang
    prob_sql = "NULL" if prob is None else "%.4f" % prob
    dbq("UPDATE audio_lang_detect SET status='%s', lang=%s, prob=%s, error=%s, "
        "detected_at=CURRENT_TIMESTAMP, claimed_by=NULL "
        "WHERE media_id=%d AND stream_index=%d;"
        % (status, lang_sql, prob_sql, err, media_id, stream_index), write=True)


def gpu_busy(util_max=25, mem_max_mb=8000):
    """Mismo guard que tanda3090.py: no molestar si Beren esta jugando."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=30).stdout.strip()
        util, mem = [int(x.strip()) for x in out.splitlines()[0].split(",")]
        return (util >= util_max or mem >= mem_max_mb), util, mem
    except Exception:
        return False, -1, -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--auto", action="store_true", help="respeta el guard de GPU")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.auto and not args.force:
        busy, util, mem = gpu_busy()
        if busy:
            log("SKIP corrida: GPU ocupada (util=%s%% mem=%sMB)" % (util, mem))
            return 0

    items = worklist(args.limit)
    log("worklist: %d pistas pendientes (limit=%d)" % (len(items), args.limit))
    if not items:
        return 0

    from faster_whisper import WhisperModel, decode_audio
    log("cargando modelo whisper '%s' en cuda..." % MODEL_SIZE)
    model = WhisperModel(MODEL_SIZE, device="cuda", compute_type="float16")

    os.makedirs(LOCAL_TMP, exist_ok=True)
    ok = fail = disagree = 0

    for media_id, stream_index in items:
        if not claim(media_id, stream_index):
            continue
        try:
            rc, out = ssh("bash %s extract %d %d" % (LANG_ID_SH, media_id, stream_index), timeout=600)
            if rc != 0 or not out.startswith("OK"):
                finish(media_id, stream_index, None, None, status="failed",
                       error="extract:%s" % (out or rc))
                fail += 1
                continue

            remote = "%s/%d_%d.ogg" % (SAMPLE_DIR, media_id, stream_index)
            local = "%s/%d_%d.ogg" % (LOCAL_TMP, media_id, stream_index)
            r = subprocess.run(["scp", "-q"] + MUX + ["%s:%s" % (HOST, remote), local],
                               capture_output=True, timeout=300)
            ssh("rm -f %s" % remote, timeout=60)
            if r.returncode != 0 or not os.path.exists(local):
                finish(media_id, stream_index, None, None, status="failed", error="scp_failed")
                fail += 1
                continue

            got = []
            try:
                # detect_language espera el audio YA decodificado (float32), no una ruta:
                # pasarle el path da "'str' object has no attribute 'dtype'".
                audio = decode_audio(local, sampling_rate=16000)
                win = 30 * 16000
                for start in range(0, len(audio), win):
                    chunk = audio[start:start + win]
                    if len(chunk) < 5 * 16000:      # cola demasiado corta para decidir
                        continue
                    lang, prob, _ = model.detect_language(chunk)
                    got.append((lang, float(prob)))
            except Exception as exc:
                log("  ! detect fallo media=%d: %s" % (media_id, exc))
            finally:
                if os.path.exists(local):
                    os.remove(local)

            if not got:
                finish(media_id, stream_index, None, None, status="failed", error="no_samples")
                fail += 1
                continue

            # Voto PONDERADO: cada ventana aporta su confianza al idioma que eligio.
            # Asi una ventana de musica o silencio (que suele dar confianza baja y un
            # idioma raro) no puede imponerse sobre 3-4 ventanas de dialogo real.
            tally = {}
            for lang, prob in got:
                tally[lang] = tally.get(lang, 0.0) + prob
            winner = max(tally, key=tally.get)
            votes = [p for lg, p in got if lg == winner]
            share = tally[winner] / sum(tally.values())
            conf = sum(votes) / len(got)          # confianza media sobre TODAS las ventanas
            iso3 = ISO2.get(winner, winner)
            detail = ",".join("%s:%.2f" % (lg, p) for lg, p in got)

            # Unanimidad manda: 5 ventanas repartidas por toda la pelicula coincidiendo
            # es mas fuerte que una sola con confianza alta. Sin esta regla, "28 Days
            # Later" (5/5 en ingles, conf media 0.61 por dos ventanas de accion sin
            # dialogo) caia como ambiguo siendo obviamente ingles.
            unanime = len(votes) == len(got) and len(got) >= 3 and conf >= 0.50
            if unanime or (len(votes) >= 3 and share >= 0.5 and conf >= MIN_PROB):
                finish(media_id, stream_index, iso3, conf)
                ok += 1
                log("  %d/%d -> %s (conf=%.2f, %d/%d ventanas)"
                    % (media_id, stream_index, iso3, conf, len(votes), len(got)))
            else:
                finish(media_id, stream_index, iso3, conf, status="ambiguous",
                       error="votos=%d/%d share=%.2f [%s]" % (len(votes), len(got), share, detail))
                disagree += 1
        except Exception as exc:
            log("  ! excepcion media=%d: %s" % (media_id, exc))
            unclaim(media_id, stream_index)
            fail += 1

    log("TANDA LANGID DONE ok=%d ambiguos=%d fallidos=%d" % (ok, disagree, fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
