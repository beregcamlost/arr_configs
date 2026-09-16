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
from tgbot import pipeflix_nlu as nlu

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
    # v4
    "recent": "user", "menu": "user", "season": "user", "subs_self": "user",
    "queue": "mod", "queue_rm": "mod", "wanted": "mod", "stats": "mod", "releases_any": "mod",
    "torrents": "admin", "delete": "admin",
}
AUTO_SUBS_MAX_EPS = 10        # episodios por titulo en los arreglos automaticos (el resto lo hace el cron)


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


# ---------------------------------------------------------------- 1b. intenciones: tgbot/pipeflix_nlu.py (reglas + Claude opcional)


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


REPORT_TYPES = {"nosubs": "💬 sin subtitulos en espanol", "subsbad": "⏱ subtitulos mal (desincronizados / idioma / traduccion)",
                "noaudio": "🔊 sin audio en espanol", "noplay": "▶️ no reproduce / se traba",
                "cover": "🖼 caratula o info mal", "lang": "🌐 la quiero en otro idioma", "quality": "📉 se ve mal / mejor calidad",
                "other": "❓ otra cosa"}


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
def remember_pending(chat_id, kind, arr_id, tmdb, title, who_label, season=None, have=0):
    p = _load_json(PENDING_FILE, {})
    key = f"{kind}:{arr_id}" + (f":s{season}" if season is not None else "")
    p[key] = {"chat_id": chat_id, "kind": kind, "arr_id": arr_id, "tmdb": tmdb, "title": title,
              "who": who_label, "since": time.time(), "stage": "arr"}
    if season is not None:
        p[key].update(season=season, have=have)
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
                    if req.get("season") is not None:
                        # temporada pedida de una serie que ya existe: contamos episodios con archivo
                        total, have, _m = await asyncio.to_thread(ops.sonarr_season_status, req["arr_id"], req["season"])
                        if have > req.get("have", 0):
                            done = bool(total) and have >= total
                            await app.bot.send_message(chat, f"📺 <b>{_h(title)}</b>: {'temporada completa, ' if done else 'ya llegaron '}"
                                                             f"{have}/{total} episodios{' 🍿' if done else '; sigo pendiente del resto.'}",
                                                       parse_mode=ParseMode.HTML)
                            req["have"], changed = have, True
                            drop = done
                    elif req.get("stage", "arr") == "arr":
                        ok, obj = await asyncio.to_thread(_arrived, kind, req["arr_id"])
                        if ok:
                            req["stage"], req["downloaded_at"] = "emby", time.time()
                            changed = True
                    if req.get("season") is None and req.get("stage") == "emby":
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

HELP_USER = ("Escribeme lo que quieras en lenguaje normal 🙂\n\n"
             "🎬 <b>Ver algo</b>: <i>merlina</i> · <i>dune 2021</i> · <i>la sirenita en frances</i> → te digo si esta en Emby; "
             "si no, la pides con un toque y te aviso cuando se pueda ver.\n"
             "📺 <b>Temporadas</b>: <i>temporada 3 de dark</i> · <i>pide la temporada 5 de stranger things</i>\n"
             "💬 <b>Subtitulos</b>: <i>arregla los subs de dune</i> · <i>the boys no tiene subtitulos</i> → los busco y si no hay "
             "los traduzco yo, y te aviso. <i>los subs de moana estan desincronizados</i> → lo revisa un moderador.\n"
             "🖼 <b>Caratula</b>: <i>la caratula de moana esta mal</i>\n"
             "🆕 <i>que llego hoy</i> · ⏳ <i>que pedi</i> · 🧭 <i>menu</i>\n\n"
             "Tambien puedes abrir la tarjeta de un titulo y tocar <b>🚩 Reportar un problema</b>.\n"
             "/recientes — lo nuevo · /pendientes — lo que pediste · /menu — botones · /id — tu id")

HELP_MOD = ("\n\n<b>Moderador</b> — sin cupo. En cada tarjeta: 💬 Subs (⚡ automatico, Bazarr, nuestro modelo, sincronizar), "
            "🖼 Caratula, 🌐 Otra copia.\n"
            "Escribiendo: <i>que esta bajando</i> · <i>cuanto falta a superman</i> · <i>que llego hoy</i> · "
            "<i>que falta de subs</i> · <i>busca mejor calidad de barbie</i> · <i>dune en frances</i> · "
            "<i>traduce los subs de dune</i> · <i>arregla la caratula de moana</i> · <i>quien esta viendo</i> · <i>estado</i> · <i>cuantas pelis hay</i>\n"
            "/cola — descargas (quitar, vetar y buscar otra) · /recientes [horas] · /faltantes — sin subs es · /stats\n"
            "/sistema · /sesiones · /reportes · /usuarios · /pendientes (de todos)")

HELP_ADMIN = ("\n\n<b>Admin</b>\n/torrents — Transmission (estancados, borrar) · /borrar &lt;titulo&gt; — quitar del servidor con archivos\n"
              "/emby — reiniciar, caratulas faltantes, salud · /tareas — scripts del pipeline · /log &lt;nombre&gt;\n"
              "/rol &lt;id o usuario&gt; admin|mod|usuario · /desvincular &lt;id o usuario&gt;\n"
              "Escribiendo: <i>reinicia emby</i> · <i>caratulas faltantes</i> · <i>borra scary movie 2</i> · <i>torrents</i> · "
              "<i>log de faststart</i> · <i>corre salud</i>"
              + ("\n🧠 Claude activo para frases libres." if nlu.llm_available() else
                 "\n💡 Sin ANTHROPIC_API_KEY en .env: entiendo las frases de arriba (reglas); con clave entiendo cualquier redaccion."))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.effective_message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    text = f"🎬 <b>PIPEFLIX</b> — hola, {_h(label)} ({ROLE_LABEL[role]})\n\n" + HELP_USER
    if role == "user":
        text += f"\n\nTienes {max(0, DAILY_QUOTA - quota_used(update.effective_user.id))} peticiones disponibles hoy."
    if LEVEL[role] >= LEVEL["mod"]:
        text += HELP_MOD
    if role == "admin":
        text += HELP_ADMIN
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Tu id: <code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML)


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
    await update.effective_message.reply_text("👥 <b>Usuarios</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


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
        return await update.effective_message.reply_text("Uso: <code>/rol &lt;id o usuario&gt; admin|mod|usuario</code>", parse_mode=ParseMode.HTML)
    tid = _find_tg_id(args[0])
    if not tid:
        return await update.effective_message.reply_text("No encuentro ese usuario (usa el id de Telegram o el usuario de Emby vinculado).")
    new = {"moderador": "mod", "user": "usuario"}.get(args[1].lower(), args[1].lower())
    roles = load_roles()
    if new == "usuario":
        roles.pop(tid, None)
    else:
        roles[tid] = new
    _save_json(ROLES_FILE, roles)
    log_request("role", update, target=tid, role=new)
    await update.effective_message.reply_text(f"✅ <code>{tid}</code> ahora es <b>{new}</b>.", parse_mode=ParseMode.HTML)
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
    await update.effective_message.reply_text(f"Desvinculados: {len(gone)}")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not role:
        return await update.effective_message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    p = _load_json(PENDING_FILE, {})
    all_ = can(role, "pending_all")
    mine = [r for r in p.values() if all_ or r["chat_id"] == update.effective_chat.id]
    if not mine:
        return await update.effective_message.reply_text("Nada pendiente 👌")
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
    await update.effective_message.reply_text("⏳ <b>Pendientes</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


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
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


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
    await update.effective_message.reply_text(await asyncio.to_thread(_sesiones_text), parse_mode=ParseMode.HTML)


async def cmd_emby(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "restart"):
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("♻️ Reiniciar Emby", callback_data="emby:restart")],
                               [InlineKeyboardButton("🖼 Caratulas faltantes", callback_data="cov:all"),
                                InlineKeyboardButton("🩺 Salud", callback_data="task:salud")],
                               [InlineKeyboardButton("👀 Sesiones", callback_data="sess"),
                                InlineKeyboardButton("🗂 Estante idioma", callback_data="task:estante")]])
    await update.effective_message.reply_text("🎛 <b>Emby</b>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_tareas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "tasks"):
        return
    rows = [[InlineKeyboardButton(lbl, callback_data=f"task:{name}")] for name, (lbl, _a, _t) in ops.TASKS.items()]
    await update.effective_message.reply_text("🧰 <b>Tareas del pipeline</b> (corren en mubuntu; las marcadas BORRAR piden confirmacion)",
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
    await update.effective_message.reply_text(f"📄 <b>{_h(name)}</b>\n<pre>{_h(text)[-3600:]}</pre>", parse_mode=ParseMode.HTML)


async def cmd_reportes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "reports"):
        return
    reps = [r for r in _load_json(REPORTS_FILE, {}).values() if r.get("open")]
    if not reps:
        return await update.effective_message.reply_text("Sin reportes abiertos 👌")
    for r in sorted(reps, key=lambda r: r["since"])[:15]:
        await update.effective_message.reply_text(_report_text(r), parse_mode=ParseMode.HTML, reply_markup=_report_kb(r))


def _report_text(r):
    lang = f" ({ops.LANG_LABEL.get(r.get('lang'), r.get('lang'))})" if r.get("lang") else ""
    return (f"🚩 <b>Reporte #{r['id']}</b> · {REPORT_TYPES.get(r['type'], r['type'])}{lang}\n"
            f"{'🎬' if r['kind'] == 'm' else '📺'} <b>{_h(r['title'])}</b> · por {_h(r['by'])} · hace {_ago(r['since'])}")


def _report_kb(r):
    k, t = r["kind"], r["tmdb"]
    row, row2 = [], []
    if r["type"] in ("nosubs", "subsbad", "other", "noplay"):
        row.append(InlineKeyboardButton("💬 Subs", callback_data=f"sub:menu:{k}:{t}"))
    if r["type"] == "nosubs":
        row.append(InlineKeyboardButton("⚡ Auto", callback_data=f"sub:auto:{k}:{t}"))
    if r["type"] == "subsbad":
        row.append(InlineKeyboardButton("⏱ Sincronizar", callback_data=f"sub:sync:{k}:{t}"))
        row2.append(InlineKeyboardButton("🔁 Rehacer es", callback_data=f"sub:trr:{k}:{t}"))
    if r["type"] in ("cover", "other"):
        row.append(InlineKeyboardButton("🖼 Caratula", callback_data=f"cov:{k}:{t}"))
    if r["type"] in ("lang", "noaudio", "noplay", "other"):
        row.append(InlineKeyboardButton("🌐 Otra copia", callback_data=f"relq:{k}:{t}:{r.get('lang') or '-'}"))
    if r["type"] in ("quality", "noplay"):
        row2.append(InlineKeyboardButton("🏆 Mejor copia", callback_data=f"rela:{k}:{t}"))
    rows = [row] + ([row2] if row2 else []) + [[InlineKeyboardButton("✅ Resuelto", callback_data=f"repok:{r['id']}")]]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------- v4: ver todo desde el bot (cola, recientes, faltantes, torrents, stats, menu)
_q_lists = {}   # clave corta -> filas de la cola (para los botones)
_t_lists = {}   # clave corta -> torrents


def _stash(store, rows):
    _rel_seq[0] += 1
    key = str(_rel_seq[0])
    store[key] = rows
    for k in list(store)[:-20]:
        store.pop(k, None)
    return key


def _cola_text(filter_title=None):
    rows = ops.arr_queue()
    if filter_title:
        f = _fold(filter_title)
        rows = [r for r in rows if f in _fold(r["title"]) or f in _fold(r["release"])]
    if not rows:
        return "📥 Nada en la cola" + (f" que se parezca a «{_h(filter_title)}»" if filter_title else "") + " 👌", \
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Actualizar", callback_data="q:refresh")]])
    key = _stash(_q_lists, rows)
    lines = [f"📥 <b>Cola</b> — {len(rows)} descarga(s)"]
    btns = []
    for i, r in enumerate(rows[:25], 1):
        ico = "🎬" if r["kind"] == "m" else "📺"
        st = {"downloading": "⬇️", "queued": "🕓", "paused": "⏸", "completed": "✅", "warning": "⚠️", "failed": "❌", "delay": "⏳"}.get(r["status"], r["status"])
        eps = f" {r['eps'][0]}…{r['eps'][-1]}" if len(r["eps"]) > 1 else (f" {r['eps'][0]}" if r["eps"] else "")
        extra = f" · {_h(r['tstate'])}" if r["tstate"] not in ("downloading", "") else ""
        lines.append(f"<b>{i}.</b> {ico} <b>{_h(r['title'])}</b>{_h(eps)} — {st} {r['pct']}% · {r['size_gb']} GB"
                     + (f" · ⏱ {_h(r['eta'])}" if r["eta"] else "") + extra)
        if r["msgs"]:
            lines.append("    ⚠️ " + _h(" | ".join(r["msgs"])[:160]))
        if r["ids"]:
            btns.append(InlineKeyboardButton(f"🔁 {i}", callback_data=f"q:bl:{key}:{i - 1}"))
            btns.append(InlineKeyboardButton(f"🗑 {i}", callback_data=f"q:rm:{key}:{i - 1}"))
    lines.append("\n🔁 = quitar, vetar esa copia y buscar otra · 🗑 = solo quitar")
    kb_rows = [btns[j:j + 6] for j in range(0, len(btns), 6)]
    kb_rows.append([InlineKeyboardButton("🔄 Actualizar", callback_data="q:refresh"), InlineKeyboardButton("🧲 Torrents", callback_data="tor:list")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(kb_rows)


def _recientes_text(hours, staff=False):
    rows = ops.emby_recent(hours)
    kb = [[InlineKeyboardButton("24 h", callback_data="rec:24"), InlineKeyboardButton("48 h", callback_data="rec:48"),
           InlineKeyboardButton("7 dias", callback_data="rec:168")]]
    if not rows:
        return f"🆕 Nada nuevo en las ultimas {hours} h.", InlineKeyboardMarkup(kb)
    lines = [f"🆕 <b>Nuevo en las ultimas {hours} h</b> — {len(rows)} titulo(s)"]
    no_es = 0
    for r in rows[:30]:
        ico = "🎬" if r["kind"] == "m" else "📺"
        det = f" · {r['n']} ep" if r["kind"] == "s" else ""
        flag = "✅ es" if not r["no_es"] else ("❌ sin es" + (f" ({r['no_es']} ep)" if r["kind"] == "s" and r["no_es"] != r["n"] else ""))
        lines.append(f"• {ico} <b>{_h(r['name'])}</b>{det} — {flag} · hace {_ago(r['ts'])}")
        no_es += 1 if r["no_es"] else 0
    if staff and no_es:
        kb.append([InlineKeyboardButton(f"⚡ Arreglar subs de los {no_es} sin es (max 8)", callback_data=f"wnt:fixrecent:{hours}")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(kb)


def _faltantes_text():
    movies, series, tm, te = ops.bazarr_wanted()
    lines = [f"💬 <b>Sin subs en espanol (segun Bazarr)</b> — {tm} pelis · {te} episodios"]
    for m in movies[:15]:
        lines.append(f"• 🎬 {_h(m['title'])} (falta {', '.join(m['missing']) or 'es'})")
    for s, eps in list(series.items())[:15]:
        lines.append(f"• 📺 {_h(s)}: {len(eps)} ep ({', '.join(str(e[0]) for e in eps[:6])}{'…' if len(eps) > 6 else ''})")
    if not movies and not series:
        lines.append("• Bazarr no tiene nada pendiente 👌")
    hist = ops.bazarr_history(5)
    if hist:
        lines.append("\n🕘 <b>Ultimo de Bazarr</b>\n" + "\n".join(f"• {_h(h)}" for h in hist[:6]))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Bazarr: buscar todo lo que falta", callback_data="wnt:search")],
                               [InlineKeyboardButton("🤖 Traducir pelis faltantes con nuestro modelo (max 8)", callback_data="wnt:translate")],
                               [InlineKeyboardButton("📄 Estado del traductor", callback_data="task:traductor"),
                                InlineKeyboardButton("🔄 Actualizar", callback_data="wnt:list")]])
    return "\n".join(lines)[:4000], kb


def _translate_wanted(limit=8):
    """Hilo largo: las pelis que Bazarr no pudo resolver -> nuestro modelo."""
    movies, _s, _tm, _te = ops.bazarr_wanted()
    idx = emby_index()
    lib_m, _ = library_ids()
    out, n = [], 0
    for m in movies:
        if n >= limit:
            break
        try:
            mv = ops.arr_movie(m["radarrId"])
        except Exception as e:
            out.append(f"⚠️ {m['title']}: Radarr {e}")
            continue
        item = idx.get(("m", "tmdb", mv.get("tmdbId")))
        if not item:
            out.append(f"⚠️ {m['title']}: no esta en Emby")
            continue
        n += 1
        ok, txt = _auto_subs("m", item, lib_m.get(mv.get("tmdbId")), skip_bazarr=True)
        out.append(f"{'✅' if ok else '⚠️'} {m['title']}: {txt.splitlines()[0]}")
    return "\n".join(out) or "Bazarr no tiene pelis pendientes"


def _fix_recent(hours, limit=8):
    """Hilo largo: lo recien llegado sin es -> Bazarr y luego nuestro modelo."""
    rows = [r for r in ops.emby_recent(hours) if r["no_es"]][:limit]
    lib_m, lib_s = library_ids()
    out = []
    for r in rows:
        it = ops.emby_item(r["id"]) or {"Id": r["id"]}
        try:
            tmdb = int((it.get("ProviderIds") or {}).get("Tmdb") or 0)
        except ValueError:
            tmdb = 0
        lib = (lib_m if r["kind"] == "m" else lib_s).get(tmdb)
        ok, txt = _auto_subs(r["kind"], it, lib)
        out.append(f"{'✅' if ok else '⚠️'} {r['name']}: {txt.splitlines()[0]}")
    return "\n".join(out) or "nada que arreglar"


def _torrents_text():
    rows = ops.transmission_list()
    if not rows:
        return "🧲 Transmission esta vacio.", None
    key = _stash(_t_lists, rows)
    bad = sum(1 for r in rows if r["err"] or r["stalled"])
    lines = [f"🧲 <b>Torrents</b> — {len(rows)} · bajando {sum(1 for r in rows if r['pct'] < 100)} · "
             f"completos {sum(1 for r in rows if r['pct'] == 100)} · ⚠️ {bad}"]
    btns = []
    for i, r in enumerate(rows[:25], 1):
        flag = "❌" if r["err"] else ("🐌" if r["stalled"] else ("✅" if r["pct"] == 100 else "⬇️"))
        eta = f" · ⏱ {r['eta_min']} min" if r["eta_min"] else ""
        lines.append(f"<b>{i}.</b> {flag} {_h(r['name'][:60])}\n    {_h(r['cat'] or 'manual')} · {r['pct']}% · {r['size_gb']} GB · "
                     f"{r['rate_mb']} MB/s{eta} · {r['state']} · {r['age_d']} d" + (f" · ⚠️ {_h(r['err'][:60])}" if r["err"] else ""))
        btns.append(InlineKeyboardButton(f"🗑 {i}", callback_data=f"tor:rm:{key}:{i - 1}"))
    lines.append("\n🐌 = estancado · 🗑 = borrar con sus datos (pide confirmacion)")
    kb_rows = [btns[j:j + 6] for j in range(0, len(btns), 6)]
    kb_rows.append([InlineKeyboardButton("🔄 Actualizar", callback_data="tor:list"),
                    InlineKeyboardButton("🧹 Limpiar importados (simulacro)", callback_data="task:torrents")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(kb_rows)


def _stats_text():
    c = ops.emby_counts()
    tot, a7, a30, users = ops.emby_users_activity()
    p = _load_json(PENDING_FILE, {})
    reps = [r for r in _load_json(REPORTS_FILE, {}).values() if r.get("open")]
    since, ev = time.time() - 7 * 86400, {}
    try:
        with REQUESTS_LOG.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) > since:
                    ev[r.get("event")] = ev.get(r.get("event"), 0) + 1
    except FileNotFoundError:
        pass
    lines = ["📊 <b>PIPEFLIX en numeros</b>",
             f"🎬 {c['Movie']} pelis · 📺 {c['Series']} series · {c['Episode']} episodios",
             f"👥 {tot} usuarios de Emby · activos 7 d: {a7} · 30 d: {a30} · vinculados a Telegram: {len(load_users())}",
             f"⏳ pendientes: {len(p)} · 🚩 reportes abiertos: {len(reps)}",
             f"📈 Ultimos 7 d: {ev.get('search', 0)} busquedas · {ev.get('add', 0)} peticiones · "
             f"{ev.get('sub_req', 0) + ev.get('subs', 0)} acciones de subs · {ev.get('report', 0)} reportes"]
    for pth, tb, pct in ops.disk():
        lines.append(f"💽 {pth}: {tb} TB libres ({pct}%)")
    lines.append("\n🕒 <b>Ultima actividad</b>: " + ", ".join(f"{_h(n)} ({d} d)" if d is not None else f"{_h(n)} (nunca)" for n, d in users[:14]))
    return "\n".join(lines)


def _menu_kb(role):
    rows = [[InlineKeyboardButton("🆕 Recientes", callback_data="rec:48"), InlineKeyboardButton("⏳ Pendientes", callback_data="menu:pending")]]
    if LEVEL[role] >= LEVEL["mod"]:
        rows += [[InlineKeyboardButton("📥 Cola", callback_data="q:refresh"), InlineKeyboardButton("💬 Subs faltantes", callback_data="wnt:list"),
                  InlineKeyboardButton("🚩 Reportes", callback_data="menu:reports")],
                 [InlineKeyboardButton("🩺 Sistema", callback_data="menu:system"), InlineKeyboardButton("👀 Sesiones", callback_data="sess"),
                  InlineKeyboardButton("📊 Stats", callback_data="menu:stats")]]
    if role == "admin":
        rows += [[InlineKeyboardButton("🧲 Torrents", callback_data="tor:list"), InlineKeyboardButton("🧰 Tareas", callback_data="menu:tasks"),
                  InlineKeyboardButton("🎛 Emby", callback_data="menu:emby")]]
    return InlineKeyboardMarkup(rows)


async def cmd_cola(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "queue"):
        return
    text, kb = await asyncio.to_thread(_cola_text, " ".join(ctx.args or []) or None)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_recientes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not role:
        return await update.effective_message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    hours = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 48
    text, kb = await asyncio.to_thread(_recientes_text, min(hours, 720), can(role, "wanted"))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_faltantes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "wanted"):
        return
    text, kb = await asyncio.to_thread(_faltantes_text)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_torrents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "torrents"):
        return
    text, kb = await asyncio.to_thread(_torrents_text)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, _ = who(update)
    if not can(role, "stats"):
        return
    await update.effective_message.reply_text(await asyncio.to_thread(_stats_text), parse_mode=ParseMode.HTML)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not role:
        return await update.effective_message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    await update.effective_message.reply_text(f"🧭 <b>Menu</b> ({ROLE_LABEL[role]}) — o escribeme lo que necesites.", parse_mode=ParseMode.HTML,
                                              reply_markup=_menu_kb(role))


async def cmd_borrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role, label = who(update)
    if not can(role, "delete"):
        return
    title = " ".join(ctx.args or []).strip()
    if not title:
        return await update.effective_message.reply_text("Uso: <code>/borrar titulo</code>", parse_mode=ParseMode.HTML)
    await handle_intent(update, ctx, role, label, dict(nlu.EMPTY, intent="delete", title=title))


# ---------------------------------------------------------------- v4: autoservicio de subs (usuarios) y temporadas
async def subs_self_service(update, ctx, details, status, bad=False):
    """Usuario normal: «arregla los subs de X». Ticket para el staff y, si X esta en Emby y no es «estan mal»,
    intento automatico (Bazarr → nuestro modelo) avisandole al terminar."""
    msg, app = update.effective_message, ctx.application
    role, label = who(update)
    kind, tmdb = details["kind"], details["tmdb"]
    title = f"{details['title']}{_year(details.get('year'))}"
    if status["status"] != "emby":
        caption, markup, poster = build_card(details, status, [], {}, role=role)
        await msg.reply_text(f"«{_h(title)}» todavia no esta en Emby, asi que no hay subs que arreglar 🙂 Si quieres, pidela aqui:",
                             parse_mode=ParseMode.HTML)
        return await send_card(msg, caption, markup, poster)
    if role == "user" and quota_used(update.effective_user.id) >= DAILY_QUOTA:
        return await msg.reply_text(f"Ya usaste tus {DAILY_QUOTA} peticiones de hoy; manana se renuevan 🙂")
    rid = new_report(update, kind, tmdb, title, "subsbad" if bad else "nosubs")
    log_request("sub_req", update, kind=kind, tmdb=tmdb, title=title, bad=bad)
    r = _load_json(REPORTS_FILE, {}).get(rid)
    if bad:
        await msg.reply_text(f"🚩 Anotado (#{rid}): subtitulos de <b>{_h(title)}</b> con problemas. Un moderador los revisa y te aviso.",
                             parse_mode=ParseMode.HTML)
        return await notify_staff(app, _report_text(r), _report_kb(r))
    await msg.reply_text(f"🔧 Voy a intentar arreglar los subs de <b>{_h(title)}</b> yo solo: primero busco en Bazarr y, si no hay, "
                         f"los traduzco con nuestro modelo. Te aviso aqui mismo (puede tardar unos minutos"
                         f"{'; en series hago hasta %d episodios ahora y el resto de noche' % AUTO_SUBS_MAX_EPS if kind == 's' else ''}).",
                         parse_mode=ParseMode.HTML)
    await notify_staff(app, f"🔧 <b>{_h(label)}</b> pidio subs de <b>{_h(title)}</b> (#{rid}); intentando automatico…")
    item, lib, chat_id = status["emby"], status.get("lib"), update.effective_chat.id

    async def job():
        try:
            ok, txt = await run_long(_auto_subs, kind, item, lib)
        except Exception as e:
            log.exception("auto subs %s", title)
            ok, txt = False, f"error: {e}"
        reps = _load_json(REPORTS_FILE, {})
        rr = reps.get(rid)
        if ok:
            if rr:
                rr["open"], rr["closed_by"], rr["closed"] = False, "auto", time.time()
                _save_json(REPORTS_FILE, reps)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🍿 Abrir en Emby", url=emby_link(item["Id"]))]])
            await app.bot.send_message(chat_id, f"✅ Listo: subs de <b>{_h(title)}</b> — {_h(txt.splitlines()[0])}.\n"
                                                f"Si no aparecen, cierra y abre la peli (Emby tarda un minuto en verlos).",
                                       parse_mode=ParseMode.HTML, reply_markup=kb)
            await notify_staff(app, f"✅ Auto #{rid} <b>{_h(title)}</b>: {_h(txt.splitlines()[0])}")
        else:
            await app.bot.send_message(chat_id, f"😕 No pude arreglar solo los subs de <b>{_h(title)}</b> ({_h(txt.splitlines()[0][:120])}). "
                                                f"Queda el reporte #{rid} para un moderador; te aviso cuando lo resuelvan.", parse_mode=ParseMode.HTML)
            if rr:
                await notify_staff(app, f"⚠️ Auto #{rid} fallo: {_h(txt[:300])}\n" + _report_text(rr), _report_kb(rr))

    app.create_task(job())


async def season_flow(update, ctx, details, status, season, role, label):
    """Temporada N de una serie que YA esta (Emby o Sonarr). -> True si respondio; False = que siga la tarjeta normal."""
    msg = update.effective_message
    if details["kind"] != "s" or status["status"] == "missing":
        return False
    lib = status.get("lib")
    title = details["title"]
    if not lib:
        await msg.reply_text(f"«{_h(title)}» esta en Emby pero no en Sonarr; no puedo pedir temporadas sueltas.", parse_mode=ParseMode.HTML)
        return True
    total, have, mon = await asyncio.to_thread(ops.sonarr_season_status, lib["arr_id"], season)
    if total == 0:
        await msg.reply_text(f"📺 Sonarr no conoce la temporada {season} de <b>{_h(title)}</b> (¿todavia no existe o no esta anunciada?).",
                             parse_mode=ParseMode.HTML)
        return True
    if have >= total:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🍿 Abrir en Emby", url=emby_link(status["emby"]["Id"]))]]) if status.get("emby") else None
        await msg.reply_text(f"✅ <b>{_h(title)}</b> — temporada {season} ya esta completa ({have} episodios).", parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"➕ {'Volver a buscar' if mon else 'Pedir'} temporada {season}",
                                                     callback_data=f"sea:{details['tmdb']}:{lib['arr_id']}:{season}:{have}")]])
    await msg.reply_text(f"📺 <b>{_h(title)}</b> — temporada {season}: {have}/{total} episodios"
                         f"{' (ya monitoreada, Sonarr la esta buscando)' if mon else ' (no monitoreada)'}.", parse_mode=ParseMode.HTML, reply_markup=kb)
    return True


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
    rows = [[InlineKeyboardButton("⚡ Automatico: Bazarr y si no, nuestro modelo", callback_data=f"sub:auto:{kind}:{tmdb}")],
            [InlineKeyboardButton("🔎 Bazarr: bajar es", callback_data=f"sub:dl:{kind}:{tmdb}"),
             InlineKeyboardButton("🤖 Traducir en→es (nuestro modelo)", callback_data=f"sub:tr:{kind}:{tmdb}")],
            [InlineKeyboardButton("⏱ Sincronizar es (Bazarr)", callback_data=f"sub:sync:{kind}:{tmdb}"),
             InlineKeyboardButton("🌍 Bazarr: traducir de otro idioma", callback_data=f"sub:bz:{kind}:{tmdb}")],
            [InlineKeyboardButton("🧹 Encolar mantenimiento (01:00Z)", callback_data=f"sub:q:{kind}:{tmdb}")]]
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
    elif action == "auto":
        ok, txt = _auto_subs(kind, item, lib)
        out.append(("✅ " if ok else "⚠️ ") + txt)
    elif action == "sync":
        if not lib:
            return "no esta en Radarr/Sonarr; Bazarr no puede sincronizarlo"
        eps = {} if kind == "m" else {os.path.basename(e.get("path") or ""): e for e in ops.bazarr_episodes(lib["arr_id"])}
        n = 0
        for ep, path in vids[:SERIES_SUB_MAX]:
            srts = ops.sidecars(path).get("es") or []
            if not srts:
                continue
            mid = lib["arr_id"] if kind == "m" else (eps.get(os.path.basename(path)) or {}).get("sonarrEpisodeId")
            if mid is None:
                continue
            try:
                ops.bazarr_sync(kind, mid, srts[0], "es")
                n += 1
            except Exception as ex:
                out.append(f"{os.path.basename(path)[:40]}: {ex}")
        out.insert(0, f"⏱ Bazarr re-sincronizo {n} subtitulo(s) es contra el audio (ffsubsync)")
    return "\n".join(out) or "nada que hacer"


def _auto_subs(kind, item, lib, max_eps=AUTO_SUBS_MAX_EPS, skip_bazarr=False):
    """Hilo largo. Para lo que no tenga es: Bazarr primero y, lo que quede, nuestro modelo. -> (ok, resumen)"""
    vids = video_paths(kind, item)
    if not vids:
        return False, "no encuentro el archivo en disco"

    def has_es(it, path):
        a, s = ops.streams_summary(it)
        return "es" in ops.sidecars(path) or bool(s & ES_LANGS) or bool(a & ES_LANGS)

    targets = [(it, p) for it, p in vids if not has_es(it, p)]
    if not targets:
        return True, "ya tenia subtitulos (o audio) en espanol; si se ven mal, reportalo como «subtitulos mal»"
    got_bz, got_tr, fails = 0, 0, []
    if lib and not skip_bazarr:
        try:
            if kind == "m":
                ops.bazarr_download_movie(lib["arr_id"], "es")
            else:
                eps = {os.path.basename(e.get("path") or ""): e for e in ops.bazarr_episodes(lib["arr_id"])}
                for it, p in targets[:max_eps]:
                    e = eps.get(os.path.basename(p))
                    if e and any(m.get("code2") == "es" for m in e.get("missing_subtitles") or []):
                        try:
                            ops.bazarr_download_episode(lib["arr_id"], e["sonarrEpisodeId"], "es")
                        except Exception as ex:
                            fails.append(f"bazarr {os.path.basename(p)[:40]}: {ex}")
        except Exception as ex:
            fails.append(f"bazarr: {ex}")
        still = []
        for it, p in targets:
            if "es" in ops.sidecars(p):
                got_bz += 1
            else:
                still.append((it, p))
        targets = still
    for it, p in targets[:max_eps]:
        ok, msg = ops.translate_with_model(p)
        if ok:
            got_tr += 1
        else:
            fails.append(f"{os.path.basename(p)[:40]}: {(msg.splitlines() or ['?'])[-1][:80]}")
    left = len(targets) - got_tr
    if left > 0 and len(targets) > max_eps:
        try:
            ops.sqm_enqueue([p for _, p in targets[max_eps:]][:200])
        except Exception:
            pass
    if got_bz or got_tr:
        for d in {os.path.dirname(p) for _, p in vids}:
            try:
                ops.emby_scan_folder(d)
            except Exception:
                pass
    parts = []
    if got_bz:
        parts.append(f"Bazarr encontro {got_bz}")
    if got_tr:
        parts.append(f"nuestro modelo tradujo {got_tr}")
    if left > 0:
        parts.append(f"quedan {left} sin resolver" + (" (encolados para esta noche)" if len(targets) > max_eps else ""))
    txt = ("; ".join(parts) or "nada que hacer") + ("\n" + "\n".join(fails[:4]) if fails else "")
    return (got_bz + got_tr) > 0 and left <= 0, txt


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
    what = f"en {ops.LANG_LABEL[lang]}" if lang else "(cualquier idioma, mejor copia primero)"
    wait = await q.message.reply_text(f"🔎 Buscando copias de <b>{_h(details['title'])}</b> {what} en todos los indexers (hasta 1 min)…",
                                      parse_mode=ParseMode.HTML)
    try:
        rows, total = await asyncio.to_thread(ops.releases, kind, lib["arr_id"], lang, None, 8)
    except Exception as e:
        log.exception("releases")
        return await wait.edit_text(f"💥 La busqueda fallo: {_h(e)}", parse_mode=ParseMode.HTML)
    if not rows:
        return await wait.edit_text(f"🤷 {total} resultados y ninguno sirve" + (f" (que traiga {ops.LANG_LABEL[lang]} con seeders)" if lang else " (con seeders)") + ".",
                                    parse_mode=ParseMode.HTML)
    _rel_seq[0] += 1
    key = str(_rel_seq[0])
    _rel_lists[key] = (kind, rows)
    for k in list(_rel_lists)[:-30]:
        _rel_lists.pop(k, None)
    lines = [f"🌐 <b>{_h(details['title'])}</b> {what} — {len(rows)} de {total}:"]
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
        return await update.effective_message.reply_text(ONBOARD, parse_mode=ParseMode.HTML)
    text = update.message.text.strip()
    if len(text) < 2:
        return await update.effective_message.reply_text("Escribe al menos 2 letras 🙂")

    it = await asyncio.to_thread(nlu.understand, text)
    log.info("nlu %s: %r -> %s %s", label, text, it["intent"], {k: v for k, v in it.items() if v and k not in ("intent",)})
    if it["intent"] != "search":
        if await handle_intent(update, ctx, role, label, it):
            return
    # busqueda: si Claude limpio el titulo, usalo (y conserva el idioma que detecto)
    if it["via"] == "llm" and it.get("title"):
        text = it["title"] + (f" en {it['lang']}" if it.get("lang") else "")

    wait = await update.effective_message.reply_text("🔎 Buscando…")
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
    try:
        await wait.delete()
    except TelegramError:
        pass
    # "dark temporada 3" de una serie que ya existe -> flujo de temporada, no tarjeta de "ya esta en Emby"
    if season and details["kind"] == "s" and status["status"] != "missing":
        if await season_flow(update, ctx, details, status, season, role, label):
            return
    caption, markup, poster = build_card(details, status, results[1:], alts, season, want_es, role, lang)
    await send_card(update.message, caption, markup, poster)


_STAFF_ONLY = "Eso es de moderadores 🙂 Si un titulo tiene un problema, escribeme «arregla los subs de X» o abre su tarjeta y toca 🚩 Reportar."
_CHAT_REPLIES = ["🙂 Aqui estoy. Escribeme el nombre de una peli o serie, o «menu».", "👋 Dime que quieres ver o arreglar.",
                 "🍿 Cuando quieras: un titulo, «que llego hoy», «arregla los subs de X»…"]


async def handle_intent(update, ctx, role, label, it):
    """Ejecuta una intencion de nlu.understand. -> True si respondio; False = que siga como busqueda."""
    msg, intent = update.effective_message, it["intent"]
    app = ctx.application
    staff = LEVEL[role] >= LEVEL["mod"]
    admin = role == "admin"

    # ---- sin titulo
    if intent == "chat":
        await msg.reply_text(_CHAT_REPLIES[int(time.time()) % len(_CHAT_REPLIES)])
        return True
    if intent == "help":
        await cmd_start(update, ctx)
        return True
    if intent == "menu":
        await cmd_menu(update, ctx)
        return True
    if intent == "pending":
        await cmd_pending(update, ctx)
        return True
    if intent == "recent":
        text, kb = await asyncio.to_thread(_recientes_text, it.get("hours") or 48, staff)
        await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    if intent in ("status", "sessions", "queue", "wanted", "reports", "users", "stats", "torrents", "log", "task", "restart", "covers_all"):
        need_admin = intent in ("torrents", "log", "task", "restart", "covers_all")
        if not staff or (need_admin and not admin):
            await msg.reply_text(_STAFF_ONLY if not staff else "Eso es solo del admin 🙂")
            return True
        if intent == "status":
            await cmd_sistema(update, ctx)
        elif intent == "sessions":
            await cmd_sesiones(update, ctx)
        elif intent == "queue":
            text, kb = await asyncio.to_thread(_cola_text, it.get("title"))
            await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif intent == "wanted":
            await cmd_faltantes(update, ctx)
        elif intent == "reports":
            await cmd_reportes(update, ctx)
        elif intent == "users":
            await cmd_users(update, ctx)
        elif intent == "stats":
            await cmd_stats(update, ctx)
        elif intent == "torrents":
            await cmd_torrents(update, ctx)
        elif intent == "log":
            ctx.args = [it.get("name") or "pipeline_health"]
            await cmd_log(update, ctx)
        elif intent == "task":
            if not it.get("name"):
                await cmd_tareas(update, ctx)
            else:
                name = it["name"]
                if name.endswith("-borrar"):
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Si, borrar", callback_data=f"task:{name}:yes"),
                                                InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
                    await msg.reply_text(f"¿Seguro? <b>{_h(ops.TASKS[name][0])}</b>", parse_mode=ParseMode.HTML, reply_markup=kb)
                else:
                    wait = await msg.reply_text(f"⏳ {_h(ops.TASKS[name][0])}…", parse_mode=ParseMode.HTML)
                    log_request("task", update, task=name)
                    ok, out = await run_long(ops.run_task, name)
                    await wait.edit_text(f"{'✅' if ok else '❌'} <b>{_h(ops.TASKS[name][0])}</b>\n<pre>{_h(out)}</pre>", parse_mode=ParseMode.HTML)
        elif intent == "restart":
            await ask_restart(msg)
        elif intent == "covers_all":
            wait = await msg.reply_text("🖼 Buscando items sin caratula y pidiendo refresco…")
            total, n, names = await run_long(_covers_all)
            await wait.edit_text(f"🖼 Sin caratula: {total}; refresco pedido para {n}.\n" + "\n".join(f"• {_h(x)}" for x in names),
                                 parse_mode=ParseMode.HTML)
        return True

    # ---- sobre un titulo
    title = (it.get("title") or "").strip()
    if not title:
        return False
    if intent == "delete" and not admin:
        await msg.reply_text("Borrar titulos es solo del admin 🙂")
        return True
    wait = await msg.reply_text(f"🔎 Buscando «{_h(title)}»…", parse_mode=ParseMode.HTML)
    try:
        details, status = await asyncio.to_thread(find_one, title)
    except Exception as e:
        log.exception("find_one %r", title)
        await wait.edit_text(f"💥 {_h(e)}", parse_mode=ParseMode.HTML)
        return True
    if not details:
        await wait.edit_text(f"🤷 No encontre «{_h(title)}».", parse_mode=ParseMode.HTML)
        return True
    kind, tmdb = details["kind"], details["tmdb"]
    full = f"{details['title']}{_year(details.get('year'))}"
    try:
        await wait.delete()
    except TelegramError:
        pass

    if intent == "add_season":
        if details["kind"] != "s":
            await msg.reply_text(f"«{_h(full)}» es una pelicula; no tiene temporadas 🙂")
            return True
        if not await season_flow(update, ctx, details, status, it["season"], role, label):
            caption, markup, poster = build_card(details, status, [], {}, it["season"], False, role, it.get("lang"))
            await send_card(msg, caption, markup, poster)
        return True

    if intent == "delete":
        if status["status"] == "missing":
            await msg.reply_text(f"«{_h(full)}» no esta en el servidor.", parse_mode=ParseMode.HTML)
            return True
        lib = status.get("lib")
        where = f"{app_name(kind)}" + (" y Emby" if status["status"] == "emby" else "")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Si, borrar con archivos", callback_data=f"del:{kind}:{tmdb}:yes"),
                                    InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
        note = "" if lib else "\n⚠️ No esta en Radarr/Sonarr: solo puedo avisarte la ruta, no borrar."
        await msg.reply_text(f"¿Borro <b>{_h(full)}</b> de {where} con sus archivos?{note}", parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if intent == "releases":
        if not staff:
            rid = new_report(update, kind, tmdb, full, "quality" if it.get("quality") or not it.get("lang") else "lang", it.get("lang"))
            log_request("report", update, kind=kind, tmdb=tmdb, title=full, type="quality", lang=it.get("lang"))
            await msg.reply_text(f"🚩 Anotado (#{rid}): quieres otra copia de <b>{_h(full)}</b>"
                                 f"{' en ' + ops.LANG_LABEL[it['lang']] if it.get('lang') else ' de mejor calidad'}. Un moderador la busca y te aviso.",
                                 parse_mode=ParseMode.HTML)
            r = _load_json(REPORTS_FILE, {}).get(rid)
            await notify_staff(app, _report_text(r), _report_kb(r))
            return True
        await do_releases(FakeQuery(msg), kind, tmdb, it.get("lang"), role)
        return True

    if status["status"] != "emby":
        if not staff:
            await msg.reply_text(f"«{_h(full)}» todavia no esta en Emby ({status['status']}); no hay nada que arreglar aun. Si quieres, pidela:")
            caption, markup, poster = build_card(details, status, [], {}, role=role)
            await send_card(msg, caption, markup, poster)
        else:
            await msg.reply_text(f"«{_h(full)}» no esta en Emby ({status['status']}); primero hay que tenerla.", parse_mode=ParseMode.HTML)
        return True

    if intent == "cover":
        if not staff:
            rid = new_report(update, kind, tmdb, full, "cover")
            log_request("report", update, kind=kind, tmdb=tmdb, title=full, type="cover")
            await msg.reply_text(f"🚩 Anotado (#{rid}): caratula/info de <b>{_h(full)}</b>. Un moderador la refresca y te aviso.", parse_mode=ParseMode.HTML)
            r = _load_json(REPORTS_FILE, {}).get(rid)
            await notify_staff(app, _report_text(r), _report_kb(r))
            return True
        log_request("cover", update, kind=kind, tmdb=tmdb)
        await do_cover(FakeQuery(msg), kind, tmdb)
        return True

    if intent in ("subs_fix", "subs_bad", "translate"):
        if not staff:
            await subs_self_service(update, ctx, details, status, bad=(intent == "subs_bad"))
            return True
        if intent == "translate":
            wait = await msg.reply_text(f"🤖 Traduciendo subs de <b>{_h(full)}</b> con nuestro modelo (en→es)… "
                                        f"{'una peli tarda 1-3 min' if kind == 'm' else 'una serie puede tardar bastante'}.", parse_mode=ParseMode.HTML)
            res = await run_long(_do_subs, "tr", kind, status["emby"], status.get("lib"), False)
            log_request("subs", update, action="tr", kind=kind, tmdb=tmdb, title=full)
            await wait.edit_text(f"🤖 <b>{_h(full)}</b>\n{_h(res)}", parse_mode=ParseMode.HTML)
            return True
        if intent == "subs_bad":
            await msg.reply_text("Para subs que estan mal: ⏱ Sincronizar (Bazarr los cuadra con el audio) o 🔁 Rehacer con nuestro modelo.")
        await subs_menu(FakeQuery(msg), kind, tmdb, role)
        return True
    return False


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

        # ---- v4: para todos los perfiles
        if action == "rec":
            await q.answer()
            text, kb = await asyncio.to_thread(_recientes_text, int(parts[1]), can(role, "wanted"))
            try:
                return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except TelegramError:
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

        if action == "menu":
            await q.answer()
            fn = {"pending": cmd_pending, "reports": cmd_reportes, "system": cmd_sistema, "stats": cmd_stats,
                  "tasks": cmd_tareas, "emby": cmd_emby}.get(parts[1])
            return await fn(update, ctx) if fn else None

        if action == "sea":
            tmdb, arr_id, season, have = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
            if role == "user" and quota_used(update.effective_user.id) >= DAILY_QUOTA:
                return await q.answer(f"Ya usaste tus {DAILY_QUOTA} peticiones de hoy 🙂", show_alert=True)
            await q.answer("Pidiendo…")
            ok = await asyncio.to_thread(ops.sonarr_want_season, arr_id, season)
            if not ok:
                return await q.message.reply_text("Sonarr no tiene esa temporada en la serie.")
            s = await asyncio.to_thread(ops.arr_series, arr_id)
            title = f"{s.get('title')}{_year(s.get('year'))}"
            remember_pending(chat_id, "s", arr_id, tmdb, f"{title} · T{season}", label, season=season, have=have)
            log_request("add", update, kind="s", tmdb=tmdb, arr_id=arr_id, title=title, mode=season)
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(f"➕ Temporada {season} de <b>{_h(title)}</b> pedida; Sonarr ya la busca y te aviso cuando lleguen episodios.",
                                       parse_mode=ParseMode.HTML)
            if role == "user":
                await notify_staff(ctx.application, f"➕ <b>{_h(label)}</b> pidio 📺 <b>{_h(title)}</b> temporada {season}")
            return

        # ---- de aqui en adelante: staff
        if not can(role, "subs"):
            return await q.answer("Eso es de moderadores 🙂", show_alert=True)

        if action == "q":
            sub = parts[1]
            if sub == "refresh":
                await q.answer("Leyendo colas…")
                text, kb = await asyncio.to_thread(_cola_text)
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            key, i = parts[2], int(parts[3])
            if key not in _q_lists or i >= len(_q_lists[key]):
                return await q.answer("Esa lista ya caduco; toca 🔄 Actualizar", show_alert=True)
            r = _q_lists[key][i]
            if len(parts) == 4:
                await q.answer()
                what = "quitar, VETAR esta copia y buscar otra" if sub == "bl" else "quitar de la cola (sin buscar otra)"
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Si", callback_data=f"q:{sub}:{key}:{i}:yes"),
                                            InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
                return await q.message.reply_text(f"¿{what}?\n<b>{_h(r['title'])}</b>\n<i>{_h(r['release'][:90])}</i>",
                                                  parse_mode=ParseMode.HTML, reply_markup=kb)
            await q.answer("Voy…")
            await asyncio.to_thread(ops.queue_remove, r["kind"], r["ids"], sub == "bl", sub == "bl")
            log_request("queue_rm", update, kind=r["kind"], title=r["title"], release=r["release"], blocklist=(sub == "bl"))
            await q.edit_message_reply_markup(None)
            return await q.message.reply_text(f"{'🔁 Vetada y buscando otra copia' if sub == 'bl' else '🗑 Quitada de la cola'}: <b>{_h(r['title'])}</b>",
                                              parse_mode=ParseMode.HTML)

        if action == "wnt":
            sub = parts[1]
            if sub == "list":
                await q.answer("Preguntando a Bazarr…")
                text, kb = await asyncio.to_thread(_faltantes_text)
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            if sub == "search":
                await q.answer("Lanzando…")
                fired = await asyncio.to_thread(ops.bazarr_search_wanted)
                return await q.message.reply_text(f"🔎 Bazarr busca lo que falta ({', '.join(fired) or 'no encontre la tarea; revisa /system/tasks'}). "
                                                  f"Tarda unos minutos; vuelve a mirar «que falta de subs».")
            if sub == "translate":
                await q.answer("Traduciendo…")
                wait = await q.message.reply_text("🤖 Traduciendo con nuestro modelo las pelis que Bazarr no resolvio (max 8, 1-3 min cada una)…")
                log_request("subs", update, action="wanted_translate")
                out = await run_long(_translate_wanted, 8)
                return await wait.edit_text(f"🤖 <b>Faltantes</b>\n{_h(out)}", parse_mode=ParseMode.HTML)
            if sub == "fixrecent":
                await q.answer("Arreglando…")
                wait = await q.message.reply_text("⚡ Arreglando subs de lo recien llegado (Bazarr → nuestro modelo; max 8 titulos)…")
                log_request("subs", update, action="fix_recent", hours=int(parts[2]))
                out = await run_long(_fix_recent, int(parts[2]), 8)
                return await wait.edit_text(f"⚡ <b>Recientes</b>\n{_h(out)}", parse_mode=ParseMode.HTML)

        if action == "rela":
            await q.answer("Buscando…")
            return await do_releases(q, parts[1], int(parts[2]), None, role)

        if action == "tor":
            if not can(role, "torrents"):
                return await q.answer("Solo el admin", show_alert=True)
            sub = parts[1]
            if sub == "list":
                await q.answer("Leyendo Transmission…")
                text, kb = await asyncio.to_thread(_torrents_text)
                return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            key, i = parts[2], int(parts[3])
            if key not in _t_lists or i >= len(_t_lists[key]):
                return await q.answer("Esa lista ya caduco; toca 🔄 Actualizar", show_alert=True)
            t = _t_lists[key][i]
            if len(parts) == 4:
                await q.answer()
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Si, borrar con datos", callback_data=f"tor:rm:{key}:{i}:yes"),
                                            InlineKeyboardButton("✖️ Cancelar", callback_data="nop")]])
                return await q.message.reply_text(f"¿Borro de Transmission (con sus archivos)?\n<b>{_h(t['name'][:90])}</b> · {t['pct']}% · {t['size_gb']} GB",
                                                  parse_mode=ParseMode.HTML, reply_markup=kb)
            await q.answer("Borrando…")
            await asyncio.to_thread(ops.transmission_remove, [t["id"]], True)
            log_request("torrent_rm", update, name=t["name"])
            await q.edit_message_reply_markup(None)
            return await q.message.reply_text(f"🗑 Borrado: <b>{_h(t['name'][:90])}</b>", parse_mode=ParseMode.HTML)

        if action == "del":
            if not can(role, "delete"):
                return await q.answer("Solo el admin", show_alert=True)
            kind, tmdb = parts[1], int(parts[2])
            await q.answer("Borrando…")
            await q.edit_message_reply_markup(None)
            details, status = await asyncio.to_thread(locate, kind, tmdb)
            lib, item = status.get("lib"), status.get("emby")
            full = f"{details['title']}{_year(details.get('year'))}"
            if not lib:
                paths = await asyncio.to_thread(video_paths, kind, item) if item else []
                where = os.path.dirname(paths[0][1]) if paths else "?"
                return await q.message.reply_text(f"⚠️ <b>{_h(full)}</b> no esta en {app_name(kind)}; borra a mano la carpeta:\n<code>{_h(where)}</code>",
                                                  parse_mode=ParseMode.HTML)
            await asyncio.to_thread(ops.arr_delete, kind, lib["arr_id"], True)
            _library_cache["at"] = _emby_idx["at"] = 0
            log_request("delete", update, kind=kind, tmdb=tmdb, title=full)
            return await q.message.reply_text(f"🗑 <b>{_h(full)}</b> borrada de {app_name(kind)} con sus archivos; Emby la quita en unos minutos.",
                                              parse_mode=ParseMode.HTML)

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
                    "trr": "🔁 rehaciendo es con nuestro modelo", "q": "🧹 encolando", "bz": "🌍 Bazarr traduciendo",
                    "auto": "⚡ automatico: Bazarr y, lo que falte, nuestro modelo", "sync": "⏱ Bazarr sincronizando con el audio"}[sub]
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
    base = [BotCommand("start", "Como funciona"), BotCommand("menu", "Botones"), BotCommand("recientes", "Lo nuevo en Emby"),
            BotCommand("pendientes", "Lo que pediste y aun no llega"),
            BotCommand("vincular", "Vincular tu cuenta de Emby"), BotCommand("id", "Tu id de Telegram")]
    mod = base + [BotCommand("cola", "Descargas en curso"), BotCommand("faltantes", "Sin subs en espanol"),
                  BotCommand("sistema", "Salud del pipeline y Emby"), BotCommand("sesiones", "Quien esta viendo"),
                  BotCommand("reportes", "Problemas abiertos"), BotCommand("usuarios", "Vinculados y roles"), BotCommand("stats", "Numeros")]
    adm = mod + [BotCommand("torrents", "Transmission"), BotCommand("borrar", "Quitar un titulo con archivos"),
                 BotCommand("emby", "Reiniciar, caratulas, salud"), BotCommand("tareas", "Scripts del pipeline"),
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
    log.info("listo v4; admins=%s staff=%s usuarios=%d cupo=%d/dia nlu_llm=%s", sorted(ADMINS) or "NINGUNO", sorted(staff_ids()),
             len(load_users()), DAILY_QUOTA, nlu.NLU_MODEL if nlu.llm_available() else "off")


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
    app.add_handler(CommandHandler(["cola", "descargas"], cmd_cola))
    app.add_handler(CommandHandler(["recientes", "nuevo"], cmd_recientes))
    app.add_handler(CommandHandler(["faltantes", "subs"], cmd_faltantes))
    app.add_handler(CommandHandler("torrents", cmd_torrents))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("borrar", cmd_borrar))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
