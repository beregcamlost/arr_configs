#!/usr/bin/env python3
"""lang_from_arr.py (2026-08-31) — desatasca la cola de idioma con el idioma ORIGINAL.

POR QUE EXISTE: lang_id.sh manda el audio sin etiquetar a la 3090 y solo aplica lo que
sale con prob >= 0.70. Lo que queda por debajo (estado 'detected') y lo que sale con
ventanas discordantes ('ambiguous') se queda ahi PARA SIEMPRE: volver a pasarlo por la
GPU da el mismo numero, porque el audio no cambia. Medido el 2026-08-31: 65 ambiguous
+ 10 detected llevaban semanas girando sin poder avanzar.

LA IDEA: no hace falta mas computo, hace falta una SEGUNDA FUENTE. Radarr y Sonarr ya
guardan el `originalLanguage` de TMDB de cada titulo. Cuando el voto del detector
coincide con ese idioma, dos fuentes independientes dicen lo mismo y la etiqueta es
buena aunque la confianza por ventana fuera 0.61.

QUE HACE Y QUE NO: este script NO escribe en los archivos. Solo resuelve la fila
(status='detected', prob=1.0) y deja que `lang_id.sh apply` — que ya sabe de
mkvpropedit, MP4Box, remux con verificacion, exentos y refresh de Emby — haga el
trabajo. Una sola ruta de escritura, la que ya estaba probada.

REGLAS (en orden):
  A. cruzado : la mayoria del detector == idioma original -> se aplica.
  B. familia : la mayoria y el original son el mismo idioma con otro nombre (español y
               gallego/catalan, serbio y croata, malayo e indonesio, las variantes del
               noruego) -> se etiqueta con el original, que es la fuente fiable.
  C. doblaje : mayoria que NO coincide con el original. Suele ser un doblaje real (audio
               ingles de un anime japones, o "Emilia Perez", francesa y hablada en
               español) y la etiqueta correcta es lo que SUENA, no el original; pero ahi
               el original no confirma nada, asi que va detras de --doblajes.
Lo que no cae en ninguna se queda como esta: sin evidencia cruzada no se inventa nada.

VENTANAS DEBILES (prob < 0.60) NO VOTAN. Sin este filtro:
  - "Goodnight Mommy" (alemana) salia nn 3/5 con 0.48/0.56/0.54 contra de 2/5 con
    0.99/0.87. El ruido ganaba por cantidad a la evidencia.
  - "The Red Turtle", que NO TIENE DIALOGOS, salia en 4/5... con 0.30-0.55 en cada
    ventana: whisper balbuceando sobre musica. Filtrado se queda sin votos y sin
    etiqueta, que es exactamente lo correcto.
El 'nn' (nynorsk) es el falso positivo recurrente de este detector; casi siempre cae
por debajo del filtro solo.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

DB = os.environ.get("CODEC_DB", "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db")

# whisper habla ISO 639-1; los contenedores quieren 639-2. mkvpropedit/MP4Box normalizan
# al codigo bibliografico (zho->chi), y lang_id.sh ya trata esos pares como equivalentes.
ISO1_A_ISO3 = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita", "pt": "por",
    "ru": "rus", "ja": "jpn", "zh": "zho", "ko": "kor", "th": "tha", "da": "dan",
    "nl": "nld", "no": "nor", "nn": "nno", "sv": "swe", "tr": "tur", "hi": "hin",
    "id": "ind", "pl": "pol", "ar": "ara", "he": "heb", "fi": "fin", "cs": "ces",
    "el": "ell", "hu": "hun", "ro": "ron", "uk": "ukr", "vi": "vie", "fa": "fas",
    "ta": "tam", "te": "tel", "ms": "msa", "tl": "tgl", "ca": "cat", "gl": "glg",
    "eu": "eus", "la": "lat", "cy": "cym", "is": "isl", "sk": "slk", "bg": "bul",
    "sr": "srp", "hr": "hrv", "lt": "lit", "lv": "lav", "et": "est", "bn": "ben",
    "ur": "urd", "ml": "mal", "kn": "kan", "mr": "mar", "my": "mya", "km": "khm",
}
# nombres tal como los devuelve el arr (vienen de TMDB)
NOMBRE_A_ISO3 = {
    "english": "eng", "spanish": "spa", "japanese": "jpn", "french": "fra",
    "korean": "kor", "chinese": "zho", "mandarin": "zho", "cantonese": "zho",
    "german": "deu", "thai": "tha", "russian": "rus", "italian": "ita",
    "hindi": "hin", "indonesian": "ind", "norwegian": "nor", "portuguese": "por",
    "danish": "dan", "dutch": "nld", "flemish": "nld", "swedish": "swe",
    "polish": "pol", "turkish": "tur", "arabic": "ara", "hebrew": "heb",
    "finnish": "fin", "czech": "ces", "greek": "ell", "hungarian": "hun",
    "romanian": "ron", "ukrainian": "ukr", "vietnamese": "vie", "persian": "fas",
    "tamil": "tam", "telugu": "tel", "malay": "msa", "filipino": "tgl",
    "tagalog": "tgl", "catalan": "cat", "galician": "glg", "basque": "eus",
    "icelandic": "isl", "slovak": "slk", "bulgarian": "bul", "serbian": "srp",
    "croatian": "hrv", "latin": "lat", "malayalam": "mal", "bengali": "ben",
    "urdu": "urd", "marathi": "mar", "burmese": "mya", "khmer": "khm",
}
# Idiomas que el detector intercambia porque en 30 segundos de audio son el mismo:
# el original de TMDB manda dentro de cada grupo.
FAMILIAS = (
    {"spa", "glg", "cat"},          # gallego y catalan salen como español y al reves
    {"srp", "hrv", "bos"},          # serbocroata: un idioma con tres nombres
    {"msa", "ind"},                 # malayo e indonesio
    {"nor", "nno", "nob"},          # las tres formas del noruego
    {"zho", "yue"},                 # mandarin y cantones comparten codigo en el detector
)
PROB_MINIMA_VENTANA = 0.60
VOTOS_RE = re.compile(r"\[([^\]]*)")


def misma_familia(a, b):
    return any(a in f and b in f for f in FAMILIAS)


def arr_index():
    """{carpeta_del_titulo: iso3} leido de Radarr y Sonarr. La carpeta es prefijo del path."""
    idx = {}
    for base_env, key_env, endpoint, campo in (
        ("RADARR_URL", "RADARR_KEY", "/api/v3/movie", "folderName"),
        ("SONARR_URL", "SONARR_KEY", "/api/v3/series", "path"),
    ):
        base = os.environ.get(base_env, "").rstrip("/")
        key = os.environ.get(key_env, "")
        if not base or not key:
            print("aviso: falta " + base_env + "/" + key_env + " en el entorno", file=sys.stderr)
            continue
        req = urllib.request.Request(base + endpoint, headers={"X-Api-Key": key})
        with urllib.request.urlopen(req, timeout=120) as r:
            for item in json.load(r):
                carpeta = item.get(campo) or item.get("path")
                nombre = ((item.get("originalLanguage") or {}).get("name") or "").lower()
                iso3 = NOMBRE_A_ISO3.get(nombre)
                if carpeta and iso3:
                    idx[carpeta.rstrip("/")] = iso3
    return idx


def titulo_de(path, idx):
    """(carpeta_del_titulo, idioma_original) — la carpeta mas larga del arr que sea prefijo."""
    mejor = None
    for carpeta, iso3 in idx.items():
        if path.startswith(carpeta + "/") and (mejor is None or len(carpeta) > len(mejor[0])):
            mejor = (carpeta, iso3)
    return mejor if mejor else (None, None)


def voto(fila):
    """(iso3_ganador, ventanas_ganadas, ventanas_validas) segun lo que dejo el worker.

    'detected' trae el veredicto ya resuelto en lang/prob. 'ambiguous' guarda las cinco
    ventanas crudas en `error`: "votos=4/5 share=0.85 [en:0.99,it:0.84,...]".
    """
    if fila["status"] == "detected" and fila["lang"]:
        return fila["lang"], 5, 5
    m = VOTOS_RE.search(fila["error"] or "")
    if not m:
        return None, 0, 0
    cuenta = {}
    total = 0
    for par in m.group(1).split(","):
        trozos = par.split(":")
        code = trozos[0].strip()
        if not code:
            continue
        total += 1
        try:
            prob = float(trozos[1])
        except (IndexError, ValueError):
            prob = 0.0
        if prob < PROB_MINIMA_VENTANA:   # ventana debil: no vota (ver cabecera)
            continue
        iso3 = ISO1_A_ISO3.get(code, code)
        cuenta[iso3] = cuenta.get(iso3, 0) + 1
    if not cuenta:
        return None, 0, 0
    gana = max(cuenta, key=lambda k: cuenta[k])
    return gana, cuenta[gana], sum(cuenta.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="escribir en la cola (por defecto: simulacro)")
    ap.add_argument("--doblajes", action="store_true", help="regla C: mayoria >=4/5 que no coincide con el original")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    filas = con.execute(
        "SELECT d.media_id, d.stream_index, d.status, d.lang, d.prob, d.error, mf.path "
        "FROM audio_lang_detect d JOIN media_files mf ON mf.id = d.media_id "
        "WHERE d.status IN ('detected','ambiguous') AND mf.deleted_at IS NULL "
        "ORDER BY mf.path").fetchall()
    if not filas:
        print("cola limpia: nada atascado")
        return 0

    idx = arr_index()
    print(str(len(filas)) + " filas atascadas | " + str(len(idx)) + " titulos con idioma original en Radarr/Sonarr\n")

    resueltas = []
    saltadas = []
    fantasmas = []
    for f in filas:
        carpeta, orig = titulo_de(f["path"], idx)
        gana, n, tot = voto(f)
        nombre = os.path.basename(f["path"])
        # El archivo se remuxeo o se renombro y esta fila quedo apuntando al nombre
        # viejo: no hay nada que etiquetar y sin sacarla se reintenta cada media hora
        # para siempre. Es el mismo estado terminal que ya usa lang_id.sh.
        if not os.path.exists(f["path"]):
            fantasmas.append((f, nombre))
            continue
        if not gana:
            saltadas.append((nombre, "ninguna ventana llego a " + str(PROB_MINIMA_VENTANA),
                             orig, None, carpeta, f))
            continue
        if orig and gana == orig:
            regla = "cruzado"
        elif orig and misma_familia(gana, orig):
            regla = "familia"
            gana = orig
        elif args.doblajes and n >= 2 and n * 5 >= 3 * tot:
            regla = "doblaje"
        else:
            saltadas.append((nombre, "voto " + gana + " " + str(n) + "/" + str(tot),
                             orig, gana, carpeta, f))
            continue
        resueltas.append((f, gana, regla, n, tot, orig))

    # REGLA D, hermanos: un episodio mudo o con musica encima no da votos, pero si el
    # resto de SU MISMA serie ya se resolvio al idioma original, ese episodio habla lo
    # mismo. Sin esto, "El Pueblo" quedaba con 7 episodios etiquetados y uno colgado
    # para siempre. Solo aplica cuando el detector no dijo nada que contradiga.
    ya = set()
    for f, lang, _r, _n, _t, _o in resueltas:
        carpeta_res, _ = titulo_de(f["path"], idx)
        if carpeta_res:
            ya.add((carpeta_res, lang))
    pendientes = []
    for entrada in saltadas:
        nombre, motivo, orig, gana, carpeta, f = entrada
        if gana is None and orig and carpeta and (carpeta, orig) in ya:
            resueltas.append((f, orig, "hermanos", 0, 0, orig))
        else:
            pendientes.append(entrada)
    saltadas = pendientes

    for f, lang, regla, n, tot, orig in resueltas:
        print("  [%-8s] %s  (voto %d/%d, original %s)  %s" % (
            regla, lang, n, tot, orig or "?", os.path.basename(f["path"])[:70]))
    print("\n== resueltas: %d   fantasmas (archivo ya no existe): %d   sin resolver: %d"
          % (len(resueltas), len(fantasmas), len(saltadas)))
    for f, nombre in fantasmas:
        print("  [fantasma] %s" % nombre[:70])
    for nombre, motivo, orig, gana, _carpeta, _f in saltadas:
        print("  [sin evidencia] voto=%s original=%s  (%s)  %s" % (
            gana or "-", orig or "?", motivo, nombre[:60]))

    if not args.apply:
        print("\n(simulacro: nada escrito. Repetir con --apply)")
        return 0

    for f, lang, regla, n, tot, orig in resueltas:
        con.execute(
            "UPDATE audio_lang_detect SET status='detected', lang=?, prob=1.0, error=? "
            "WHERE media_id=? AND stream_index=?",
            (lang,
             "resuelto por idioma original (" + regla + "): arr=" + (orig or "?") + " voto=" + str(n) + "/" + str(tot),
             f["media_id"], f["stream_index"]))
    for f, _nombre in fantasmas:
        con.execute(
            "UPDATE audio_lang_detect SET status='failed', error='file_gone' "
            "WHERE media_id=? AND stream_index=?", (f["media_id"], f["stream_index"]))
    con.commit()
    print("\n%d filas puestas en 'detected' con prob=1.0 (las etiqueta el cron de "
          "`lang_id.sh apply`, cada 30 min) y %d fantasmas fuera de la cola."
          % (len(resueltas), len(fantasmas)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
