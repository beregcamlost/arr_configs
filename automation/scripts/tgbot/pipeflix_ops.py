#!/usr/bin/env python3
"""pipeflix_ops.py — operaciones que el bot de Telegram ejecuta sobre el servidor.
Todo es sincrono (se llama con asyncio.to_thread desde el bot). Nada aqui decide
permisos: eso lo hace pipeflix_bot.py con los perfiles admin / moderador / usuario.

Bloques:
  Emby      : item por id, streams, refrescar caratula/metadata, caratulas faltantes,
              sesiones activas, reiniciar (con guard de reproduccion).
  Bazarr    : bajar el mejor sub de un idioma (pelicula / episodio), traducir con Bazarr
              (Google, para fuentes no inglesas), estado de subs por pelicula/serie.
  Traductor : translation/translator.py --file (nuestro modelo subtitler-nmt, en->es).
  Releases  : busqueda interactiva en Radarr/Sonarr filtrada por idioma + grab.
  Tareas    : catalogo cerrado de scripts del pipeline que el admin puede lanzar.
  Sistema   : ultimo bloque de pipeline_health.log, disco, salud de Emby.
"""
import os
import pathlib
import re
import shutil
import subprocess
import time
import unicodedata

import requests

SCRIPTS = pathlib.Path("/config/berenstuff/automation/scripts")
LOGS = pathlib.Path("/config/berenstuff/automation/logs")
REPO = pathlib.Path("/config/berenstuff")
MEDIA = pathlib.Path("/APPBOX_DATA/storage/media")

RADARR_URL = os.environ["RADARR_URL"]
RADARR_KEY = os.environ["RADARR_KEY"]
SONARR_URL = os.environ["SONARR_URL"]
SONARR_KEY = os.environ["SONARR_KEY"]
EMBY_API = os.environ.get("EMBY_INTERNAL_BASE") or (os.environ["EMBY_URL"] + "/emby")
EMBY_KEY = os.environ["EMBY_API_KEY"]
BAZARR_URL = os.environ.get("BAZARR_URL", "http://127.0.0.1:6767/bazarr").rstrip("/")
BAZARR_KEY = os.environ.get("BAZARR_API_KEY", "")

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov", ".webm")

# Idiomas: como los escribe la gente -> (nombre en Radarr/Sonarr, codigos ISO en Emby, code2 Bazarr)
LANGS = {
    "espanol": ("Spanish", {"spa", "es", "esp", "es-es", "es-419", "es-mx", "es-la", "lat", "latin", "spanish", "castilian"}, "es"),
    "ingles": ("English", {"eng", "en", "english"}, "en"),
    "frances": ("French", {"fra", "fre", "fr", "french"}, "fr"),
    "japones": ("Japanese", {"jpn", "ja", "japanese"}, "ja"),
    "italiano": ("Italian", {"ita", "it", "italian"}, "it"),
    "aleman": ("German", {"ger", "deu", "de", "german"}, "de"),
    "portugues": ("Portuguese", {"por", "pt", "pt-br", "portuguese"}, "pt"),
    "coreano": ("Korean", {"kor", "ko", "korean"}, "ko"),
    "chino": ("Chinese", {"chi", "zho", "zh", "zt", "chinese"}, "zh"),
    "ruso": ("Russian", {"rus", "ru", "russian"}, "ru"),
    "hindi": ("Hindi", {"hin", "hi", "hindi"}, "hi"),
}
LANG_ALIASES = {"latino": "espanol", "castellano": "espanol", "espaol": "espanol", "spanish": "espanol",
                "english": "ingles", "japanese": "japones", "french": "frances", "italian": "italiano", "german": "aleman",
                "brasileno": "portugues", "portuguese": "portugues", "korean": "coreano", "mandarin": "chino",
                "cantones": "chino", "chinese": "chino", "russian": "ruso"}
LANG_LABEL = {"espanol": "Español", "ingles": "Inglés", "frances": "Francés", "japones": "Japonés", "italiano": "Italiano",
              "aleman": "Alemán", "portugues": "Portugués", "coreano": "Coreano", "chino": "Chino", "ruso": "Ruso", "hindi": "Hindi"}
# pistas en el NOMBRE de la release cuando el indexer no reporta idiomas
LANG_TITLE_HINTS = {
    "espanol": r"dual[\s.-]*lat|latino|castellano|spanish|\besp\b|\bspa\b|\blat\b|dual[\s.-]*audio|multi",
    "frances": r"vostfr|vff|vfq|french|\bfre?\b|truefrench|multi",
    "japones": r"japanese|\bjpn?\b|multi", "italiano": r"italian|\bita\b|multi", "aleman": r"german|\bger\b|multi",
    "portugues": r"portuguese|dublado|\bpt-?br\b|multi", "coreano": r"korean|\bkor\b|multi", "chino": r"chinese|mandarin|multi",
    "ruso": r"russian|\brus\b|multi", "hindi": r"hindi|\bhin\b|multi", "ingles": r".",
}


def fold(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def norm_lang(word):
    """'francés' / 'Latino' / 'french' -> clave de LANGS o None."""
    w = fold(word)
    w = LANG_ALIASES.get(w, w)
    return w if w in LANGS else None


# ---------------------------------------------------------------- http
def _arr(base, key, method, path, **kw):
    timeout = kw.pop("timeout", 30)
    r = requests.request(method, f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, timeout=timeout, **kw)
    r.raise_for_status()
    return r.json() if r.content else {}


def emby(method, path, timeout=30, **params):
    params["api_key"] = EMBY_KEY
    r = requests.request(method, f"{EMBY_API}/{path}", params=params, timeout=timeout)
    r.raise_for_status()
    if r.content and r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {}


def bazarr(method, path, timeout=60, **data):
    if not BAZARR_KEY:
        raise RuntimeError("BAZARR_API_KEY vacio")
    if method == "GET":
        r = requests.get(f"{BAZARR_URL}/api/{path}", headers={"X-API-KEY": BAZARR_KEY}, params=data, timeout=timeout)
    else:
        r = requests.request(method, f"{BAZARR_URL}/api/{path}", headers={"X-API-KEY": BAZARR_KEY}, data=data, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------- Emby
def emby_item(item_id):
    items = emby("GET", "Items", Ids=item_id, Fields="Path,MediaStreams,ProviderIds,ImageTags,ProductionYear").get("Items", [])
    return items[0] if items else None


def emby_children(series_id):
    """Episodios de una serie con ruta y streams."""
    return emby("GET", "Items", ParentId=series_id, Recursive="true", IncludeItemTypes="Episode",
                Fields="Path,MediaStreams,ParentIndexNumber,IndexNumber,ImageTags", Limit=2000).get("Items", [])


def streams_summary(item):
    """-> (audio_langs:set, sub_langs:set) en codigos crudos (minusculas)."""
    a, s = set(), set()
    for st in item.get("MediaStreams") or []:
        lang = (st.get("Language") or "und").lower()
        if st.get("Type") == "Audio":
            a.add(lang)
        elif st.get("Type") == "Subtitle":
            s.add(lang)
    return a, s


def has_lang(codes, lang_key):
    return bool(codes & LANGS[lang_key][1])


def emby_refresh(item_id, replace_images=True):
    """Vuelve a pedir metadatos e imagenes a TMDB/TVDB para un item (y sus hijos)."""
    emby("POST", f"Items/{item_id}/Refresh", MetadataRefreshMode="FullRefresh", ImageRefreshMode="FullRefresh",
         ReplaceAllImages="true" if replace_images else "false", ReplaceAllMetadata="false", Recursive="true")
    return True


def emby_missing_images(limit=60):
    """Items sin imagen Primary (pelis, series, temporadas, episodios). -> (lista recortada, total)"""
    data = emby("GET", "Items", Recursive="true", IncludeItemTypes="Movie,Series,Season,Episode",
                Fields="ImageTags,DateCreated,SeriesName", Limit=20000, timeout=90).get("Items", [])
    out = [it for it in data if not (it.get("ImageTags") or {}).get("Primary")]
    out.sort(key=lambda it: it.get("DateCreated") or "", reverse=True)
    return out[:limit], len(out)


def emby_sessions():
    """Quien esta viendo algo ahora."""
    out = []
    for s in emby("GET", "Sessions"):
        np = s.get("NowPlayingItem")
        if not np:
            continue
        name = np.get("Name")
        if np.get("SeriesName"):
            name = f"{np['SeriesName']} · {name}"
        pos = (s.get("PlayState") or {}).get("PositionTicks") or 0
        tot = np.get("RunTimeTicks") or 0
        out.append({"user": s.get("UserName"), "client": s.get("Client"), "device": s.get("DeviceName"), "item": name,
                    "paused": bool((s.get("PlayState") or {}).get("IsPaused")),
                    "pct": int(100 * pos / tot) if tot else None})
    return out


def emby_restart(wait_s=180):
    """POST System/Restart y espera a que vuelva. -> segundos que tardo (o -1 si no volvio)."""
    emby("POST", "System/Restart")
    t0 = time.time()
    time.sleep(8)
    while time.time() - t0 < wait_s:
        try:
            emby("GET", "System/Info", timeout=5)
            return int(time.time() - t0)
        except Exception:
            time.sleep(5)
    return -1


def emby_scan_folder(path):
    """Aviso puntual (el camino seguro; el refresco global duplica items si choca con el vigilante)."""
    requests.post(f"{EMBY_API}/Library/Media/Updated", params={"api_key": EMBY_KEY},
                  json={"Updates": [{"Path": str(path), "UpdateType": "Modified"}]}, timeout=30).raise_for_status()


# ---------------------------------------------------------------- Bazarr
def bazarr_movie(radarr_id):
    d = bazarr("GET", "movies", **{"radarrid[]": radarr_id}).get("data") or []
    return d[0] if d else None


def bazarr_episodes(sonarr_series_id):
    return bazarr("GET", "episodes", **{"seriesid[]": sonarr_series_id}).get("data") or []


def bazarr_download_movie(radarr_id, code2="es"):
    """Baja el mejor subtitulo disponible en los proveedores de Bazarr."""
    return bazarr("PATCH", "movies/subtitles", radarrid=radarr_id, language=code2, forced="false", hi="false", timeout=240)


def bazarr_download_episode(sonarr_series_id, sonarr_episode_id, code2="es"):
    return bazarr("PATCH", "episodes/subtitles", seriesid=sonarr_series_id, episodeid=sonarr_episode_id,
                  language=code2, forced="false", hi="false", timeout=240)


def bazarr_translate(kind, media_id, source_srt, code2="es"):
    """Traduccion de Bazarr (Google, sin clave): sirve para fuentes NO inglesas (fr/it/ja...)."""
    return bazarr("PATCH", "subtitles", action="translate", language=code2, path=str(source_srt),
                  type="movie" if kind == "m" else "episode", id=media_id, forced="False", hi="False", timeout=600)


# ---------------------------------------------------------------- subtitulos en disco
_SRT_LANG = {"spa": "es", "eng": "en", "fre": "fr", "fra": "fr", "ita": "it", "jpn": "ja", "ger": "de", "deu": "de",
             "por": "pt", "kor": "ko", "chi": "zh", "zho": "zh"}
_SRT_KNOWN = set(_SRT_LANG) | {"es", "en", "fr", "it", "ja", "de", "pt", "ko", "zh", "zt", "und"}


def sidecars(video_path):
    """{'es': [ruta...], 'en': [...]} de los .srt junto al video."""
    p = pathlib.Path(video_path)
    out = {}
    if not p.parent.is_dir():
        return out
    stem = p.stem
    for f in p.parent.iterdir():
        if f.suffix.lower() != ".srt" or not f.name.startswith(stem):
            continue
        tags = f.name[len(stem):].lower().strip(".").split(".")[:-1]  # quita 'srt'
        lang = next((t for t in tags if t in _SRT_KNOWN), "und")
        lang = _SRT_LANG.get(lang, lang)
        out.setdefault(lang, []).append(str(f))
    return out


def translate_with_model(video_path, redo=False, timeout_s=2400):
    """Nuestro modelo (translation/translator.py -> subtitler-nmt via shim): en->es del video dado.
    redo=True: aparta el .es.srt actual (.bak-<ts>) para que el traductor lo rehaga.
    -> (ok:bool, resumen:str)"""
    video_path = str(video_path)
    if not os.path.exists(video_path):
        return False, "el archivo no existe en disco"
    moved = []
    if not redo and sidecars(video_path).get("es"):
        return False, "ya tiene .es.srt; el traductor no lo pisa (usa «Rehacer» para reemplazarlo)"
    if redo:
        for srt in sidecars(video_path).get("es", []):
            bak = f"{srt}.bak-{time.strftime('%Y%m%d%H%M%S')}"
            shutil.move(srt, bak)
            moved.append(bak)
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    cmd = ["/usr/bin/python3", str(SCRIPTS / "translation/translator.py"), "translate", "--file", video_path]
    try:
        r = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"se paso de {timeout_s // 60} min; sigue en segundo plano o fallo"
    out = (r.stdout + "\n" + r.stderr).strip()
    es_now = sidecars(video_path).get("es", [])
    ok = r.returncode == 0 and bool(es_now)
    tail = "\n".join(out.splitlines()[-6:])
    if moved and not ok:  # devolver lo que apartamos
        for bak in moved:
            try:
                shutil.move(bak, bak.rsplit(".bak-", 1)[0])
            except Exception:
                pass
        tail += "\n(restaure el .es.srt anterior)"
    return ok, tail[-1500:]


def sqm_enqueue(video_paths):
    """Encola el/los archivos para la proxima pasada de subtitle_quality_manager auto-maintain (01:00Z)."""
    cmd = ["/bin/bash", str(SCRIPTS / "subtitles/subtitle_quality_manager.sh"), "enqueue", *map(str, video_paths)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=os.environ, stdin=subprocess.DEVNULL)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-800:]


# ---------------------------------------------------------------- releases por idioma (Radarr/Sonarr)
def _release_row(x, lang_key):
    langs = [l.get("name") for l in x.get("languages") or []]
    want = LANGS[lang_key][0] if lang_key else None
    hint = lang_key is None or re.search(LANG_TITLE_HINTS[lang_key], x.get("title", ""), re.I) is not None
    return {"title": x.get("title", ""), "size_gb": round((x.get("size") or 0) / 1e9, 1), "seeders": x.get("seeders") or 0,
            "langs": langs, "quality": ((x.get("quality") or {}).get("quality") or {}).get("name", "?"),
            "indexer": x.get("indexer", "?"), "score": x.get("customFormatScore", 0), "rejected": bool(x.get("rejected")),
            "rejections": x.get("rejections") or [], "guid": x.get("guid"), "indexerId": x.get("indexerId"),
            "lang_match": (want in langs) or hint, "lang_sure": want in langs}


def releases(kind, arr_id, lang_key, season=None, limit=8):
    """Busqueda interactiva. -> (releases que parecen traer el idioma, mejores primero; total crudo)"""
    if kind == "m":
        raw = _arr(RADARR_URL, RADARR_KEY, "GET", "release", params={"movieId": arr_id}, timeout=120)
    else:
        params = {"seriesId": arr_id}
        if season:
            params["seasonNumber"] = season
        raw = _arr(SONARR_URL, SONARR_KEY, "GET", "release", params=params, timeout=120)
    rows = [_release_row(x, lang_key) for x in raw]
    rows = [r for r in rows if r["lang_match"] and r["seeders"] > 0]
    if lang_key:
        rows.sort(key=lambda r: (r["lang_sure"], not r["rejected"], r["score"], r["seeders"]), reverse=True)
    else:  # sin idioma: la mejor copia (no rechazada, mejor custom format, mas seeders)
        rows.sort(key=lambda r: (not r["rejected"], r["score"], r["seeders"]), reverse=True)
    return rows[:limit], len(raw)


def grab(kind, guid, indexer_id):
    base, key = (RADARR_URL, RADARR_KEY) if kind == "m" else (SONARR_URL, SONARR_KEY)
    return _arr(base, key, "POST", "release", json={"guid": guid, "indexerId": indexer_id}, timeout=120)


def arr_movie(arr_id):
    return _arr(RADARR_URL, RADARR_KEY, "GET", f"movie/{arr_id}")


def arr_series(arr_id):
    return _arr(SONARR_URL, SONARR_KEY, "GET", f"series/{arr_id}")


# ---------------------------------------------------------------- tareas (catalogo cerrado)
TASKS = {
    "salud":      ("🩺 Salud de Emby (duplicados, sin entrar, sin caratula, sin subs es)", ["python3", "streaming/emby_salud.py"], 180),
    "estante":    ("🗂 Estante de idioma (es_shelf: enlaza lo nuevo con audio es)", ["python3", "streaming/es_shelf.py"], 600),
    "previews":   ("🎞 Previews de lo reciente (ventana de 15 min)", ["python3", "streaming/previews_recientes.py"], 1200),
    "huerfanas":  ("🧹 Carpetas huerfanas (simulacro)", ["python3", "streaming/orphan_folders.py"], 300),
    "huerfanas-borrar": ("🧹 Carpetas huerfanas (BORRAR)", ["python3", "streaming/orphan_folders.py", "--apply"], 300),
    "torrents":   ("🧲 Torrents manuales ya importados (simulacro)", ["python3", "transmission_cleanup_manual.py"], 120),
    "torrents-borrar": ("🧲 Torrents manuales ya importados (BORRAR)", ["python3", "transmission_cleanup_manual.py", "--apply"], 120),
    "faststart":  ("⚡ faststart (simulacro, 30 min max)", ["python3", "transcode/faststart.py", "--max-min", "30"], 2400),
    "pipeline":   ("📋 pipeline_health ahora", ["/bin/bash", "pipeline_health.sh"], 300),
    "traductor":  ("🤖 Estado del traductor (ultimas corridas)", ["/usr/bin/python3", "translation/translator.py", "status"], 120),
    "librarian":  ("📚 Librarian scan (simulacro)", ["python3", "streaming/librarian.py", "scan"], 600),
    "bot-reiniciar": ("♻️ Reiniciar este bot", None, 0),
}


def run_task(name):
    """-> (ok, salida recortada). Solo nombres de TASKS."""
    if name not in TASKS:
        return False, "tarea desconocida"
    label, argv, timeout = TASKS[name]
    if name == "bot-reiniciar":
        subprocess.Popen(["setsid", "/bin/bash", str(SCRIPTS / "tgbot/bot_up.sh"), "restart"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, "reiniciando; vuelvo en ~30 s"
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    try:
        r = subprocess.run(argv, cwd=str(SCRIPTS), env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, f"se paso de {timeout // 60} min"
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    lines = out.splitlines()
    if len(lines) > 30:
        out = "\n".join(lines[:4] + ["…"] + lines[-25:])
    return r.returncode == 0, out[-3500:] or "(sin salida)"


# ---------------------------------------------------------------- sistema
def health_last_block():
    """Ultimo bloque completo de pipeline_health.log (cada 15 min). -> (severidad, [lineas], fecha)"""
    try:
        text = (LOGS / "pipeline_health.log").read_text(errors="ignore")[-60000:]
    except FileNotFoundError:
        return "?", ["sin pipeline_health.log"], ""
    closes = [m for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[pipeline_health\] Overall severity: (\w+)", text, re.M)]
    if not closes:
        return "?", [], ""
    last = closes[-1]
    start = closes[-2].end() if len(closes) > 1 else 0
    body = [l for l in text[start:last.start()].splitlines() if l.startswith("[")]
    return last.group(2), body, last.group(1)


def disk():
    out = []
    for p in ("/APPBOX_DATA", "/config"):
        try:
            u = shutil.disk_usage(p)
            out.append((p, round(u.free / 1e12, 2), int(100 * u.free / u.total)))
        except Exception:
            pass
    return out


def recent_log(name, n=25):
    p = LOGS / name
    if not p.exists():
        return f"no existe {name}"
    lines = p.read_text(errors="ignore").splitlines()[-n:]
    return "\n".join(l[:160] for l in lines)


# ================================================================ v4: ver y tocar todo desde el bot
# ---------------------------------------------------------------- cola de descargas (Radarr + Sonarr)
def _iso_ts(s):
    """'2026-09-15T13:50:16.0000000Z' -> epoch (UTC). 0 si no parsea."""
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return 0


def arr_queue():
    """Lo que esta en las colas de Radarr y Sonarr, una fila por descarga (una temporada = 1 fila).
    -> [ {kind, ids:[queue ids], title, arr_id, release, status, tstate, tstatus, size_gb, pct, eta, msgs, protocol, client, eps} ]
    Problemas primero, luego lo menos avanzado."""
    out = {}
    for kind, base, key, extra in (("m", RADARR_URL, RADARR_KEY, {"includeMovie": "true", "includeUnknownMovieItems": "true"}),
                                   ("s", SONARR_URL, SONARR_KEY, {"includeSeries": "true", "includeEpisode": "true",
                                                                  "includeUnknownSeriesItems": "true"})):
        try:
            recs = _arr(base, key, "GET", "queue", params=dict(pageSize=500, **extra), timeout=60).get("records", [])
        except Exception as e:
            out[(kind, "err")] = {"kind": kind, "ids": [], "title": f"no pude leer la cola de {'Radarr' if kind == 'm' else 'Sonarr'}: {e}",
                                  "arr_id": None, "release": "", "status": "error", "tstate": "", "tstatus": "error", "size_gb": 0,
                                  "pct": 0, "eta": "", "msgs": [], "protocol": "", "client": "", "eps": []}
            continue
        for r in recs:
            k = (kind, r.get("downloadId") or str(r.get("id")))
            row = out.get(k)
            if not row:
                if kind == "m":
                    mv = r.get("movie") or {}
                    title = f"{mv.get('title')} ({mv.get('year')})" if mv.get("title") else "(peli que Radarr no reconoce)"
                    arr_id = r.get("movieId")
                else:
                    se = r.get("series") or {}
                    title = se.get("title") or "(serie que Sonarr no reconoce)"
                    arr_id = r.get("seriesId")
                size, left = float(r.get("size") or 0), float(r.get("sizeleft") or 0)
                msgs = [r["errorMessage"]] if r.get("errorMessage") else []
                for sm in r.get("statusMessages") or []:
                    msgs += [m for m in (sm.get("messages") or []) if m]
                row = out[k] = {"kind": kind, "ids": [], "title": title, "arr_id": arr_id, "release": r.get("title") or "",
                                "status": r.get("status") or "", "tstate": r.get("trackedDownloadState") or "",
                                "tstatus": r.get("trackedDownloadStatus") or "", "size_gb": round(size / 1e9, 1),
                                "pct": int(100 * (1 - left / size)) if size else 0, "eta": r.get("timeleft") or "",
                                "msgs": msgs[:3], "protocol": r.get("protocol") or "", "client": r.get("downloadClient") or "", "eps": []}
            row["ids"].append(r["id"])
            ep = r.get("episode") or {}
            if ep:
                row["eps"].append(f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}")
    rows = list(out.values())
    rows.sort(key=lambda r: (r["tstatus"] == "ok" and not r["msgs"], r["pct"]))
    return rows


def queue_remove(kind, queue_ids, blocklist=False, search=True):
    """Quita una descarga de la cola (y del cliente). blocklist=True la veta y search=True vuelve a buscar otra."""
    base, key = (RADARR_URL, RADARR_KEY) if kind == "m" else (SONARR_URL, SONARR_KEY)
    return _arr(base, key, "DELETE", "queue/bulk", timeout=60, json={"ids": [int(i) for i in queue_ids]},
                params={"removeFromClient": "true", "blocklist": "true" if blocklist else "false",
                        "skipRedownload": "false" if search else "true"})


# ---------------------------------------------------------------- Transmission
TRANSMISSION_URL = os.environ.get("TRANSMISSION_URL", "")
_TR_STATUS = {0: "parado", 1: "cola verif.", 2: "verificando", 3: "cola", 4: "bajando", 5: "cola seed", 6: "seed"}


class _Transmission:
    """RPC minimo (mismo esquema que transmission_cleanup_manual.py)."""
    def __init__(self):
        import base64
        self.url = TRANSMISSION_URL
        if not self.url:
            raise RuntimeError("TRANSMISSION_URL vacio en .env")
        self.auth = "Basic " + base64.b64encode(f"{os.environ.get('TRANSMISSION_USER', '')}:{os.environ.get('TRANSMISSION_PASS', '')}".encode()).decode()
        self.sid = ""

    def call(self, method, arguments=None):
        for _ in range(2):
            r = requests.post(self.url, json={"method": method, "arguments": arguments or {}},
                              headers={"Authorization": self.auth, "X-Transmission-Session-Id": self.sid}, timeout=60)
            if r.status_code == 409:
                self.sid = r.headers.get("X-Transmission-Session-Id", "")
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("Transmission: 409 persistente")


def transmission_list():
    """-> [ {id, name, cat, state, pct, rate_mb, eta_min, err, stalled, size_gb, age_d, idle_d} ] problemas primero."""
    fields = ["id", "name", "status", "percentDone", "rateDownload", "eta", "error", "errorString", "isStalled", "labels",
              "downloadDir", "addedDate", "activityDate", "totalSize", "peersSendingToUs"]
    ts = _Transmission().call("torrent-get", {"fields": fields})["arguments"]["torrents"]
    now = time.time()
    out = []
    for t in ts:
        cat = (t.get("labels") or [""])[0] or (t.get("downloadDir") or "").rstrip("/").split("/")[-1]
        pct = int(100 * float(t.get("percentDone") or 0))
        stalled = bool(t.get("isStalled")) or (pct < 100 and not t.get("rateDownload") and now - (t.get("activityDate") or now) > 3 * 86400)
        out.append({"id": t["id"], "name": t.get("name", "?"), "cat": cat, "state": _TR_STATUS.get(t.get("status"), "?"), "pct": pct,
                    "rate_mb": round((t.get("rateDownload") or 0) / 1e6, 1), "eta_min": (t["eta"] // 60) if (t.get("eta") or 0) > 0 else None,
                    "err": t.get("errorString") or "", "stalled": stalled, "size_gb": round((t.get("totalSize") or 0) / 1e9, 1),
                    "age_d": int((now - (t.get("addedDate") or now)) / 86400), "idle_d": int((now - (t.get("activityDate") or now)) / 86400),
                    "peers": t.get("peersSendingToUs") or 0})
    out.sort(key=lambda r: (not (r["err"] or r["stalled"]), r["pct"] == 100, r["pct"]))
    return out


def transmission_remove(ids, delete_data=True):
    return _Transmission().call("torrent-remove", {"ids": [int(i) for i in ids], "delete-local-data": bool(delete_data)})


# ---------------------------------------------------------------- Emby: recientes, cifras, usuarios
def emby_recent(hours=48, limit=40):
    """Lo agregado en las ultimas `hours` horas, agrupado (peli = 1 fila; serie = 1 fila con n episodios).
    -> [ {kind, id, name, n, no_es, ts, eps:[...]} ] mas nuevo primero. no_es = episodios/peli sin subs ni audio es."""
    since = time.time() - hours * 3600
    data = emby("GET", "Items", IncludeItemTypes="Movie,Episode", Recursive="true", SortBy="DateCreated", SortOrder="Descending",
                Limit=400, Fields="DateCreated,SeriesName,SeriesId,MediaStreams,ParentIndexNumber,IndexNumber,ProductionYear,Path",
                timeout=90).get("Items", [])
    es_codes = LANGS["espanol"][1]
    groups = {}
    for it in data:
        ts = _iso_ts(it.get("DateCreated"))
        if ts and ts < since:
            break
        a, s = streams_summary(it)
        has_es = bool((a | s) & es_codes)
        if not has_es and it.get("Path"):
            has_es = "es" in sidecars(it["Path"])
        if it.get("Type") == "Movie":
            groups[("m", it["Id"])] = {"kind": "m", "id": it["Id"], "name": f"{it.get('Name')} ({it.get('ProductionYear') or '?'})",
                                       "n": 1, "no_es": 0 if has_es else 1, "ts": ts, "eps": [], "path": it.get("Path")}
        else:
            g = groups.setdefault(("s", it.get("SeriesName")), {"kind": "s", "id": it.get("SeriesId"), "name": it.get("SeriesName") or "?",
                                                                 "n": 0, "no_es": 0, "ts": ts, "eps": [], "path": it.get("Path")})
            g["n"] += 1
            g["no_es"] += 0 if has_es else 1
            g["eps"].append(f"S{it.get('ParentIndexNumber', 0):02d}E{it.get('IndexNumber', 0):02d}")
    rows = sorted(groups.values(), key=lambda g: g["ts"], reverse=True)
    return rows[:limit]


def emby_counts():
    """-> {'Movie': n, 'Series': n, 'Episode': n}"""
    out = {}
    for t in ("Movie", "Series", "Episode"):
        try:
            out[t] = emby("GET", "Items", IncludeItemTypes=t, Recursive="true", Limit=0).get("TotalRecordCount", 0)
        except Exception:
            out[t] = -1
    return out


def emby_users_activity():
    """-> (total, activos 7 d, activos 30 d, [(nombre, dias desde ultima actividad)] ordenado por reciente)"""
    users = emby("GET", "Users")
    now = time.time()
    rows = []
    for u in users:
        ts = _iso_ts(u.get("LastActivityDate"))
        rows.append((u.get("Name"), int((now - ts) / 86400) if ts else None))
    rows.sort(key=lambda r: (r[1] is None, r[1] or 0))
    a7 = sum(1 for _, d in rows if d is not None and d <= 7)
    a30 = sum(1 for _, d in rows if d is not None and d <= 30)
    return len(rows), a7, a30, rows


# ---------------------------------------------------------------- Bazarr: que falta, historial, tareas, sync
def bazarr_wanted(limit=60):
    """Lo que Bazarr sabe que le falta. -> (pelis:[{title, radarrId, missing}], series:{serie: [(SxxEyy, sonarrEpisodeId)]}, tot_m, tot_e)"""
    mv = bazarr("GET", "movies/wanted", start=0, length=limit)
    ep = bazarr("GET", "episodes/wanted", start=0, length=limit * 3)
    movies = [{"title": r.get("title"), "radarrId": r.get("radarrId"),
               "missing": [m.get("code2") for m in (r.get("missing_subtitles") or [])]} for r in (mv.get("data") or [])]
    series = {}
    for r in ep.get("data") or []:
        series.setdefault(r.get("seriesTitle") or "?", []).append((r.get("episode_number") or "?", r.get("sonarrEpisodeId"), r.get("sonarrSeriesId")))
    return movies, series, mv.get("total") or len(movies), ep.get("total") or sum(len(v) for v in series.values())


def bazarr_history(n=10):
    """Ultimas acciones de Bazarr (pelis + episodios) -> [texto]"""
    out = []
    for path in ("movies/history", "episodes/history"):
        try:
            for r in (bazarr("GET", path, start=0, length=n).get("data") or []):
                lang = (r.get("language") or {}).get("code2", "?")
                out.append(f"{r.get('timestamp', '')}: {r.get('seriesTitle') + ' ' if r.get('seriesTitle') else ''}{r.get('title') or r.get('episodeTitle') or '?'} "
                           f"[{lang}] {r.get('provider') or ''} — {(r.get('description') or '')[:70]}")
        except Exception as e:
            out.append(f"{path}: {e}")
    return out[: n * 2]


def bazarr_tasks():
    return bazarr("GET", "system/tasks").get("data") or []


def bazarr_run_task(task_id):
    return bazarr("POST", "system/tasks", taskid=task_id)


def bazarr_search_wanted():
    """Dispara en Bazarr la busqueda de TODO lo que falta (pelis y series). -> [ids lanzados]"""
    fired = []
    for t in bazarr_tasks():
        tid = str(t.get("job_id") or t.get("id") or "")
        if "wanted" in tid.lower() or "missing" in tid.lower():
            try:
                bazarr_run_task(tid)
                fired.append(tid)
            except Exception:
                pass
    return fired


def bazarr_sync(kind, media_id, srt_path, code2="es"):
    """Re-sincroniza un .srt contra el audio (ffsubsync/alass de Bazarr)."""
    return bazarr("PATCH", "subtitles", action="sync", language=code2, path=str(srt_path),
                  type="movie" if kind == "m" else "episode", id=media_id, forced="False", hi="False", timeout=900)


# ---------------------------------------------------------------- Sonarr: temporadas
def sonarr_season_status(series_id, season):
    """-> (episodios de la temporada, con archivo, monitoreados)"""
    eps = _arr(SONARR_URL, SONARR_KEY, "GET", "episode", params={"seriesId": series_id, "seasonNumber": season})
    return len(eps), sum(1 for e in eps if e.get("hasFile")), sum(1 for e in eps if e.get("monitored"))


def sonarr_want_season(series_id, season):
    """Monitorea la temporada (y la serie) y lanza SeasonSearch."""
    s = arr_series(series_id)
    s["monitored"] = True
    found = False
    for se in s.get("seasons", []):
        if se.get("seasonNumber") == season:
            se["monitored"], found = True, True
    if not found:
        return False
    _arr(SONARR_URL, SONARR_KEY, "PUT", f"series/{series_id}", json=s)
    _arr(SONARR_URL, SONARR_KEY, "POST", "command", json={"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season})
    return True


# ---------------------------------------------------------------- borrar
def arr_delete(kind, arr_id, files=True):
    """Borra el titulo de Radarr/Sonarr y (files=True) sus archivos; Emby lo quita al ver desaparecer la carpeta."""
    if kind == "m":
        return _arr(RADARR_URL, RADARR_KEY, "DELETE", f"movie/{arr_id}", params={"deleteFiles": "true" if files else "false", "addImportExclusion": "false"}, timeout=120)
    return _arr(SONARR_URL, SONARR_KEY, "DELETE", f"series/{arr_id}", params={"deleteFiles": "true" if files else "false", "addImportListExclusion": "false"}, timeout=120)
