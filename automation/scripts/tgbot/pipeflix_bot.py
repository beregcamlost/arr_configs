#!/usr/bin/env python3
"""PIPEFLIX Telegram bot (v3) — escribe lo que quieras y el bot lo resuelve.

Perfiles (quien puede que):
    admin      TELEGRAM_ALLOWED_IDS (.env) o rol "admin" en telegram_roles.json. Todo:
               reiniciar Emby, tareas del pipeline (/tareas), logs, roles, desvincular.
    moderador  rol "mod" en telegram_roles.json (/rol <id|usuario> mod). Sin cupo; ve y
               atiende pendientes y reportes de todos; arregla subs (Bazarr / nuestro
               modelo), corrige caratulas, busca copias en un idioma y las baja; /sistema,
               /sesiones, /usuarios (solo lectura).
    usuario    se vincula solo con su cuenta de Emby (/vincular). Busca, pide con cupo
               (TELEGRAM_DAILY_QUOTA/dia), ve sus pendientes, reporta problemas de un
               titulo (sin subs, caratula, no reproduce, en otro idioma) -> ticket a los
               moderadores.

Que entiende (sin comandos, en lenguaje normal):
    "merlina" / "dune 2021" / "stranger things temporada 5"      -> busca y muestra tarjeta
    "dune en frances" / "la sirenita con audio latino"           -> idioma pedido: audio/subs
                                                                    en la tarjeta o copia en ese idioma
    "traduce los subs de dune"                                    -> nuestro modelo (en->es)
    "arregla / mejora / busca los subs de moana"                  -> menu de subtitulos
    "arregla la caratula de moana" / "caratulas faltantes"        -> refresco de imagenes
    "reinicia emby" / "estado" / "quien esta viendo"              -> admin / moderador

Backend en pipeflix_ops.py. Corre en mubuntu (ligero); lo pesado (traducir, previews)
corre en un hilo aparte de a uno. Lanzado por bot_up.sh (cron */5 + @reboot).
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
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from streaming.arr_client import add_movie, ensure_tag
from streaming.media_shelf import classify as classify_shelf, shelf_path
from tgbot import pipeflix_ops as ops

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
ROLES_FILE = CACHE_DIR / "telegram_roles.json"
REPORTS_FILE = CACHE_DIR / "telegram_reports.json"
REQUESTS_LOG = CACHE_DIR / "telegram_requests.jsonl"
PIDFILE = pathlib.Path("/tmp/pipeflix_bot.pid")
PENDING_POLL_S = 120
PENDING_GIVEUP_S = 14 * 86400
EMBY_LAG_WARN_S = 3 * 3600    # descargada pero Emby no la muestra: avisar una vez
LIBRARY_TTL_S = 120
QUEUE_TTL_S = 30
LINK_FAILS_PER_HOUR = 5
SERIES_SUB_MAX = 40           # episodios por accion de subs en una serie

_pool = ThreadPoolExecutor(max_workers=6)
_long = ThreadPoolExecutor(max_workers=1)   # trabajos pesados de a uno (appbox de 2 vCPU)
_emby_server_id = ""
_emby_idx = {"at": 0.0, "map": {}}
_library_cache = {"at": 0.0, "movies": {}, "series": {}}
_queue_cache = {"at": 0.0, "map": {}}
_link_fails = {}  # tg_id -> [timestamps]
_rel_lists = {}   # clave corta -> lista de releases (para los botones de grab)
_rel_seq = [0]

LEVEL = {"user": 1, "mod": 2, "admin": 3}
ROLE_LABEL = {"user": "usuario", "mod": "moderador", "admin": "admin"}
# accion -> rol minimo
PERMS = {
    "search": "user", "add": "user", "report": "user", "pending_all": "mod", "users": "mod",
    "subs": "mod", "cover": "mod", "releases": "mod", "system": "mod", "sessions": "mod", "reports": "mod",
    "restart": "admin", "tasks": "admin", "roles": "admin", "unlink": "admin", "logs": "admin", "subs_redo": "admin",
}


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


async def run_long(fn, *a):
    """Trabajo pesado (traducir, previews, refrescos masivos): de a uno, sin bloquear el bot."""
    return await asyncio.get_running_loop().run_in_executor(_long, lambda: fn(*a))


# ---------------------------------------------------------------- perfiles
def load_users():
    return _load_json(USERS_FILE, {})


def load_roles():
    return _load_json(ROLES_FILE, {})


def role_of(tg_id):
    """-> 'admin' | 'mod' | 'user' | None"""
    if tg_id in ADMINS:
        return "admin"
    r = load_roles().get(str(tg_id))
    if r in ("admin", "mod"):
        return r
    if str(tg_id) in load_users():
        return "user"
    return None


def can(role, action):
    return bool(role) and LEVEL[role] >= LEVEL[PERMS[action]]


def who(update):
    """-> (rol, etiqueta legible)"""
    u = update.effective_user
    if not u:
        return None, "?"
    role = role_of(u.id)
    if role in ("admin", "mod"):
        return role, u.username or u.full_name or str(u.id)
    if role == "user":
        return "user", load_users().get(str(u.id), {}).get("emby_user") or str(u.id)
    return None, u.username or u.full_name or str(u.id)


def staff_ids():
    ids = set(ADMINS)
    ids |= {int(k) for k, v in load_roles().items() if v in ("admin", "mod") and k.isdigit()}
    return ids


def is_admin(tg_id):
    return role_of(tg_id) == "admin"


# ---------------------------------------------------------------- 1. limpiar la frase
_PREFIX = re.compile(
    r"^(?:(?:hola|oye|epa|porfa|por favor|pls)[,\s]+)*"
    r"(?:(?:quiero|quisiera|me gustaria|me gustar[ií]a|queremos|necesito)\s+ver|quiero|quisiera|"
    r"busca(?:me|r)?|pon(?:me|er)?|agrega(?:me|r)?|a[nñ]ade(?:me)?|descarga(?:me|r)?|baja(?:me|r)?|rebaja(?:me|r)?|"
    r"pide|pedir|tienes|tienen|tendr[aá]s|hay|est[aá]|existe|ver|mira|dame|consigue(?:me)?)\s+", re.I)
_MEDIA = re.compile(
    r"^(?:(?:la|el|una|un|las|los)\s+)?(?:peli(?:cula|culas)?|pel[ií]cula|pelis|serie|series|anime|dorama|"
    r"documental|caricatura)\s+(?:de\s+|del\s+|llamad[ao]\s+|que se llama\s+)?", re.I)
_TRAIL = re.compile(r"[\s,]*(?:por favor|porfa|porfis|pls|please|gracias|completa|entera|en emby|en espa[nñ]ol|"
                    r"latino|subtitulada)\s*$", re.I)
_SEASON = re.compile(r"\b(?:temporada|temp|season|t)\s*\.?\s*(\d{1,2})\b|\bs(\d{2})(?:e\d{1,3})?\b", re.I)
_YEAR = re.compile(r"(?:^|[\s(])((?:19|20)\d{2})\)?\s*$")
_LANG_WORDS = r"espa[nñ]ol|latino|castellano|ingl[eé]s|franc[eé]s|japon[eé]s|italiano|alem[aá]n|portugu[eé]s|brasile[nñ]o|coreano|chino|mandar[ií]n|ruso|hindi"
_LANG_REQ = re.compile(
    r"\b(?:(?:con\s+)?(?:audio|doblad[ao]|doblaje|hablad[ao])\s+(?:en\s+|al\s+)?|(?:con\s+)?sub(?:t[ií]tulos?|s)?\s+(?:en\s+|al\s+)?|en\s+)"
    r"(" + _LANG_WORDS + r")\b", re.I)


def parse_query(text):
    """-> (limpio, ano, temporada, quiere_espanol, idioma_pedido|None).
    'quiero ver la pelicula de barbie' -> ('barbie', None, None, False, None);
    'dune 2021 en frances' -> ('dune', 2021, None, False, 'frances')."""
    lang = None
    m = _LANG_REQ.search(text)
    if m:
        lang = ops.norm_lang(m.group(1))
        text = (text[:m.start()] + " " + text[m.end():]).strip()
    want_es = lang == "espanol" or bool(re.search(r"en espa[nñ]ol|latino|doblad[ao]|castellano|audio espa|en castellano", text, re.I))
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
    return q or text.strip(), year, season, want_es, lang


# ---------------------------------------------------------------- 1b. intenciones (antes de buscar)
_INTENTS = [
    ("restart", re.compile(r"^\s*(?:reinicia(?:r)?|restart|resetea(?:r)?)\s+(?:el\s+)?emby\s*[.!]*$", re.I)),
    ("status", re.compile(r"^\s*(?:estado|status|salud|sistema|health|como (?:esta|va) (?:todo|el sistema|el servidor|emby))\s*[?¿!.]*$", re.I)),
    ("sessions", re.compile(r"^\s*(?:sesiones|qui[eé]n(?:es)? (?:est[aá]n?|anda) viendo|qu[eé] est[aá]n viendo|viendo ahora)\s*[?¿!.]*$", re.I)),
    ("covers_all", re.compile(r"^\s*(?:arregla|corrige|repara|refresca|actualiza|revisa)\w*\s+(?:las\s+)?(?:car[aá]tulas|portadas|posters|im[aá]genes)(?:\s+(?:faltantes|que faltan|rotas))?\s*[.!]*$|^\s*car[aá]tulas faltantes\s*$", re.I)),
    ("cover", re.compile(r"^\s*(?:arregla|arreglar|corrige|corregir|repara|reparar|refresca|refrescar|actualiza|actualizar|cambia|cambiar)\w*\s+(?:la\s+|el\s+)?(?:car[aá]tula|portada|poster|imagen|metadata|metadatos|info)\s+(?:de\s+|del\s+|a\s+)?(.+)$", re.I)),
    ("translate", re.compile(r"^\s*(?:traduce|traducir|traduceme|tradu[zc]\w*)\s+(?:los\s+|las\s+|el\s+|la\s+)?(?:sub(?:t[ií]tulos?|s)?\s+)?(?:de\s+|del\s+|a\s+|para\s+)?(.+)$", re.I)),
    ("subs", re.compile(r"^\s*(?:arregla|arreglar|mejora|mejorar|repara|reparar|busca|buscar|consigue|conseguir|baja|bajar|revisa|revisar|corrige|corregir|sincroniza)\w*\s+(?:los\s+|las\s+|el\s+|la\s+|unos\s+)?(?:mejores\s+)?sub(?:t[ií]tulos?|s)?(?:\s+(?:en\s+)?(?:espa[nñ]ol|es|latino))?\s+(?:de\s+|del\s+|a\s+|para\s+)?(.+)$", re.I)),
    ("subs2", re.compile(r"^(.+?)\s+(?:no tiene|sin|le faltan?)\s+(?:los\s+)?sub(?:t[ií]tulos?|s)?(?:\s+(?:en\s+)?espa[nñ]ol)?\s*[.!]*$", re.I)),
]


def parse_intent(text):
    """-> (intent, argumento) o (None, None)."""
    for name, rx in _INTENTS:
        m = rx.match(text)
        if m:
            arg = (m.group(1).strip(" .!¡¿?") if m.groups() else None)
            return ("subs" if name == "subs2" else name), arg
    return None, None


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


LANG_NAMES = {"spa": "Español", "es": "Español", "eng": "Inglés", "en": "Inglés", "jpn": "Japonés", "ja": "Japonés",
              "kor": "Coreano", "ko": "Coreano", "fra": "Francés", "fre": "Francés", "fr": "Francés", "por": "Portugués",
              "pt": "Portugués", "ger": "Alemán", "deu": "Alemán", "de": "Alemán", "ita": "Italiano", "it": "Italiano",
              "chi": "Chino", "zho": "Chino", "zh": "Chino", "hin": "Hindi", "rus": "Ruso", "und": "?"}
ES_LANGS = {"spa", "es", "esp", "es-es", "es-419", "es-mx", "es-la", "spanish", "castilian", "lat", "latin"}


def emby_streams(item_id):
    """-> {'audio': [nombres], 'subs': [nombres], 'es_audio': bool, 'es_subs': bool, 'audio_codes', 'sub_codes'}"""
    items = _emby_get("Items", Ids=item_id, Fields="MediaStreams").get("Items", [])
    streams = (items[0].get("MediaStreams") if items else None) or []
    out = {"audio": [], "subs": [], "es_audio": False, "es_subs": False, "audio_codes": set(), "sub_codes": set()}
    for st in streams:
        lang = (st.get("Language") or "").lower()
        name = LANG_NAMES.get(lang) or st.get("DisplayLanguage") or lang or "?"
        if st.get("Type") == "Audio":
            if name not in out["audio"]:
                out["audio"].append(name)
            out["es_audio"] |= lang in ES_LANGS
            out["audio_codes"].add(lang)
        elif st.get("Type") == "Subtitle":
            if name not in out["subs"]:
                out["subs"].append(name)
            out["es_subs"] |= lang in ES_LANGS
            out["sub_codes"].add(lang)
    return out


def resolve(kind, tmdb, tvdb=None, title=None, year=None):
    """-> {'status': 'emby'|'downloaded'|'requested'|'missing', 'emby': item, 'lib': info, 'pct': int|None}"""
    idx = emby_index()
    item = idx.get((kind, "tmdb", tmdb))
    if not item and tvdb:
        item = idx.get((kind, "tvdb", tvdb))
    if not item and title:
        item = idx.get((kind, "name", (_fold(title), year)))
    lib_m, lib_s = library_ids()
    lib = lib_m.get(tmdb) if kind == "m" else (lib_s.get(tmdb) or (tvdb and lib_s.get(("tvdb", tvdb))))
    if item:
        streams = None
        if kind == "m":
            try:
                streams = emby_streams(item["Id"])
            except Exception:
                log.exception("streams %s", item["Id"])
        return {"status": "emby", "emby": item, "lib": lib, "pct": None, "streams": streams}
    if lib and lib["has_file"]:
        return {"status": "downloaded", "emby": None, "lib": lib, "pct": None}
    if lib:
        return {"status": "requested", "emby": None, "lib": lib, "pct": queue_progress().get((kind, lib["arr_id"]))}
    return {"status": "missing", "emby": None, "lib": None, "pct": None}


def do_search(text):
    """-> (clean, year, season, want_es, lang, results, details_top, status_top, {(kind,tmdb): status_str})"""
    clean, year, season, want_es, lang = parse_query(text)
    f_idx, f_lib = _pool.submit(emby_index), _pool.submit(library_ids)   # calientan cache en paralelo
    results = tmdb_search(clean, year, raw=text)
    f_idx.result()
    f_lib.result()
    if not results:
        return clean, year, season, want_es, lang, [], None, None, {}
    top = results[0]
    details = tmdb_details(top["kind"], top["tmdb"])
    status = resolve(top["kind"], top["tmdb"], details.get("tvdb"), details.get("title"), details.get("year"))
    alts = {(r["kind"], r["tmdb"]): resolve(r["kind"], r["tmdb"])["status"] for r in results[1:]}
    return clean, year, season, want_es, lang, results, details, status, alts


def find_one(text):
    """Para las intenciones: el mejor candidato + su estado. -> (details, status) o (None, None)"""
    clean, year, season, want_es, lang = parse_query(text)
    results = tmdb_search(clean, year, raw=text)
    if not results:
        return None, None
    top = results[0]
    details = tmdb_details(top["kind"], top["tmdb"])
    status = resolve(top["kind"], top["tmdb"], details.get("tvdb"), details.get("title"), details.get("year"))
    return details, status


# ---------------------------------------------------------------- 4. tarjeta
def build_card(details, status, alternatives, alt_status, season_hint=None, want_es=False, role="user", lang=None):
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
    lang_label = ops.LANG_LABEL.get(lang) if lang else None
    staff = LEVEL.get(role, 0) >= LEVEL["mod"]

    rows, st = [], status["status"]
    if st == "emby":
        lines.append("\n✅ <b>Ya esta en Emby</b>")
        sm = status.get("streams")
        if sm and (sm["audio"] or sm["subs"]):
            lines.append(f"🔊 {', '.join(sm['audio']) or '?'} · 💬 Subs: {', '.join(sm['subs']) or 'ninguno'}")
            if want_es and not sm["es_audio"] and lang in (None, "espanol"):
                lines.append("⚠️ Sin audio en espanol" + ("; si tiene subtitulos en espanol." if sm["es_subs"]
                             else "; los subtitulos en espanol se generan solos en unas horas."))
            if lang and lang != "espanol":
                has_a, has_s = ops.has_lang(sm["audio_codes"], lang), ops.has_lang(sm["sub_codes"], lang)
                lines.append(f"{'✅' if has_a else '❌'} audio en {lang_label} · {'✅' if has_s else '❌'} subs en {lang_label}")
        rows.append([InlineKeyboardButton("🍿 Abrir en Emby", url=emby_link(status["emby"]["Id"]))])
        if staff:
            rows.append([InlineKeyboardButton("💬 Subs", callback_data=f"sub:menu:{kind}:{d['tmdb']}"),
                         InlineKeyboardButton("🖼 Caratula", callback_data=f"cov:{kind}:{d['tmdb']}"),
                         InlineKeyboardButton("🌐 Otra copia", callback_data=f"relq:{kind}:{d['tmdb']}:{lang or '-'}")])
        else:
            rows.append([InlineKeyboardButton("🚩 Reportar un problema", callback_data=f"rep:menu:{kind}:{d['tmdb']}:{lang or '-'}")])
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
        if staff and lang:
            rows.append([InlineKeyboardButton(f"🌐 Elegir copia en {lang_label}", callback_data=f"rel:{kind}:{d['tmdb']}:{lang}")])
    else:
        today = dt.date.today().isoformat()
        if kind == "m" and d.get("digital") and d["digital"] > today:
            lines.append(f"\n🎟 Aun no salio en digital (sale ~{d['digital']}); si la pides, llega sola ese dia.")
        elif kind == "m" and not d.get("digital") and d.get("theatrical") and d["theatrical"] > today:
            lines.append(f"\n🎟 Se estrena en cines el {d['theatrical']}; puedes dejarla pedida.")
        elif kind == "m" and not d.get("digital") and d.get("theatrical") and d["theatrical"] > (dt.date.today() - dt.timedelta(days=100)).isoformat():
            lines.append("\n🎟 Todavia en cines; se descargara cuando salga en digital.")
        if lang and lang != "espanol":
            lines.append(f"\n🗣 Pediste <b>{lang_label}</b>: " + ("puedes elegir la copia tu mismo 👇" if staff else
                         "se pide en la mejor calidad y aviso a los moderadores para que busquen una copia en ese idioma."))
        elif want_es:
            lines.append("\n🗣 Se pide en la mejor calidad; se prefieren copias con audio latino cuando existen, "
                         "y los subtitulos en espanol se agregan solos.")
        lg = lang or "-"
        if kind == "m":
            rows.append([InlineKeyboardButton("➕ Pedir pelicula", callback_data=f"add:m:{d['tmdb']}:all:{lg}")])
        else:
            if season_hint:
                rows.append([InlineKeyboardButton(f"➕ Solo temporada {season_hint}", callback_data=f"add:s:{d['tmdb']}:{season_hint}:{lg}")])
            if d.get("episodes", 0) > BIG_SERIES_EPISODES:
                rows.append([InlineKeyboardButton(f"➕ Toda la serie ({d['episodes']} ep)", callback_data=f"add:s:{d['tmdb']}:all:{lg}")])
                rows.append([InlineKeyboardButton("➕ Solo la ultima temporada", callback_data=f"add:s:{d['tmdb']}:last:{lg}")])
            else:
                rows.append([InlineKeyboardButton("➕ Pedir serie completa", callback_data=f"add:s:{d['tmdb']}:all:{lg}")])

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


# ---------------------------------------------------------------- acciones Radarr/Sonarr
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


def locate(kind, tmdb):
    """Para las acciones sobre un titulo: -> (details, status) con status['emby'] y status['lib'] cuando existen."""
    details = tmdb_details(kind, tmdb)
    status = resolve(kind, tmdb, details.get("tvdb"), details.get("title"), details.get("year"))
    return details, status


def video_paths(kind, item):
    """Rutas reales de los videos de un item de Emby (1 para peli, N para serie)."""
    if kind == "m":
        it = ops.emby_item(item["Id"])
        p = it.get("Path") if it else None
        return [(it, os.path.realpath(p))] if p else []
    out = []
    for ep in ops.emby_children(item["Id"]):
        if ep.get("Path"):
            out.append((ep, os.path.realpath(ep["Path"])))
    return out


# ---------------------------------------------------------------- bitacora, cupo, reportes
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
                if r.get("event") in ("add", "sub_req") and r.get("tg_id") == tg_id and r.get("ts", 0) > since:
                    n += 1
    except FileNotFoundError:
        pass
    return n


def new_report(update, kind, tmdb, title, rtype, lang=None):
    reps = _load_json(REPORTS_FILE, {})
    rid = str(int(time.time() * 1000))[-8:]
    reps[rid] = {"id": rid, "kind": kind, "tmdb": tmdb, "title": title, "type": rtype, "lang": lang,
                 "by": who(update)[1], "chat_id": update.effective_chat.id, "since": time.time(), "open": True}
    _save_json(REPORTS_FILE, reps)
    return rid


REPORT_TYPES = {"nosubs": "💬 sin subtitulos en espanol", "noaudio": "🔊 sin audio en espanol", "noplay": "▶️ no reproduce / se traba",
                "cover": "🖼 caratula o info mal", "lang": "🌐 la quiero en otro idioma", "other": "❓ otra cosa"}


def emby_authenticate(username, password):
    r = requests.post(f"{EMBY_API}/Users/AuthenticateByName",
                      json={"Username": username, "Pw": password or ""},
                      headers={"X-Emby-Authorization": 'MediaBrowser Client="pipeflix", Device="telegram", '
                                                       'DeviceId="pipeflix-bot", Version="3"'},
                      timeout=30)
    if r.status_code in (401, 403):
        return None
    r.raise_for_status()
    return r.json().get("User") or {}


async def notify_staff(app, text, markup=None, only_admins=False):
    for tid in (ADMINS if only_admins else staff_ids()):
        try:
            await app.bot.send_message(tid, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup)
        except Exception:
            log.warning("no pude avisar a %s", tid)


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


# ---------------------------------------------------------------- ayuda
ONBOARD = ("👋 Soy <b>PIPEFLIX</b>, el bot del Emby.\n\n"
           "Para entrar, vincula tu cuenta de Emby (la misma con la que ves las pelis):\n"
           "<code>/vincular tu_usuario tu_contrasena</code>\n\n"
           "Borro ese mensaje en cuanto lo leo. Despues solo escribes el nombre de lo que quieras ver 🍿")

HELP_USER = ("Escribeme el nombre de una pelicula o serie (en espanol o en ingles, con o sin ano) y te digo si ya esta "
             "en Emby; si no, la pides con un toque y te aviso cuando ya se pueda ver.\n\n"
             "Ejemplos: <i>merlina</i> · <i>dune 2021</i> · <i>stranger things temporada 5</i> · <i>la sirenita en frances</i>\n\n"
             "Si algo esta mal (sin subs, sin audio en espanol, no reproduce, caratula), abre la tarjeta y toca "
             "<b>🚩 Reportar un problema</b>.\n\n"
             "/pendientes — lo que pediste y aun no llega\n/id — tu id de Telegram")

HELP_MOD = ("\n\n<b>Moderador</b> — sin cupo, y en cada tarjeta: 💬 Subs (bajar de Bazarr, traducir con nuestro modelo, "
            "encolar mantenimiento), 🖼 Caratula (refrescar imagen y metadatos), 🌐 Otra copia (buscar y bajar una copia "
            "en un idioma).\n"
            "Tambien en lenguaje normal: <i>traduce los subs de dune</i> · <i>arregla la caratula de moana</i> · "
            "<i>busca subs de merlina</i> · <i>dune en frances</i>\n"
            "/sistema — salud del pipeline y de Emby\n/sesiones — quien esta viendo\n/reportes — problemas abiertos\n"
            "/usuarios — vinculados y roles\n/pendientes — de todos")

HELP_ADMIN = ("\n\n<b>Admin</b>\n/emby — reiniciar, caratulas faltantes, salud\n/tareas — scripts del pipeline (estante, previews, "
              "huerfanas, torrents, faststart…)\n/log &lt;nombre&gt; — cola de un log\n"
              "/rol &lt;id o usuario&gt; admin|mod|usuario\n/desvincular &lt;id o usuario&gt;\n"
              "Y escribiendo: <i>reinicia emby</i> · <i>caratulas faltantes</i> · <i>estado</i>")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    text = f"🎬 <b>PIPEFLIX</b> — hola, {_h(label)} ({ROLE_LABEL[role]})\n\n" + HELP_USER
    if role == "user":
        text += f"\n\nTienes {max(0, DAILY_QUOTA - quota_used(update.effective_user.id))} peticiones disponibles hoy."
    if LEVEL[role] >= LEVEL["mod"]:
        text += HELP_MOD
    if role == "admin":
        text += HELP_ADMIN
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
    await notify_staff(ctx.application, f"🔗 <b>{_h(user.get('Name'))}</b> (Emby) vinculo Telegram "
                                        f"@{_h(u.username or u.full_name)} (<code>{u.id}</code>).")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "users"):
        return
    users, roles = load_users(), load_roles()
    lines = [f"• 👑 admin <code>{a}</code>" for a in sorted(ADMINS)]
    for tid, r in roles.items():
        if int(tid) not in ADMINS:
            lines.append(f"• {'👑' if r == 'admin' else '🛡'} {ROLE_LABEL.get(r, r)} <code>{tid}</code> · {_h(users.get(tid, {}).get('tg_name', ''))}")
    for tid, r in users.items():
        if roles.get(tid) in ("admin", "mod") or int(tid) in ADMINS:
            continue
        lines.append(f"• <b>{_h(r['emby_user'])}</b> ← @{_h(r.get('tg_name'))} <code>{tid}</code> · {r.get('linked', '')[:10]} · "
                     f"hoy {quota_used(int(tid))}/{DAILY_QUOTA}")
    await update.message.reply_text("👥 <b>Usuarios</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


def _find_tg_id(target):
    target = (target or "").strip().lstrip("@").lower()
    if target.isdigit():
        return target
    for tid, r in load_users().items():
        if (r.get("emby_user") or "").lower() == target or (r.get("tg_name") or "").lower() == target:
            return tid
    return None


async def cmd_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "roles"):
        return
    args = ctx.args or []
    if len(args) != 2 or args[1].lower() not in ("admin", "mod", "moderador", "usuario", "user"):
        return await update.message.reply_text("Uso: <code>/rol &lt;id o usuario&gt; admin|mod|usuario</code>", parse_mode=ParseMode.HTML)
    tid = _find_tg_id(args[0])
    if not tid:
        return await update.message.reply_text("No encuentro ese usuario (usa el id de Telegram o el usuario de Emby vinculado).")
    new = {"moderador": "mod", "user": "usuario"}.get(args[1].lower(), args[1].lower())
    roles = load_roles()
    if new == "usuario":
        roles.pop(tid, None)
    else:
        roles[tid] = new
    _save_json(ROLES_FILE, roles)
    log_request("role", update, target=tid, role=new)
    await update.message.reply_text(f"✅ <code>{tid}</code> ahora es <b>{new}</b>.", parse_mode=ParseMode.HTML)
    try:
        await ctx.application.bot.send_message(int(tid), f"🛡 Ahora eres <b>{ROLE_LABEL.get(new, new)}</b> en PIPEFLIX. Escribe /start para ver que puedes hacer.",
                                               parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def cmd_unlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "unlink"):
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
    all_ = can(role, "pending_all")
    mine = [r for r in p.values() if all_ or r["chat_id"] == update.effective_chat.id]
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
        owner = f" · {_h(r.get('who'))}" if all_ else ""
        lines.append(f"• {ico} {_h(r['title'])} — {state} · hace {_ago(r['since'])}{owner}")
    await update.message.reply_text("⏳ <b>Pendientes</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- sistema / emby / tareas (staff)
def _sistema_text():
    sev, lines, when = ops.health_last_block()
    bad = [l for l in lines if not l.startswith("[OK]")]
    ico = {"OK": "🟢", "WARN": "🟡", "CRIT": "🔴"}.get(sev, "⚪")
    out = [f"{ico} <b>Pipeline: {sev}</b> <i>({when} UTC)</i>"]
    out += [f"• {_h(l)}" for l in bad[:12]] or ["• todo OK"]
    for p, tb, pct in ops.disk():
        out.append(f"💽 {p}: {tb} TB libres ({pct}%)")
    return "\n".join(out)


async def cmd_sistema(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "system"):
        return
    text = await asyncio.to_thread(_sistema_text)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🩺 Salud de Emby ahora (20 s)", callback_data="task:salud"),
                                InlineKeyboardButton("👀 Sesiones", callback_data="sess")]])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def _sesiones_text():
    ss = ops.emby_sessions()
    if not ss:
        return "😴 Nadie esta viendo nada ahora."
    lines = ["👀 <b>Viendo ahora</b>"]
    for s in ss:
        st = "⏸" if s["paused"] else "▶️"
        pct = f" · {s['pct']}%" if s["pct"] is not None else ""
        lines.append(f"• {st} <b>{_h(s['user'])}</b> — {_h(s['item'])}{pct} <i>({_h(s['client'])})</i>")
    return "\n".join(lines)


async def cmd_sesiones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "sessions"):
        return
    await update.message.reply_text(await asyncio.to_thread(_sesiones_text), parse_mode=ParseMode.HTML)


async def cmd_emby(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "restart"):
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Reiniciar Emby", callback_data="emby:restart")],
                               [InlineKeyboardButton("🖼 Caratulas faltantes", callback_data="cov:all"),
                                InlineKeyboardButton("🩺 Salud", callback_data="task:salud")],
                               [InlineKeyboardButton("👀 Sesiones", callback_data="sess"),
                                InlineKeyboardButton("🗂 Estante idioma", callback_data="task:estante")]])
    await update.message.reply_text("🎛 <b>Emby</b>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_tareas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "tasks"):
        return
    rows = [[InlineKeyboardButton(lbl, callback_data=f"task:{name}")] for name, (lbl, _a, _t) in ops.TASKS.items()]
    await update.message.reply_text("🧰 <b>Tareas del pipeline</b> (corren en mubuntu; las marcadas BORRAR piden confirmacion)",
                                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "logs"):
        return
    name = (ctx.args or ["pipeline_health"])[0]
    name = re.sub(r"[^A-Za-z0-9_.-]", "", name)
    if not name.endswith(".log"):
        name += ".log"
    text = await asyncio.to_thread(ops.recent_log, name, 25)
    await update.message.reply_text(f"📄 <b>{_h(name)}</b>\n<pre>{_h(text)[-3600:]}</pre>", parse_mode=ParseMode.HTML)


async def cmd_reportes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "reports"):
        return
    reps = [r for r in _load_json(REPORTS_FILE, {}).values() if r.get("open")]
    if not reps:
        return await update.message.reply_text("Sin reportes abiertos 👌")
    for r in sorted(reps, key=lambda r: r["since"])[:15]:
        await update.message.reply_text(_report_text(r), parse_mode=ParseMode.HTML, reply_markup=_report_kb(r))


def _report_text(r):
    lang = f" ({ops.LANG_LABEL.get(r.get('lang'), r.get('lang'))})" if r.get("lang") else ""
    return (f"🚩 <b>Reporte #{r['id']}</b> · {REPORT_TYPES.get(r['type'], r['type'])}{lang}\n"
            f"{'🎬' if r['kind'] == 'm' else '📺'} <b>{_h(r['title'])}</b> · por {_h(r['by'])} · hace {_ago(r['since'])}")


def _report_kb(r):
    k, t = r["kind"], r["tmdb"]
    row = []
    if r["type"] in ("nosubs", "other", "noplay"):
        row.append(InlineKeyboardButton("💬 Subs", callback_data=f"sub:menu:{k}:{t}"))
    if r["type"] in ("cover", "other"):
        row.append(InlineKeyboardButton("🖼 Caratula", callback_data=f"cov:{k}:{t}"))
    if r["type"] in ("lang", "noaudio", "noplay", "other"):
        row.append(InlineKeyboardButton("🌐 Otra copia", callback_data=f"relq:{k}:{t}:{r.get('lang') or '-'}"))
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("✅ Resuelto", callback_data=f"repok:{r['id']}")]])


# ---------------------------------------------------------------- acciones sobre un titulo (staff)
async def _need_emby(q, kind, tmdb):
    """-> (details, status, item) o (None, None, None) avisando por que."""
    details, status = await asyncio.to_thread(locate, kind, tmdb)
    if status["status"] != "emby":
        await q.message.reply_text(f"«{_h(details['title'])}» no esta en Emby todavia ({status['status']}).", parse_mode=ParseMode.HTML)
        return None, None, None
    return details, status, status["emby"]


def _subs_status_text(kind, details, item, lib):
    vids = video_paths(kind, item)
    if kind == "m":
        if not vids:
            return "sin ruta en Emby", vids
        it, path = vids[0]
        sc = ops.sidecars(path)
        a, s = ops.streams_summary(it)
        return (f"📄 <code>{_h(os.path.basename(path))}</code>\n"
                f"🔊 audio: {', '.join(sorted(a)) or '?'} · 💬 embebidos: {', '.join(sorted(s)) or 'ninguno'}\n"
                f"📎 sidecars: " + (", ".join(f"{l}×{len(v)}" for l, v in sorted(sc.items())) or "ninguno")), vids
    tot, no_es = 0, []
    for ep, path in vids:
        tot += 1
        sc = ops.sidecars(path)
        a, s = ops.streams_summary(ep)
        if "es" not in sc and not (s & ES_LANGS):
            no_es.append(f"S{ep.get('ParentIndexNumber', 0):02d}E{ep.get('IndexNumber', 0):02d}")
    txt = f"📺 {tot} episodios · sin subs es: {len(no_es)}"
    if no_es:
        txt += " (" + ", ".join(no_es[:12]) + ("…" if len(no_es) > 12 else "") + ")"
    return txt, vids


async def subs_menu(q, kind, tmdb, role):
    details, status, item = await _need_emby(q, kind, tmdb)
    if not item:
        return
    txt, vids = await asyncio.to_thread(_subs_status_text, kind, details, item, status.get("lib"))
    lib = status.get("lib")
    rows = [[InlineKeyboardButton("🔎 Bazarr: bajar es", callback_data=f"sub:dl:{kind}:{tmdb}"),
             InlineKeyboardButton("🤖 Traducir en→es (nuestro modelo)", callback_data=f"sub:tr:{kind}:{tmdb}")],
            [InlineKeyboardButton("🧹 Encolar mantenimiento (01:00Z)", callback_data=f"sub:q:{kind}:{tmdb}"),
             InlineKeyboardButton("🌍 Bazarr: traducir de otro idioma", callback_data=f"sub:bz:{kind}:{tmdb}")]]
    if can(role, "subs_redo"):
        rows.append([InlineKeyboardButton("🔁 Rehacer es con nuestro modelo (aparta el actual)", callback_data=f"sub:trr:{kind}:{tmdb}")])
    note = "" if lib else "\n⚠️ No esta en Radarr/Sonarr: Bazarr no lo conoce; solo sirve nuestro modelo."
    await q.message.reply_text(f"💬 <b>Subs de {_h(details['title'])}</b>\n{txt}{note}", parse_mode=ParseMode.HTML,
                               reply_markup=InlineKeyboardMarkup(rows))


def _do_subs(action, kind, item, lib, redo=False):
    """Corre en el hilo largo. -> texto de resultado."""
    vids = video_paths(kind, item)
    if not vids:
        return "sin rutas de video en Emby"
    out = []
    if action == "dl":
        if not lib:
            return "no esta en Radarr/Sonarr; Bazarr no puede buscarlo"
        if kind == "m":
            ops.bazarr_download_movie(lib["arr_id"], "es")
            sc = ops.sidecars(vids[0][1])
            out.append("Bazarr busco el mejor es; ahora hay " + (", ".join(f"{l}×{len(v)}" for l, v in sorted(sc.items())) or "nada"))
        else:
            eps = {e["sonarrEpisodeId"]: e for e in ops.bazarr_episodes(lib["arr_id"])}
            missing = [e for e in eps.values() if any(m.get("code2") == "es" for m in e.get("missing_subtitles") or [])]
            n = 0
            for e in missing[:SERIES_SUB_MAX]:
                try:
                    ops.bazarr_download_episode(lib["arr_id"], e["sonarrEpisodeId"], "es")
                    n += 1
                except Exception as ex:
                    out.append(f"ep {e.get('sonarrEpisodeId')}: {ex}")
            out.insert(0, f"Bazarr: {n} de {len(missing)} episodios sin es buscados" + (f" (tope {SERIES_SUB_MAX})" if len(missing) > SERIES_SUB_MAX else ""))
    elif action in ("tr", "trr"):
        done, fail = 0, []
        targets = vids if kind == "m" else [(e, p) for e, p in vids if redo or "es" not in ops.sidecars(p)]
        for ep, path in targets[:SERIES_SUB_MAX]:
            ok, msg = ops.translate_with_model(path, redo=redo)
            if ok:
                done += 1
            else:
                fail.append(f"{os.path.basename(path)[:50]}: {msg.splitlines()[-1] if msg else '?'}")
        out.append(f"🤖 traducidos: {done} de {len(targets[:SERIES_SUB_MAX])}" + (f" (tope {SERIES_SUB_MAX})" if len(targets) > SERIES_SUB_MAX else ""))
        out += fail[:6]
        if kind == "m" and done:
            try:
                ops.emby_scan_folder(os.path.dirname(vids[0][1]))
            except Exception:
                pass
    elif action == "q":
        ok, msg = ops.sqm_enqueue([p for _, p in vids][:200])
        out.append(("🧹 encolados para el auto-maintain de las 01:00Z: %d" % min(len(vids), 200)) if ok else f"fallo: {msg}")
    elif action == "bz":
        if not lib:
            return "no esta en Radarr/Sonarr; Bazarr no puede traducirlo"
        n = 0
        for ep, path in vids[:SERIES_SUB_MAX]:
            sc = ops.sidecars(path)
            if "es" in sc:
                continue
            src = next((sc[l][0] for l in ("fr", "it", "pt", "de", "ja", "ko", "zh", "en") if l in sc), None)
            if not src:
                continue
            mid = lib["arr_id"] if kind == "m" else None
            if kind == "s":
                # id del episodio en Sonarr: Bazarr lo lista por serie
                key = os.path.basename(path)
                for e in ops.bazarr_episodes(lib["arr_id"]):
                    if os.path.basename(e.get("path") or "") == key:
                        mid = e["sonarrEpisodeId"]
                        break
            if mid is None:
                continue
            try:
                ops.bazarr_translate(kind, mid, src, "es")
                n += 1
            except Exception as ex:
                out.append(f"{os.path.basename(path)[:40]}: {ex}")
        out.insert(0, f"🌍 Bazarr tradujo (Google) {n} archivo(s) desde otro idioma")
    return "\n".join(out) or "nada que hacer"


async def do_cover(q, kind, tmdb):
    details, status, item = await _need_emby(q, kind, tmdb)
    if not item:
        return
    await asyncio.to_thread(ops.emby_refresh, item["Id"], True)
    _emby_idx["at"] = 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🍿 Ver en Emby", url=emby_link(item["Id"]))]])
    await q.message.reply_text(f"🖼 Pedi a Emby que vuelva a bajar caratula y metadatos de <b>{_h(details['title'])}</b> "
                               f"(TMDB/TVDB). Tarda 10-60 s.", parse_mode=ParseMode.HTML, reply_markup=kb)


def _covers_all():
    items, total = ops.emby_missing_images(60)
    for it in items:
        try:
            ops.emby_refresh(it["Id"], True)
        except Exception:
            log.exception("refresh %s", it.get("Id"))
    names = [f"{it.get('SeriesName') + ' · ' if it.get('SeriesName') else ''}{it.get('Name')}" for it in items[:10]]
    return total, len(items), names


async def do_releases(q, kind, tmdb, lang, role):
    details, status = await asyncio.to_thread(locate, kind, tmdb)
    lib = status.get("lib")
    if not lib:
        if status["status"] == "missing":
            await q.message.reply_text("Primero la pido (sin buscar) y luego listo copias…")
            obj, shelf, why, _m = await asyncio.to_thread(do_add, kind, tmdb, "all")
            if not obj:
                return await q.message.reply_text("💥 Radarr/Sonarr no reconocio ese titulo.")
            lib = {"arr_id": obj["id"]}
        else:
            return await q.message.reply_text("No esta en Radarr/Sonarr; no puedo buscar copias.")
    wait = await q.message.reply_text(f"🔎 Buscando copias de <b>{_h(details['title'])}</b> en {ops.LANG_LABEL[lang]} en todos los indexers (hasta 1 min)…",
                                      parse_mode=ParseMode.HTML)
    try:
        rows, total = await asyncio.to_thread(ops.releases, kind, lib["arr_id"], lang, None, 8)
    except Exception as e:
        log.exception("releases")
        return await wait.edit_text(f"💥 La busqueda fallo: {_h(e)}", parse_mode=ParseMode.HTML)
    if not rows:
        return await wait.edit_text(f"🤷 {total} resultados y ninguno parece traer {ops.LANG_LABEL[lang]} (con seeders).", parse_mode=ParseMode.HTML)
    _rel_seq[0] += 1
    key = str(_rel_seq[0])
    _rel_lists[key] = (kind, rows)
    for k in list(_rel_lists)[:-30]:
        _rel_lists.pop(k, None)
    lines = [f"🌐 <b>{_h(details['title'])}</b> en {ops.LANG_LABEL[lang]} — {len(rows)} de {total}:"]
    btns = []
    for i, r in enumerate(rows, 1):
        sure = "✅" if r["lang_sure"] else "❔"
        rej = " ❌" + _h(r["rejections"][0][:40]) if r["rejected"] and r["rejections"] else ""
        lines.append(f"<b>{i}.</b> {sure} {_h(r['title'][:70])}\n    {r['quality']} · {r['size_gb']} GB · 🌱{r['seeders']} · "
                     f"{_h(', '.join(r['langs']) or '?')} · {_h(r['indexer'])} · cf {r['score']}{rej}")
        btns.append(InlineKeyboardButton(f"⬇️ {i}", callback_data=f"grab:{key}:{i - 1}"))
    kb = InlineKeyboardMarkup([btns[:4], btns[4:]] if len(btns) > 4 else [btns])
    await wait.edit_text("\n".join(lines)[:4000] + "\n\n✅ = el indexer declara el idioma · ❔ = solo por el nombre",
                         parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- mensajes de texto
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    text = update.message.text.strip()
    if len(text) < 2:
        return await update.message.reply_text("Escribe al menos 2 letras 🙂")

    intent, arg = parse_intent(text)
    if intent and LEVEL[role] >= LEVEL["mod"]:
        return await handle_intent(update, ctx, role, label, intent, arg)
    if intent in ("restart", "covers_all", "status", "sessions") and role == "user":
        return await update.message.reply_text("Eso solo lo hacen los moderadores 🙂 Si un titulo tiene un problema, abre su tarjeta y toca 🚩 Reportar.")
    if intent in ("translate", "subs", "cover") and role == "user" and arg:
        text = arg  # el usuario busca el titulo y reporta desde la tarjeta

    wait = await update.message.reply_text("🔎 Buscando…")
    try:
        clean, year, season, want_es, lang, results, details, status, alts = await asyncio.to_thread(do_search, text)
    except Exception as e:
        log.exception("search %r", text)
        return await wait.edit_text(f"💥 Fallo la busqueda: {_h(e)}", parse_mode=ParseMode.HTML)
    log.info("search por %s: %r -> %r (%s) %s", label, text, clean, year or "",
             f"{results[0]['kind']}:{results[0]['tmdb']} {results[0]['title']} [{status['status']}]" if results else "sin resultados")
    log_request("search", update, text=text, clean=clean, top=(f"{results[0]['kind']}:{results[0]['tmdb']}" if results else None),
                status=(status or {}).get("status"), lang=lang)
    if not results:
        return await wait.edit_text(
            f"🤷 No encontre nada para «{_h(clean)}».\nPrueba con el titulo original (en ingles), sin tildes, o agrega el ano.",
            parse_mode=ParseMode.HTML)
    caption, markup, poster = build_card(details, status, results[1:], alts, season, want_es, role, lang)
    try:
        await wait.delete()
    except TelegramError:
        pass
    await send_card(update.message, caption, markup, poster)


async def handle_intent(update, ctx, role, label, intent, arg):
    msg = update.message
    if intent == "restart":
        if not can(role, "restart"):
            return await msg.reply_text("Reiniciar Emby es solo del admin.")
        return await ask_restart(msg)
    if intent == "status":
        return await cmd_sistema(update, ctx)
    if intent == "sessions":
        return await cmd_sesiones(update, ctx)
    if intent == "covers_all":
        if not can(role, "restart"):
            return await msg.reply_text("El barrido de caratulas es del admin; para un titulo concreto: «arregla la caratula de X».")
        wait = await msg.reply_text("🖼 Buscando items sin caratula y pidiendo refresco…")
        total, n, names = await run_long(_covers_all)
        return await wait.edit_text(f"🖼 Sin caratula: {total}; refresco pedido para {n}.\n" + "\n".join(f"• {_h(x)}" for x in names),
                                    parse_mode=ParseMode.HTML)
    # intenciones sobre un titulo: cover / translate / subs
    wait = await msg.reply_text(f"🔎 Buscando «{_h(arg)}»…", parse_mode=ParseMode.HTML)
    try:
        details, status = await asyncio.to_thread(find_one, arg)
    except Exception as e:
        log.exception("find_one %r", arg)
        return await wait.edit_text(f"💥 {_h(e)}", parse_mode=ParseMode.HTML)
    if not details:
        return await wait.edit_text(f"🤷 No encontre «{_h(arg)}».", parse_mode=ParseMode.HTML)
    if status["status"] != "emby":
        return await wait.edit_text(f"«{_h(details['title'])}» no esta en Emby ({status['status']}); primero hay que tenerla.", parse_mode=ParseMode.HTML)
    kind, tmdb = details["kind"], details["tmdb"]
    if intent == "cover":
        await wait.delete()
        return await do_cover(FakeQuery(msg), kind, tmdb)
    if intent == "subs":
        await wait.delete()
        return await subs_menu(FakeQuery(msg), kind, tmdb, role)
    if intent == "translate":
        await wait.edit_text(f"🤖 Traduciendo subs de <b>{_h(details['title'])}</b> con nuestro modelo (en→es)… "
                             f"{'una peli tarda 1-3 min' if kind == 'm' else 'una serie puede tardar bastante'}.", parse_mode=ParseMode.HTML)
        res = await run_long(_do_subs, "tr", kind, status["emby"], status.get("lib"), False)
        log_request("subs", update, action="tr", kind=kind, tmdb=tmdb, title=details["title"])
        return await msg.reply_text(f"🤖 <b>{_h(details['title'])}</b>\n{_h(res)}", parse_mode=ParseMode.HTML)


class FakeQuery:
    """Para reusar las acciones de boton desde un mensaje de texto."""
    def __init__(self, message):
        self.message = message

    async def answer(self, *a, **k):
        return None


async def ask_restart(msg):
    ss = await asyncio.to_thread(ops.emby_sessions)
    warn = f"\n⚠️ Hay {len(ss)} reproduccion(es) activa(s): " + ", ".join(_h(s["user"]) for s in ss[:5]) if ss else "\nNadie esta viendo nada."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Si, reiniciar", callback_data="emby:restart:yes"),
                                InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
    await msg.reply_text(f"¿Reinicio Emby?{warn}", parse_mode=ParseMode.HTML, reply_markup=kb)


# ---------------------------------------------------------------- botones
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    role, label = who(update)
    if not role:
        return await q.answer("Primero vincula tu cuenta de Emby con /vincular", show_alert=True)
    parts = q.data.split(":")
    action = parts[0]
    chat_id = update.effective_chat.id
    try:
        if action == "nop":
            await q.answer("Cancelado")
            return await q.edit_message_reply_markup(None)

        if action == "pick":
            await q.answer()
            kind, tmdb = parts[1], int(parts[2])
            details = await asyncio.to_thread(tmdb_details, kind, tmdb)
            status = await asyncio.to_thread(resolve, kind, tmdb, details.get("tvdb"), details.get("title"), details.get("year"))
            caption, markup, poster = build_card(details, status, [], {}, role=role)
            return await send_card(q.message, caption, markup, poster)

        if action == "add":
            kind, tmdb, mode = parts[1], int(parts[2]), (parts[3] if len(parts) > 3 else "all")
            lang = parts[4] if len(parts) > 4 and parts[4] != "-" else None
            if role == "user" and quota_used(update.effective_user.id) >= DAILY_QUOTA:
                return await q.answer(f"Ya usaste tus {DAILY_QUOTA} peticiones de hoy; manana se renuevan 🙂", show_alert=True)
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
            log_request("add", update, kind=kind, tmdb=tmdb, arr_id=obj["id"], title=title, shelf=shelf, mode=mode, lang=lang)
            scope = {"all": "", "last": " (solo la ultima temporada)"}.get(mode, f" (solo la temporada {mode})") if kind == "s" else ""
            left = "" if role != "user" else f"\nTe quedan {max(0, DAILY_QUOTA - quota_used(update.effective_user.id))} peticiones hoy."
            kb = None
            if lang and lang != "espanol" and can(role, "releases"):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🌐 Elegir copia en {ops.LANG_LABEL[lang]}", callback_data=f"rel:{kind}:{tmdb}:{lang}")]])
            await q.message.reply_text(
                f"➕ Pedida: <b>{_h(title)}</b>{_h(scope)}\n"
                f"📁 {shelf} · {_h(why)}\n"
                f"Ya esta buscando; te aviso cuando se pueda ver en Emby.{left}",
                parse_mode=ParseMode.HTML, reply_markup=kb)
            if role == "user":
                extra = ""
                skb = None
                if lang and lang != "espanol":
                    rid = new_report(update, kind, tmdb, title, "lang", lang)
                    extra = f" · quiere <b>{ops.LANG_LABEL[lang]}</b> (reporte #{rid})"
                    skb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🌐 Elegir copia en {ops.LANG_LABEL[lang]}", callback_data=f"rel:{kind}:{tmdb}:{lang}"),
                                                 InlineKeyboardButton("✅ Resuelto", callback_data=f"repok:{rid}")]])
                await notify_staff(ctx.application, f"➕ <b>{_h(label)}</b> pidio {'🎬' if kind == 'm' else '📺'} "
                                                    f"<b>{_h(title)}</b>{_h(scope)} → {shelf}{extra}", skb)
            return

        if action == "rs":
            await q.answer("Relanzando…")
            kind, arr_id = parts[1], int(parts[2])
            await asyncio.to_thread(do_research, kind, arr_id)
            _, obj = await asyncio.to_thread(_arrived, kind, arr_id)
            title = f"{obj.get('title')}{_year(obj.get('year'))}"
            remember_pending(chat_id, kind, arr_id, obj.get("tmdbId"), title, label)
            log_request("research", update, kind=kind, arr_id=arr_id, title=title)
            return await q.message.reply_text(f"🔁 Busqueda relanzada para <b>{_h(title)}</b>; te aviso si llega.", parse_mode=ParseMode.HTML)

        # ---- reportes (usuarios)
        if action == "rep":
            if parts[1] == "menu":
                await q.answer()
                kind, tmdb, lang = parts[2], parts[3], parts[4]
                rows = [[InlineKeyboardButton(v, callback_data=f"rep:{k}:{kind}:{tmdb}:{lang}")] for k, v in REPORT_TYPES.items()]
                return await q.message.reply_text("¿Que pasa con este titulo?", reply_markup=InlineKeyboardMarkup(rows))
            rtype, kind, tmdb, lang = parts[1], parts[2], int(parts[3]), (parts[4] if parts[4] != "-" else None)
            await q.answer("Anotado, gracias 🙏")
            details = await asyncio.to_thread(tmdb_details, kind, tmdb)
            title = f"{details['title']}{_year(details.get('year'))}"
            rid = new_report(update, kind, tmdb, title, rtype, lang)
            log_request("report", update, kind=kind, tmdb=tmdb, title=title, type=rtype, lang=lang)
            await q.message.reply_text(f"🚩 Reporte #{rid} enviado a los moderadores: {REPORT_TYPES[rtype]} — <b>{_h(title)}</b>. "
                                       f"Te aviso cuando lo resuelvan.", parse_mode=ParseMode.HTML)
            r = _load_json(REPORTS_FILE, {}).get(rid)
            return await notify_staff(ctx.application, _report_text(r), _report_kb(r))

        if action == "repok":
            if not can(role, "reports"):
                return await q.answer("Solo moderadores", show_alert=True)
            reps = _load_json(REPORTS_FILE, {})
            r = reps.get(parts[1])
            if not r:
                return await q.answer("Ese reporte ya no existe")
            r["open"], r["closed_by"], r["closed"] = False, label, time.time()
            _save_json(REPORTS_FILE, reps)
            await q.answer("Cerrado ✅")
            try:
                await q.edit_message_text(_report_text(r) + f"\n✅ resuelto por {_h(label)}", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
            try:
                await ctx.application.bot.send_message(r["chat_id"], f"✅ Tu reporte #{r['id']} sobre <b>{_h(r['title'])}</b> "
                                                                     f"({REPORT_TYPES.get(r['type'], r['type'])}) quedo resuelto.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

        # ---- de aqui en adelante: staff
        if not can(role, "subs"):
            return await q.answer("Eso es de moderadores 🙂", show_alert=True)

        if action == "sub":
            sub, kind, tmdb = parts[1], parts[2], int(parts[3])
            if sub == "menu":
                await q.answer()
                return await subs_menu(q, kind, tmdb, role)
            if sub == "trr" and not can(role, "subs_redo"):
                return await q.answer("Rehacer es solo del admin", show_alert=True)
            await q.answer("Voy…")
            details, status, item = await _need_emby(q, kind, tmdb)
            if not item:
                return
            what = {"dl": "🔎 Bazarr buscando es", "tr": "🤖 traduciendo con nuestro modelo (1-3 min por archivo)",
                    "trr": "🔁 rehaciendo es con nuestro modelo", "q": "🧹 encolando", "bz": "🌍 Bazarr traduciendo"}[sub]
            wait = await q.message.reply_text(f"{what} — <b>{_h(details['title'])}</b>…", parse_mode=ParseMode.HTML)
            res = await run_long(_do_subs, sub, kind, item, status.get("lib"), sub == "trr")
            log_request("subs", update, action=sub, kind=kind, tmdb=tmdb, title=details["title"])
            return await wait.edit_text(f"💬 <b>{_h(details['title'])}</b>\n{_h(res)}", parse_mode=ParseMode.HTML)

        if action == "cov":
            if parts[1] == "all":
                if not can(role, "restart"):
                    return await q.answer("El barrido es del admin", show_alert=True)
                await q.answer("Barriendo…")
                wait = await q.message.reply_text("🖼 Buscando items sin caratula y pidiendo refresco…")
                total, n, names = await run_long(_covers_all)
                return await wait.edit_text(f"🖼 Sin caratula: {total}; refresco pedido para {n}.\n" + "\n".join(f"• {_h(x)}" for x in names),
                                            parse_mode=ParseMode.HTML)
            await q.answer("Refrescando…")
            kind, tmdb = parts[1], int(parts[2])
            log_request("cover", update, kind=kind, tmdb=tmdb)
            return await do_cover(q, kind, tmdb)

        if action == "relq":
            await q.answer()
            kind, tmdb, lang = parts[1], parts[2], parts[3]
            if lang != "-":
                return await do_releases(q, kind, int(tmdb), lang, role)
            rows, cur = [], []
            for k, lbl in ops.LANG_LABEL.items():
                cur.append(InlineKeyboardButton(lbl, callback_data=f"rel:{kind}:{tmdb}:{k}"))
                if len(cur) == 3:
                    rows.append(cur)
                    cur = []
            if cur:
                rows.append(cur)
            return await q.message.reply_text("¿En que idioma buscas la copia?", reply_markup=InlineKeyboardMarkup(rows))

        if action == "rel":
            await q.answer("Buscando…")
            kind, tmdb, lang = parts[1], int(parts[2]), parts[3]
            return await do_releases(q, kind, tmdb, lang, role)

        if action == "grab":
            key, i = parts[1], int(parts[2])
            if key not in _rel_lists:
                return await q.answer("Esa lista ya caduco; vuelve a buscar", show_alert=True)
            kind, rows = _rel_lists[key]
            r = rows[i]
            await q.answer("Mandando a descargar…")
            await asyncio.to_thread(ops.grab, kind, r["guid"], r["indexerId"])
            log_request("grab", update, kind=kind, title=r["title"], indexer=r["indexer"])
            return await q.message.reply_text(f"⬇️ Enviada a {app_name(kind)}: <b>{_h(r['title'][:80])}</b>\n"
                                              f"Si al importar la rechaza (no es mejora), aparece en su cola como aviso.", parse_mode=ParseMode.HTML)

        if action == "sess":
            await q.answer()
            return await q.message.reply_text(await asyncio.to_thread(_sesiones_text), parse_mode=ParseMode.HTML)

        if action == "emby":
            if not can(role, "restart"):
                return await q.answer("Solo el admin", show_alert=True)
            if parts[1] == "restart" and len(parts) == 2:
                await q.answer()
                return await ask_restart(q.message)
            if parts[1] == "restart":
                await q.answer("Reiniciando…")
                await q.edit_message_reply_markup(None)
                log_request("emby_restart", update)
                secs = await run_long(ops.emby_restart, 180)
                return await q.message.reply_text("♻️ Emby volvio en %d s." % secs if secs >= 0 else "⚠️ Mande el reinicio pero Emby no respondio en 3 min; revisa.")

        if action == "task":
            if not can(role, "tasks") and parts[1] != "salud":
                return await q.answer("Solo el admin", show_alert=True)
            name = parts[1]
            if name.endswith("-borrar") and len(parts) == 2:
                await q.answer()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Si, borrar", callback_data=f"task:{name}:yes"),
                                            InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
                return await q.message.reply_text(f"¿Seguro? <b>{_h(ops.TASKS[name][0])}</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
            await q.answer("Corriendo…")
            wait = await q.message.reply_text(f"⏳ {_h(ops.TASKS[name][0])}…", parse_mode=ParseMode.HTML)
            log_request("task", update, task=name)
            ok, out = await run_long(ops.run_task, name)
            return await wait.edit_text(f"{'✅' if ok else '❌'} <b>{_h(ops.TASKS[name][0])}</b>\n<pre>{_h(out)}</pre>", parse_mode=ParseMode.HTML)

        await q.answer("Boton desconocido")
    except requests.HTTPError as e:
        body = ""
        try:
            j = e.response.json()
            body = "; ".join(x.get("errorMessage", "") for x in j) if isinstance(j, list) else str(j)[:200]
        except Exception:
            pass
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 HTTP {e.response.status_code}: {_h(body or e)}", parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 Error: {_h(e)}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------- arranque
async def post_init(app):
    global _emby_server_id
    try:
        _emby_server_id = (await asyncio.to_thread(_emby_get, "System/Info"))["Id"]
    except Exception:
        log.exception("Emby System/Info")
    PIDFILE.write_text(str(os.getpid()))
    app.bot_data["pending_task"] = asyncio.get_running_loop().create_task(pending_loop(app))
    base = [BotCommand("start", "Como funciona"), BotCommand("pendientes", "Lo que pediste y aun no llega"),
            BotCommand("vincular", "Vincular tu cuenta de Emby"), BotCommand("id", "Tu id de Telegram")]
    mod = base + [BotCommand("sistema", "Salud del pipeline y Emby"), BotCommand("sesiones", "Quien esta viendo"),
                  BotCommand("reportes", "Problemas abiertos"), BotCommand("usuarios", "Vinculados y roles")]
    adm = mod + [BotCommand("emby", "Reiniciar, caratulas, salud"), BotCommand("tareas", "Scripts del pipeline"),
                 BotCommand("log", "Cola de un log"), BotCommand("rol", "Cambiar rol"), BotCommand("desvincular", "Quitar usuario")]
    try:
        await app.bot.set_my_commands(base)
        for tid in staff_ids():
            try:
                await app.bot.set_my_commands(adm if role_of(tid) == "admin" else mod, scope=BotCommandScopeChat(tid))
            except Exception:
                log.warning("set_my_commands scope %s fallo", tid)
    except Exception:
        log.warning("set_my_commands fallo")
    log.info("listo v3; admins=%s staff=%s usuarios=%d cupo=%d/dia", sorted(ADMINS) or "NINGUNO", sorted(staff_ids()), len(load_users()), DAILY_QUOTA)


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
    app.add_handler(CommandHandler("rol", cmd_role))
    app.add_handler(CommandHandler("pendientes", cmd_pending))
    app.add_handler(CommandHandler(["sistema", "estado", "salud"], cmd_sistema))
    app.add_handler(CommandHandler("sesiones", cmd_sesiones))
    app.add_handler(CommandHandler("reportes", cmd_reportes))
    app.add_handler(CommandHandler("emby", cmd_emby))
    app.add_handler(CommandHandler("tareas", cmd_tareas))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
