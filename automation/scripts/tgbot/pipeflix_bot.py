#!/usr/bin/env python3
"""PIPEFLIX Telegram bot (v2) — escribe un titulo y el bot lo resuelve en TMDB
(en espanol, tolera errores de tipeo), te dice si ya esta en Emby y, si no, lo
pide a Radarr (peliculas) o Sonarr (series) con un toque. Avisa con enlace
cuando el titulo YA aparece en Emby.

Quien puede usarlo:
    TELEGRAM_ALLOWED_IDS   admins (ids de Telegram separados por coma): sin cupo,
                           reciben aviso de cada peticion ajena y ven /usuarios.
    Cualquier otro usuario se vincula solo con su cuenta de Emby:
        /vincular usuario contrasena      (el bot borra ese mensaje al instante)
    y queda con un cupo de TELEGRAM_DAILY_QUOTA peticiones al dia (por defecto 3).

Como busca (do_search):
    1. parse_query() limpia la frase ("quiero ver la pelicula de barbie" -> "barbie",
       "stranger things temporada 5" -> "stranger things" + temporada 5, "dune 2021"
       -> "dune" + ano 2021).
    2. TMDB search/multi en es-MX (resuelve "merlina" -> Wednesday, "los simpson",
       "intensamente 2"); si no hay nada, cae a los lookups de Radarr/Sonarr.
    3. Estado por id, no por nombre: indice de Emby por Tmdb/Tvdb (volcado completo,
       cache 2 min; la busqueda por texto de Emby NO encuentra titulos en espanol),
       luego Radarr/Sonarr (pedida / descargada / % en cola).
    4. Tarjeta con poster: 1 boton para la accion obvia + hasta 4 alternativas.

Corre en mubuntu (ligero: 3-4 peticiones HTTP por mensaje). Lanzado por bot_up.sh
(cron cada 5 min + @reboot). Secretos en /config/berenstuff/.env (TELEGRAM_BOT_TOKEN,
TMDB_API_KEY, RADARR_*, SONARR_*, EMBY_*). La carpeta destino sale de
streaming.media_shelf.classify(), la misma regla que trending_add.py y el librarian.
"""
import asyncio
import datetime as dt
import html
import json
import logging
import os
import pathlib
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from streaming.arr_client import add_movie, ensure_tag
from streaming.media_shelf import classify as classify_shelf, shelf_path

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("pipeflix_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMINS = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").replace(";", ",").split(",")
          if x.strip().isdigit()}
DAILY_QUOTA = int(os.environ.get("TELEGRAM_DAILY_QUOTA", "3"))
RADARR_URL = os.environ["RADARR_URL"]
RADARR_KEY = os.environ["RADARR_KEY"]
SONARR_URL = os.environ["SONARR_URL"]
SONARR_KEY = os.environ["SONARR_KEY"]
EMBY_API = os.environ.get("EMBY_INTERNAL_BASE") or (os.environ["EMBY_URL"] + "/emby")
EMBY_KEY = os.environ["EMBY_API_KEY"]
TMDB_KEY = os.environ["TMDB_API_KEY"]
OPEN_PAGE = os.environ.get("EMBY_OPEN_PAGE", "https://beregcamlost.github.io/emby-open/")

RADARR_QUALITY_PROFILE = 1  # HD-1080p (mismo que trending_add.py)
SONARR_QUALITY_PROFILE = 4  # HD-1080p
TAG_LABEL = "telegram-add"
MAX_RESULTS = 5               # tarjeta + 4 alternativas
BIG_SERIES_EPISODES = 100     # de aqui en adelante se pregunta "toda o solo la ultima temporada"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
CACHE_DIR = pathlib.Path("/config/berenstuff/automation/cache")
PENDING_FILE = CACHE_DIR / "telegram_pending.json"
USERS_FILE = CACHE_DIR / "telegram_users.json"
REQUESTS_LOG = CACHE_DIR / "telegram_requests.jsonl"
PIDFILE = pathlib.Path("/tmp/pipeflix_bot.pid")
PENDING_POLL_S = 120
PENDING_GIVEUP_S = 14 * 86400
EMBY_LAG_WARN_S = 3 * 3600    # descargada pero Emby no la muestra: avisar una vez
LIBRARY_TTL_S = 120
QUEUE_TTL_S = 30
LINK_FAILS_PER_HOUR = 5

_pool = ThreadPoolExecutor(max_workers=6)
_emby_server_id = ""
_emby_idx = {"at": 0.0, "map": {}}
_library_cache = {"at": 0.0, "movies": {}, "series": {}}
_queue_cache = {"at": 0.0, "map": {}}
_link_fails = {}  # tg_id -> [timestamps]


# ---------------------------------------------------------------- helpers
def _h(s):
    return html.escape(str(s or ""))


def _year(x):
    return f" ({x})" if x else ""


def _fold(s):
    """minusculas, sin acentos ni signos: para comparar titulos."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c)).lower()).strip()


def _ago(ts):
    d = max(0, int(time.time() - ts))
    if d < 3600:
        return f"{d // 60} min"
    if d < 86400:
        return f"{d // 3600} h"
    return f"{d // 86400} d"


def _arr_get(base, key, path, **params):
    r = requests.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _arr_post(base, key, path, body):
    r = requests.post(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _arr_put(base, key, path, body):
    r = requests.put(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _emby_get(path, **params):
    params["api_key"] = EMBY_KEY
    r = requests.get(f"{EMBY_API}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _tmdb_get(path, **params):
    params["api_key"] = TMDB_KEY
    r = requests.get(f"https://api.themoviedb.org/3/{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def emby_link(item_id):
    # Pagina puente (repo emby-open): salta a la app de Emby en iOS/Android; los ids van
    # en el fragmento #, que nunca sale del telefono. Telegram no acepta emby:// directo.
    return f"{OPEN_PAGE}#{_emby_server_id}/{item_id}"


def app_name(kind):
    return "Radarr" if kind == "m" else "Sonarr"


# ---------------------------------------------------------------- 1. limpiar la frase
_PREFIX = re.compile(
    r"^(?:(?:hola|oye|epa|porfa|por favor|pls)[,\s]+)*"
    r"(?:(?:quiero|quisiera|me gustaria|me gustar[ií]a|queremos|necesito)\s+ver|quiero|quisiera|"
    r"busca(?:me|r)?|pon(?:me|er)?|agrega(?:me|r)?|a[nñ]ade(?:me)?|descarga(?:me|r)?|pide|pedir|"
    r"tienes|tienen|tendr[aá]s|hay|est[aá]|existe|ver|mira|dame|consigue(?:me)?)\s+", re.I)
_MEDIA = re.compile(
    r"^(?:(?:la|el|una|un|las|los)\s+)?(?:peli(?:cula|culas)?|pel[ií]cula|pelis|serie|series|anime|dorama|"
    r"documental|caricatura)\s+(?:de\s+|del\s+|llamad[ao]\s+|que se llama\s+)?", re.I)
_TRAIL = re.compile(r"[\s,]*(?:por favor|porfa|porfis|pls|please|gracias|completa|entera|en emby|en espa[nñ]ol|"
                    r"latino|subtitulada)\s*$", re.I)
_SEASON = re.compile(r"\b(?:temporada|temp|season|t)\s*\.?\s*(\d{1,2})\b|\bs(\d{2})(?:e\d{1,3})?\b", re.I)
_YEAR = re.compile(r"(?:^|[\s(])((?:19|20)\d{2})\)?\s*$")


def parse_query(text):
    """'quiero ver la pelicula de barbie' -> ('barbie', None, None);
    'stranger things temporada 5' -> ('stranger things', None, 5); 'dune 2021' -> ('dune', 2021, None)."""
    q = re.sub(r"[¿?¡!\"“”«»]+", " ", text).strip()
    q = re.sub(r"\s+", " ", q)
    for _ in range(3):
        before = q
        q = _PREFIX.sub("", q)
        q = _MEDIA.sub("", q)
        q = _TRAIL.sub("", q)
        if q == before:
            break
    season = None
    m = _SEASON.search(q)
    if m and len(q) - len(m.group(0)) >= 2:
        season = int(m.group(1) or m.group(2))
        q = (q[:m.start()] + q[m.end():]).strip(" -:,")
    year = None
    m = _YEAR.search(q)
    if m and len(q) - len(m.group(0)) >= 2 and int(m.group(1)) <= dt.date.today().year + 1:  # "blade runner 2049" es titulo
        year = int(m.group(1))
        q = q[:m.start()].strip(" -:,(")
    q = re.sub(r"\s+", " ", q).strip(" -:,.")
    return q or text.strip(), year, season


# ---------------------------------------------------------------- 2. TMDB
def _norm_result(x):
    kind = "m" if x.get("media_type") == "movie" else "s"
    date = x.get("release_date") if kind == "m" else x.get("first_air_date")
    return {"kind": kind, "tmdb": x["id"],
            "title": x.get("title") or x.get("name") or "?",
            "original": x.get("original_title") or x.get("original_name") or "",
            "year": int(date[:4]) if date and date[:4].isdigit() else None,
            "pop": float(x.get("popularity") or 0), "vote": x.get("vote_average") or 0,
            "poster": x.get("poster_path"), "overview": x.get("overview") or ""}


def _arr_fallback(term):
    """Cuando TMDB no devuelve nada: los lookups de Radarr/Sonarr (traen tmdbId)."""
    out = []
    try:
        for m in _arr_get(RADARR_URL, RADARR_KEY, "movie/lookup", term=term)[:MAX_RESULTS]:
            if m.get("tmdbId"):
                out.append({"kind": "m", "tmdb": m["tmdbId"], "title": m["title"], "original": m.get("originalTitle", ""),
                            "year": m.get("year"), "pop": 0, "vote": 0, "poster": None, "overview": m.get("overview", "")})
    except Exception:
        log.exception("radarr lookup %r", term)
    try:
        for s in _arr_get(SONARR_URL, SONARR_KEY, "series/lookup", term=term)[:MAX_RESULTS]:
            if s.get("tmdbId"):
                out.append({"kind": "s", "tmdb": s["tmdbId"], "title": s["title"], "original": "",
                            "year": s.get("year"), "pop": 0, "vote": 0, "poster": None, "overview": s.get("overview", "")})
    except Exception:
        log.exception("sonarr lookup %r", term)
    return out


def tmdb_search(clean, year=None, raw=None):
    """Hasta MAX_RESULTS candidatos ordenados: ano pedido > titulo exacto > popularidad."""
    base = dict(language="es-MX", include_adult="false")
    res = [_norm_result(x) for x in _tmdb_get("search/multi", query=clean, **base).get("results", [])
           if x.get("media_type") in ("movie", "tv")]
    if year and not any(r["year"] == year for r in res):
        mv = _tmdb_get("search/movie", query=clean, year=year, **base).get("results", [])
        tv = _tmdb_get("search/tv", query=clean, first_air_date_year=year, **base).get("results", [])
        res = ([_norm_result(dict(x, media_type="movie")) for x in mv]
               + [_norm_result(dict(x, media_type="tv")) for x in tv] + res)
    if not res and raw and _fold(raw) != _fold(clean):
        res = [_norm_result(x) for x in _tmdb_get("search/multi", query=raw, **base).get("results", [])
               if x.get("media_type") in ("movie", "tv")]
    if not res:
        res = _arr_fallback(raw or clean)
    q = _fold(clean)
    seen, out = set(), []
    def score(r):  # titulo exacto solo EMPUJA (x1.5); la popularidad manda ("el senor de los anillos" = la trilogia)
        exact = _fold(r["title"]) == q or _fold(r["original"]) == q
        return (bool(year) and r["year"] == year, r["pop"] * (1.5 if exact else 1.0))
    for r in sorted(res, key=score, reverse=True):
        if (r["kind"], r["tmdb"]) in seen:
            continue
        seen.add((r["kind"], r["tmdb"]))
        out.append(r)
    return out[:MAX_RESULTS]


def tmdb_details(kind, tmdb):
    """Ficha completa en espanol + ids externos (+ fechas de estreno para pelis)."""
    if kind == "m":
        d = _tmdb_get(f"movie/{tmdb}", language="es-MX", append_to_response="release_dates,external_ids")
        digital, theatrical = None, None
        for c in (d.get("release_dates") or {}).get("results", []):
            for r in c.get("release_dates", []):
                day = (r.get("release_date") or "")[:10]
                if not day:
                    continue
                if r.get("type") in (4, 5, 6):
                    digital = min(digital or day, day)
                elif r.get("type") in (2, 3):
                    theatrical = min(theatrical or day, day)
        date = d.get("release_date") or ""
        return {"kind": "m", "tmdb": tmdb, "title": d.get("title") or d.get("original_title"),
                "original": d.get("original_title", ""), "year": int(date[:4]) if date[:4].isdigit() else None,
                "genres": [g["name"] for g in d.get("genres", [])][:3], "vote": d.get("vote_average") or 0,
                "overview": d.get("overview") or "", "poster": d.get("poster_path"),
                "runtime": d.get("runtime"), "digital": digital, "theatrical": theatrical,
                "tvdb": None, "imdb": (d.get("external_ids") or {}).get("imdb_id")}
    d = _tmdb_get(f"tv/{tmdb}", language="es-MX", append_to_response="external_ids")
    date = d.get("first_air_date") or ""
    return {"kind": "s", "tmdb": tmdb, "title": d.get("name") or d.get("original_name"),
            "original": d.get("original_name", ""), "year": int(date[:4]) if date[:4].isdigit() else None,
            "genres": [g["name"] for g in d.get("genres", [])][:3], "vote": d.get("vote_average") or 0,
            "overview": d.get("overview") or "", "poster": d.get("poster_path"),
            "episodes": d.get("number_of_episodes") or 0, "seasons": d.get("number_of_seasons") or 0,
            "status": d.get("status"), "in_production": d.get("in_production"),
            "tvdb": (d.get("external_ids") or {}).get("tvdb_id"), "imdb": (d.get("external_ids") or {}).get("imdb_id")}


# ---------------------------------------------------------------- 3. estado: Emby > Radarr/Sonarr
def emby_index(force=False):
    """{('m'|'s', 'tmdb'|'tvdb', id): item} de TODO Emby (721 items, 0.1 s; cache 2 min).
    La busqueda por texto de Emby no encuentra titulos en espanol; por id si."""
    now = time.time()
    if force or now - _emby_idx["at"] > LIBRARY_TTL_S:
        data = _emby_get("Items", IncludeItemTypes="Movie,Series", Recursive="true",
                         Fields="ProviderIds,ProductionYear", Limit=20000)
        idx = {}
        for it in data.get("Items", []):
            kind = "m" if it["Type"] == "Movie" else "s"
            prov = it.get("ProviderIds") or {}
            for k in ("Tmdb", "Tvdb"):
                v = str(prov.get(k, ""))
                if v.isdigit():
                    idx.setdefault((kind, k.lower(), int(v)), it)
            idx.setdefault((kind, "name", (_fold(it["Name"]), it.get("ProductionYear"))), it)
        _emby_idx.update(at=now, map=idx)
    return _emby_idx["map"]


def library_ids(force=False):
    """{tmdb: info} de Radarr y {tmdb: info} de Sonarr (con tvdb como llave extra). Cache 2 min."""
    now = time.time()
    if force or now - _library_cache["at"] > LIBRARY_TTL_S:
        movies = {}
        for m in _arr_get(RADARR_URL, RADARR_KEY, "movie"):
            movies[m.get("tmdbId")] = {"arr_id": m["id"], "has_file": bool(m.get("hasFile")), "title": m.get("title"),
                                       "year": m.get("year"), "added": m.get("added", ""), "monitored": m.get("monitored")}
        series = {}
        for s in _arr_get(SONARR_URL, SONARR_KEY, "series"):
            st = s.get("statistics") or {}
            info = {"arr_id": s["id"], "has_file": st.get("episodeFileCount", 0) > 0, "title": s.get("title"),
                    "year": s.get("year"), "added": s.get("added", ""), "monitored": s.get("monitored"),
                    "files": st.get("episodeFileCount", 0), "episodes": st.get("episodeCount", 0),
                    "seasons": s.get("seasons", [])}
            if s.get("tmdbId"):
                series[s["tmdbId"]] = info
            if s.get("tvdbId"):
                series[("tvdb", s["tvdbId"])] = info
        _library_cache.update(at=now, movies=movies, series=series)
    return _library_cache["movies"], _library_cache["series"]


def queue_progress():
    """{('m', movieId) | ('s', seriesId): pct} de lo que esta bajando ahora. Cache 30 s."""
    now = time.time()
    if now - _queue_cache["at"] > QUEUE_TTL_S:
        agg = {}
        for kind, base, key, fld in (("m", RADARR_URL, RADARR_KEY, "movieId"), ("s", SONARR_URL, SONARR_KEY, "seriesId")):
            try:
                for rec in _arr_get(base, key, "queue", pageSize=500).get("records", []):
                    k = (kind, rec.get(fld))
                    size, left = float(rec.get("size") or 0), float(rec.get("sizeleft") or 0)
                    tot = agg.setdefault(k, [0.0, 0.0])
                    tot[0] += size
                    tot[1] += left
            except Exception:
                log.exception("queue %s", kind)
        _queue_cache.update(at=now, map={k: (int(100 * (1 - v[1] / v[0])) if v[0] else 0) for k, v in agg.items()})
    return _queue_cache["map"]


def resolve(kind, tmdb, tvdb=None, title=None, year=None):
    """-> {'status': 'emby'|'downloaded'|'requested'|'missing', 'emby': item, 'lib': info, 'pct': int|None}"""
    idx = emby_index()
    item = idx.get((kind, "tmdb", tmdb))
    if not item and tvdb:
        item = idx.get((kind, "tvdb", tvdb))
    if not item and title:
        item = idx.get((kind, "name", (_fold(title), year)))
    if item:
        return {"status": "emby", "emby": item, "lib": None, "pct": None}
    lib_m, lib_s = library_ids()
    lib = lib_m.get(tmdb) if kind == "m" else (lib_s.get(tmdb) or (tvdb and lib_s.get(("tvdb", tvdb))))
    if lib and lib["has_file"]:
        return {"status": "downloaded", "emby": None, "lib": lib, "pct": None}
    if lib:
        return {"status": "requested", "emby": None, "lib": lib, "pct": queue_progress().get((kind, lib["arr_id"]))}
    return {"status": "missing", "emby": None, "lib": None, "pct": None}


def do_search(text):
    """-> (clean, year, season, results, details_top, status_top, {(kind,tmdb): status_str de alternativas})"""
    clean, year, season = parse_query(text)
    f_idx, f_lib = _pool.submit(emby_index), _pool.submit(library_ids)   # calientan cache en paralelo
    results = tmdb_search(clean, year, raw=text)
    f_idx.result()
    f_lib.result()
    if not results:
        return clean, year, season, [], None, None, {}
    top = results[0]
    details = tmdb_details(top["kind"], top["tmdb"])
    status = resolve(top["kind"], top["tmdb"], details.get("tvdb"), details.get("title"), details.get("year"))
    alts = {(r["kind"], r["tmdb"]): resolve(r["kind"], r["tmdb"])["status"] for r in results[1:]}
    return clean, year, season, results, details, status, alts


# ---------------------------------------------------------------- 4. tarjeta
def build_card(details, status, alternatives, alt_status, season_hint=None):
    """-> (caption HTML, InlineKeyboardMarkup|None, poster_url|None)"""
    d, kind = details, details["kind"]
    ico = "🎬" if kind == "m" else "📺"
    head = f"{ico} <b>{_h(d['title'])}</b>{_year(d['year'])}"
    if d.get("original") and _fold(d["original"]) != _fold(d["title"]):
        head += f" · <i>{_h(d['original'])}</i>"
    meta = []
    if d.get("vote"):
        meta.append(f"⭐ {d['vote']:.1f}")
    if d.get("genres"):
        meta.append(", ".join(d["genres"]))
    if kind == "m" and d.get("runtime"):
        meta.append(f"{d['runtime']} min")
    if kind == "s" and d.get("episodes"):
        meta.append(f"{d['seasons']} temp · {d['episodes']} ep" + (" · en emision" if d.get("in_production") else ""))
    lines = [head, " · ".join(meta)] if meta else [head]

    rows, st = [], status["status"]
    if st == "emby":
        lines.append("\n✅ <b>Ya esta en Emby</b>")
        rows.append([InlineKeyboardButton("🍿 Abrir en Emby", url=emby_link(status["emby"]["Id"]))])
    elif st == "downloaded":
        lib = status["lib"]
        extra = f" ({lib['files']} ep)" if kind == "s" and lib.get("files") else ""
        lines.append(f"\n💾 <b>Ya se descargo{extra}</b>; Emby la muestra en unos minutos.")
    elif st == "requested":
        lib = status["lib"]
        since = ""
        try:
            since = " hace " + _ago(dt.datetime.fromisoformat(lib["added"].replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
        prog = f"⬇️ descargando {status['pct']}%" if status.get("pct") is not None else "🔎 buscando copia"
        lines.append(f"\n⏳ <b>Ya esta pedida</b>{since} · {prog}")
        rows.append([InlineKeyboardButton("🔁 Reintentar busqueda", callback_data=f"rs:{kind}:{lib['arr_id']}")])
    else:
        today = dt.date.today().isoformat()
        if kind == "m" and d.get("digital") and d["digital"] > today:
            lines.append(f"\n🎟 Aun no salio en digital (sale ~{d['digital']}); si la pides, llega sola ese dia.")
        elif kind == "m" and not d.get("digital") and d.get("theatrical") and d["theatrical"] > today:
            lines.append(f"\n🎟 Se estrena en cines el {d['theatrical']}; puedes dejarla pedida.")
        elif kind == "m" and not d.get("digital") and d.get("theatrical") and d["theatrical"] > (dt.date.today() - dt.timedelta(days=100)).isoformat():
            lines.append("\n🎟 Todavia en cines; se descargara cuando salga en digital.")
        if kind == "m":
            rows.append([InlineKeyboardButton("➕ Pedir pelicula", callback_data=f"add:m:{d['tmdb']}:all")])
        else:
            if season_hint:
                rows.append([InlineKeyboardButton(f"➕ Solo temporada {season_hint}", callback_data=f"add:s:{d['tmdb']}:{season_hint}")])
            if d.get("episodes", 0) > BIG_SERIES_EPISODES:
                rows.append([InlineKeyboardButton(f"➕ Toda la serie ({d['episodes']} ep)", callback_data=f"add:s:{d['tmdb']}:all")])
                rows.append([InlineKeyboardButton("➕ Solo la ultima temporada", callback_data=f"add:s:{d['tmdb']}:last")])
            else:
                rows.append([InlineKeyboardButton("➕ Pedir serie completa", callback_data=f"add:s:{d['tmdb']}:all")])

    if d.get("overview"):
        room = 1000 - sum(len(x) for x in lines) - 40
        ov = d["overview"].strip()
        if len(ov) > min(room, 300):
            ov = ov[:max(0, min(room, 300))].rsplit(" ", 1)[0] + "…"
        lines.insert(2 if meta else 1, f"\n{_h(ov)}")

    if alternatives:
        lines.append("\n¿No era esa? Toca otra 👇")
        for a in alternatives:
            aico = "🎬" if a["kind"] == "m" else "📺"
            mark = "✅ " if alt_status.get((a["kind"], a["tmdb"])) == "emby" else ""
            rows.append([InlineKeyboardButton(f"{mark}{aico} {a['title']}{_year(a['year'])}"[:60],
                                              callback_data=f"pick:{a['kind']}:{a['tmdb']}")])
    poster = f"{TMDB_IMG}{d['poster']}" if d.get("poster") else None
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None, poster


async def send_card(msg, caption, markup, poster):
    if poster:
        try:
            return await msg.reply_photo(photo=poster, caption=caption, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramError:
            log.warning("poster fallo, mando texto: %s", poster)
    return await msg.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)


# ---------------------------------------------------------------- acciones
def _monitor_seasons(series, mode):
    seasons = [s for s in series.get("seasons", []) if s.get("seasonNumber", 0) > 0]
    if mode == "last" and seasons:
        last = max(s["seasonNumber"] for s in seasons)
        want = {last}
    elif str(mode).isdigit() and any(s["seasonNumber"] == int(mode) for s in seasons):
        want = {int(mode)}
    else:
        mode, want = "all", {s["seasonNumber"] for s in seasons}
    for s in series.get("seasons", []):
        s["monitored"] = s.get("seasonNumber", 0) in want
    return mode, want


def add_series_by_tmdb(tmdb, root, mode="all"):
    hits = _arr_get(SONARR_URL, SONARR_KEY, "series/lookup", term=f"tmdb:{tmdb}")
    if not hits:
        return None, mode
    s = hits[0]
    mode, want = _monitor_seasons(s, mode)
    s["qualityProfileId"] = SONARR_QUALITY_PROFILE
    s["rootFolderPath"] = root
    s["monitored"] = True
    s["seasonFolder"] = True
    s["tags"] = [ensure_tag(SONARR_URL, SONARR_KEY, TAG_LABEL)]
    # monitor: 'all' | 'lastSeason' | 'none' (+ flags por temporada, que Sonarr respeta en el PUT)
    s["addOptions"] = {"monitor": {"all": "all", "last": "lastSeason"}.get(mode, "none"),
                       "searchForMissingEpisodes": True}
    obj = _arr_post(SONARR_URL, SONARR_KEY, "series", s)
    if mode not in ("all", "last") or not obj.get("monitored"):
        # temporada concreta: Sonarr no tiene esa opcion al crear (y monitor=none desmonitorea la serie)
        # -> PUT con la serie monitoreada + banderas por temporada, y busqueda de esa temporada
        cur = _arr_get(SONARR_URL, SONARR_KEY, f"series/{obj['id']}")
        cur["monitored"] = True
        for season in cur.get("seasons", []):
            season["monitored"] = season.get("seasonNumber") in want
        obj = _arr_put(SONARR_URL, SONARR_KEY, f"series/{obj['id']}", cur)
        if str(mode).isdigit():
            _arr_post(SONARR_URL, SONARR_KEY, "command", {"name": "SeasonSearch", "seriesId": obj["id"], "seasonNumber": int(mode)})
    return obj, mode


def do_add(kind, tmdb, mode="all"):
    """Anade a Radarr/Sonarr en la carpeta que dicta media_shelf. -> (obj, shelf, why, mode)"""
    if kind == "m":
        shelf, why = classify_shelf("movie", tmdb_id=tmdb)
        root = shelf_path(shelf)
        pathlib.Path(root).mkdir(parents=True, exist_ok=True)
        tag = ensure_tag(RADARR_URL, RADARR_KEY, TAG_LABEL)
        obj = add_movie(RADARR_URL, RADARR_KEY, tmdb, RADARR_QUALITY_PROFILE, root, tags=[tag])
    else:
        shelf, why = classify_shelf("tv", tmdb_id=tmdb)
        root = shelf_path(shelf)
        pathlib.Path(root).mkdir(parents=True, exist_ok=True)
        obj, mode = add_series_by_tmdb(tmdb, root, mode)
    _library_cache["at"] = 0  # que la proxima busqueda ya lo vea
    return obj, shelf, why, mode


def do_research(kind, arr_id):
    if kind == "m":
        return _arr_post(RADARR_URL, RADARR_KEY, "command", {"name": "MoviesSearch", "movieIds": [arr_id]})
    return _arr_post(SONARR_URL, SONARR_KEY, "command", {"name": "SeriesSearch", "seriesId": arr_id})


def _arrived(kind, arr_id):
    if kind == "m":
        m = _arr_get(RADARR_URL, RADARR_KEY, f"movie/{arr_id}")
        return bool(m.get("hasFile")), m
    s = _arr_get(SONARR_URL, SONARR_KEY, f"series/{arr_id}")
    return (s.get("statistics") or {}).get("episodeFileCount", 0) > 0, s


# ---------------------------------------------------------------- usuarios, cupo, bitacora
def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def load_users():
    return _load_json(USERS_FILE, {})


def is_admin(tg_id):
    return tg_id in ADMINS


def who(update):
    """-> ('admin'|'user'|None, etiqueta legible)"""
    u = update.effective_user
    if not u:
        return None, "?"
    if is_admin(u.id):
        return "admin", u.username or u.full_name or str(u.id)
    rec = load_users().get(str(u.id))
    if rec:
        return "user", rec.get("emby_user") or str(u.id)
    return None, u.username or u.full_name or str(u.id)


def log_request(event, update, **extra):
    u = update.effective_user
    rec = {"ts": time.time(), "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "event": event, "tg_id": u.id if u else None, "tg_name": (u.username or u.full_name) if u else None,
           "who": who(update)[1], **extra}
    REQUESTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with REQUESTS_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def quota_used(tg_id):
    since = time.time() - 86400
    n = 0
    try:
        with REQUESTS_LOG.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") == "add" and r.get("tg_id") == tg_id and r.get("ts", 0) > since:
                    n += 1
    except FileNotFoundError:
        pass
    return n


def emby_authenticate(username, password):
    r = requests.post(f"{EMBY_API}/Users/AuthenticateByName",
                      json={"Username": username, "Pw": password or ""},
                      headers={"X-Emby-Authorization": 'MediaBrowser Client="pipeflix", Device="telegram", '
                                                       'DeviceId="pipeflix-bot", Version="2"'},
                      timeout=30)
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()
    return r.json().get("User") or {}


async def notify_admins(app, text):
    for aid in ADMINS:
        try:
            await app.bot.send_message(aid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            log.warning("no pude avisar al admin %s", aid)


# ---------------------------------------------------------------- pendientes: avisar cuando YA se ve en Emby
def remember_pending(chat_id, kind, arr_id, tmdb, title, who_label):
    p = _load_json(PENDING_FILE, {})
    p[f"{kind}:{arr_id}"] = {"chat_id": chat_id, "kind": kind, "arr_id": arr_id, "tmdb": tmdb, "title": title,
                             "who": who_label, "since": time.time(), "stage": "arr"}
    _save_json(PENDING_FILE, p)


async def pending_loop(app):
    await asyncio.sleep(45)
    while True:
        try:
            p = _load_json(PENDING_FILE, {})
            changed = False
            if p:
                idx = await asyncio.to_thread(emby_index, True)
            for key, req in list(p.items()):
                kind, title, chat = req["kind"], req["title"], req["chat_id"]
                drop = False
                try:
                    if req.get("stage", "arr") == "arr":
                        ok, obj = await asyncio.to_thread(_arrived, kind, req["arr_id"])
                        if ok:
                            req["stage"], req["downloaded_at"] = "emby", time.time()
                            changed = True
                    if req.get("stage") == "emby":
                        item = idx.get((kind, "tmdb", req.get("tmdb")))
                        if item:
                            what = "pelicula" if kind == "m" else "serie (ya hay episodios)"
                            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🍿 Abrir en Emby", url=emby_link(item["Id"]))]])
                            await app.bot.send_message(chat, f"🍿 <b>{_h(title)}</b> ya esta en Emby ({what}).",
                                                       parse_mode=ParseMode.HTML, reply_markup=kb)
                            drop = True
                        elif not req.get("lag_warned") and time.time() - req.get("downloaded_at", time.time()) > EMBY_LAG_WARN_S:
                            await app.bot.send_message(chat, f"💾 <b>{_h(title)}</b> ya se descargo pero Emby aun no la muestra; "
                                                             f"sigo pendiente y te aviso cuando aparezca.", parse_mode=ParseMode.HTML)
                            req["lag_warned"] = True
                            changed = True
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        drop = True  # lo borraron del *arr
                    else:
                        raise
                if not drop and time.time() - req["since"] > PENDING_GIVEUP_S:
                    await app.bot.send_message(chat, f"⌛ <b>{_h(title)}</b> lleva 14 dias sin llegar; dejo de vigilarla "
                                                     f"(sigue pedida en {app_name(kind)}).", parse_mode=ParseMode.HTML)
                    drop = True
                if drop:
                    p.pop(key, None)
                    changed = True
            if changed:
                _save_json(PENDING_FILE, p)
        except Exception:
            log.exception("pending_loop")
        await asyncio.sleep(PENDING_POLL_S)


# ---------------------------------------------------------------- handlers
ONBOARD = ("👋 Soy <b>PIPEFLIX</b>, el bot del Emby.\n\n"
           "Para entrar, vincula tu cuenta de Emby (la misma con la que ves las pelis):\n"
           "<code>/vincular tu_usuario tu_contrasena</code>\n\n"
           "Borro ese mensaje en cuanto lo leo. Despues solo escribes el nombre de lo que quieras ver 🍿")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    extra = ("\n/usuarios — quien esta vinculado\n/desvincular &lt;id o usuario&gt;" if role == "admin"
             else f"\nTienes {max(0, DAILY_QUOTA - quota_used(update.effective_user.id))} peticiones disponibles hoy.")
    await update.message.reply_text(
        f"🎬 <b>PIPEFLIX</b> — hola, {_h(label)}\n\n"
        "Escribeme el nombre de una pelicula o serie (en espanol o en ingles, con o sin ano) y te digo si ya esta "
        "en Emby; si no, la pides con un toque y te aviso cuando ya se pueda ver.\n\n"
        "Ejemplos: <i>merlina</i> · <i>dune 2021</i> · <i>stranger things temporada 5</i>\n\n"
        "/pendientes — lo que pediste y aun no llega\n"
        "/id — tu id de Telegram" + extra,
        parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu id: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg, u = update.message, update.effective_user
    parts = (msg.text or "").split(maxsplit=2)
    try:
        await msg.delete()  # que la contrasena no quede en el chat
    except TelegramError:
        pass
    if len(parts) < 2:
        return await msg.chat.send_message("Uso: <code>/vincular usuario contrasena</code>", parse_mode=ParseMode.HTML)
    username, password = parts[1], (parts[2] if len(parts) > 2 else "")
    fails = [t for t in _link_fails.get(u.id, []) if time.time() - t < 3600]
    if len(fails) >= LINK_FAILS_PER_HOUR:
        return await msg.chat.send_message("Demasiados intentos; espera una hora 🙏")
    try:
        user = await asyncio.to_thread(emby_authenticate, username, password)
    except Exception as e:
        log.exception("emby auth")
        return await msg.chat.send_message(f"💥 Emby no respondio: {_h(e)}", parse_mode=ParseMode.HTML)
    if not user:
        fails.append(time.time())
        _link_fails[u.id] = fails
        log_request("link_fail", update, emby_user=username)
        return await msg.chat.send_message("🚫 Usuario o contrasena incorrectos (son los de Emby). Intenta de nuevo.")
    users = load_users()
    users[str(u.id)] = {"emby_user": user.get("Name", username), "emby_id": user.get("Id"), "tg_name": u.username or u.full_name,
                        "linked": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    _save_json(USERS_FILE, users)
    log_request("link", update, emby_user=user.get("Name"))
    await msg.chat.send_message(
        f"✅ Listo, <b>{_h(user.get('Name', username))}</b>. Escribeme el nombre de lo que quieras ver.\n"
        f"Tienes {DAILY_QUOTA} peticiones al dia.", parse_mode=ParseMode.HTML)
    await notify_admins(ctx.application, f"🔗 <b>{_h(user.get('Name'))}</b> (Emby) vinculo Telegram "
                                         f"@{_h(u.username or u.full_name)} (<code>{u.id}</code>).")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if who(update)[0] != "admin":
        return
    users = load_users()
    if not users:
        return await update.message.reply_text("Nadie vinculado todavia.")
    lines = [f"• <b>{_h(r['emby_user'])}</b> ← @{_h(r.get('tg_name'))} <code>{tid}</code> · {r.get('linked', '')[:10]} · "
             f"hoy {quota_used(int(tid))}/{DAILY_QUOTA}" for tid, r in users.items()]
    await update.message.reply_text("👥 <b>Vinculados</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if who(update)[0] != "admin":
        return
    target = " ".join(ctx.args or []).strip().lower()
    users = load_users()
    gone = [tid for tid, r in users.items() if tid == target or (r.get("emby_user") or "").lower() == target]
    for tid in gone:
        users.pop(tid, None)
    _save_json(USERS_FILE, users)
    await update.message.reply_text(f"Desvinculados: {len(gone)}")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not role:
        return await update.message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    p = _load_json(PENDING_FILE, {})
    mine = [r for r in p.values() if role == "admin" or r["chat_id"] == update.effective_chat.id]
    if not mine:
        return await update.message.reply_text("Nada pendiente 👌")
    prog = await asyncio.to_thread(queue_progress)
    lines = []
    for r in sorted(mine, key=lambda r: r["since"]):
        ico = "🎬" if r["kind"] == "m" else "📺"
        if r.get("stage") == "emby":
            state = "💾 descargada, esperando a Emby"
        else:
            pct = prog.get((r["kind"], r["arr_id"]))
            state = f"⬇️ {pct}%" if pct is not None else "🔎 buscando copia"
        owner = f" · {_h(r.get('who'))}" if role == "admin" else ""
        lines.append(f"• {ico} {_h(r['title'])} — {state} · hace {_ago(r['since'])}{owner}")
    await update.message.reply_text("⏳ <b>Pendientes</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    text = update.message.text.strip()
    if len(text) < 2:
        return await update.message.reply_text("Escribe al menos 2 letras 🙂")
    wait = await update.message.reply_text("🔎 Buscando…")
    try:
        clean, year, season, results, details, status, alts = await asyncio.to_thread(do_search, text)
    except Exception as e:
        log.exception("search %r", text)
        return await wait.edit_text(f"💥 Fallo la busqueda: {_h(e)}", parse_mode=ParseMode.HTML)
    log.info("search por %s: %r -> %r (%s) %s", label, text, clean, year or "",
             f"{results[0]['kind']}:{results[0]['tmdb']} {results[0]['title']} [{status['status']}]" if results else "sin resultados")
    log_request("search", update, text=text, clean=clean, top=(f"{results[0]['kind']}:{results[0]['tmdb']}" if results else None),
                status=(status or {}).get("status"))
    if not results:
        return await wait.edit_text(
            f"🤷 No encontre nada para «{_h(clean)}».\nPrueba con el titulo original (en ingles), sin tildes, o agrega el ano.",
            parse_mode=ParseMode.HTML)
    caption, markup, poster = build_card(details, status, results[1:], alts, season)
    try:
        await wait.delete()
    except TelegramError:
        pass
    await send_card(update.message, caption, markup, poster)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    role, label = who(update)
    if not role:
        return await q.answer("Primero vincula tu cuenta de Emby con /vincular", show_alert=True)
    parts = q.data.split(":")
    action, kind = parts[0], parts[1]
    chat_id = update.effective_chat.id
    try:
        if action == "pick":
            await q.answer()
            tmdb = int(parts[2])
            details = await asyncio.to_thread(tmdb_details, kind, tmdb)
            status = await asyncio.to_thread(resolve, kind, tmdb, details.get("tvdb"), details.get("title"), details.get("year"))
            caption, markup, poster = build_card(details, status, [], {})
            return await send_card(q.message, caption, markup, poster)

        if action == "add":
            tmdb, mode = int(parts[2]), (parts[3] if len(parts) > 3 else "all")
            if role != "admin" and quota_used(update.effective_user.id) >= DAILY_QUOTA:
                return await q.answer(f"Ya usaste tus {DAILY_QUOTA} peticiones de hoy; manana se renuevan 🙂", show_alert=True)
            # ¿ya la pidio alguien mientras tanto?
            cur = await asyncio.to_thread(resolve, kind, tmdb)
            if cur["status"] != "missing":
                await q.answer("Esa ya esta pedida o ya esta en Emby 👌", show_alert=True)
                return
            await q.answer("Dale, un momento…")
            obj, shelf, why, mode = await asyncio.to_thread(do_add, kind, tmdb, mode)
            if not obj:
                return await q.message.reply_text(f"💥 {app_name(kind)} no reconocio ese titulo (id TMDB {tmdb}).")
            title = f"{obj.get('title')}{_year(obj.get('year'))}"
            remember_pending(chat_id, kind, obj["id"], tmdb, title, label)
            log_request("add", update, kind=kind, tmdb=tmdb, arr_id=obj["id"], title=title, shelf=shelf, mode=mode)
            scope = {"all": "", "last": " (solo la ultima temporada)"}.get(mode, f" (solo la temporada {mode})") if kind == "s" else ""
            left = "" if role == "admin" else f"\nTe quedan {max(0, DAILY_QUOTA - quota_used(update.effective_user.id))} peticiones hoy."
            await q.message.reply_text(
                f"➕ Pedida: <b>{_h(title)}</b>{_h(scope)}\n"
                f"📁 {shelf} · {_h(why)}\n"
                f"Ya esta buscando; te aviso cuando se pueda ver en Emby.{left}",
                parse_mode=ParseMode.HTML)
            if role != "admin":
                await notify_admins(ctx.application, f"➕ <b>{_h(label)}</b> pidio {'🎬' if kind == 'm' else '📺'} "
                                                     f"<b>{_h(title)}</b>{_h(scope)} → {shelf}")
        elif action == "rs":
            await q.answer("Relanzando…")
            arr_id = int(parts[2])
            await asyncio.to_thread(do_research, kind, arr_id)
            _, obj = await asyncio.to_thread(_arrived, kind, arr_id)
            title = f"{obj.get('title')}{_year(obj.get('year'))}"
            remember_pending(chat_id, kind, arr_id, obj.get("tmdbId"), title, label)
            log_request("research", update, kind=kind, arr_id=arr_id, title=title)
            await q.message.reply_text(f"🔁 Busqueda relanzada para <b>{_h(title)}</b>; te aviso si llega.",
                                       parse_mode=ParseMode.HTML)
    except requests.HTTPError as e:
        body = ""
        try:
            j = e.response.json()
            body = "; ".join(x.get("errorMessage", "") for x in j) if isinstance(j, list) else str(j)[:200]
        except Exception:
            pass
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 {app_name(kind)} respondio {e.response.status_code}: {_h(body or e)}",
                                   parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 Error: {_h(e)}", parse_mode=ParseMode.HTML)


async def post_init(app):
    global _emby_server_id
    try:
        _emby_server_id = (await asyncio.to_thread(_emby_get, "System/Info"))["Id"]
    except Exception:
        log.exception("Emby System/Info")
    PIDFILE.write_text(str(os.getpid()))
    app.bot_data["pending_task"] = asyncio.get_running_loop().create_task(pending_loop(app))
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([BotCommand("start", "Como funciona"), BotCommand("pendientes", "Lo que pediste y aun no llega"),
                                       BotCommand("vincular", "Vincular tu cuenta de Emby"), BotCommand("id", "Tu id de Telegram")])
    except Exception:
        log.warning("set_my_commands fallo")
    log.info("listo; admins=%s usuarios=%d cupo=%d/dia", sorted(ADMINS) or "NINGUNO", len(load_users()), DAILY_QUOTA)


async def post_shutdown(app):
    t = app.bot_data.get("pending_task")
    if t:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN vacio en .env")
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler(["start", "help", "ayuda"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["vincular", "link"], cmd_link))
    app.add_handler(CommandHandler("usuarios", cmd_users))
    app.add_handler(CommandHandler("desvincular", cmd_unlink))
    app.add_handler(CommandHandler("pendientes", cmd_pending))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
