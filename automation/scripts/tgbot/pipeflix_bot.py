#!/usr/bin/env python3
"""PIPEFLIX Telegram bot — escribe un titulo y el bot lo busca en Emby; si no
esta, lo pide a Radarr (peliculas) o Sonarr (series) y avisa cuando llega.

Corre en mubuntu (ligero, una peticion HTTP por mensaje). Lanzado por
bot_up.sh (cron cada 5 min + @reboot). Secretos en /config/berenstuff/.env:
    TELEGRAM_BOT_TOKEN   token de @BotFather
    TELEGRAM_ALLOWED_IDS ids de Telegram autorizados, separados por coma
                         (vacio = nadie; el bot te dice tu id para que lo anadas)

La carpeta destino sale de streaming.media_shelf.classify(), la misma regla que
usan trending_add.py y el librarian, asi el titulo cae donde caeria de todos modos.
"""
import asyncio
import html
import json
import logging
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from streaming.arr_client import (add_movie, ensure_tag, fetch_movies,
                                  fetch_series)
from streaming.media_shelf import classify as classify_shelf, shelf_path

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("pipeflix_bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").replace(";", ",").split(",")
           if x.strip().isdigit()}
RADARR_URL = os.environ["RADARR_URL"]
RADARR_KEY = os.environ["RADARR_KEY"]
SONARR_URL = os.environ["SONARR_URL"]
SONARR_KEY = os.environ["SONARR_KEY"]
EMBY_API = os.environ.get("EMBY_INTERNAL_BASE") or (os.environ["EMBY_URL"] + "/emby")
EMBY_WEB = os.environ.get("EMBY_URL", "https://emby.vhscave.appboxes.co")
EMBY_KEY = os.environ["EMBY_API_KEY"]

RADARR_QUALITY_PROFILE = 1  # HD-1080p (mismo que trending_add.py)
SONARR_QUALITY_PROFILE = 4  # HD-1080p
TAG_LABEL = "telegram-add"
MAX_RESULTS = 5              # por tipo (peli / serie)
PENDING_FILE = pathlib.Path("/config/berenstuff/automation/cache/telegram_pending.json")
PIDFILE = pathlib.Path("/tmp/pipeflix_bot.pid")
PENDING_POLL_S = 300
PENDING_GIVEUP_S = 14 * 86400
LIBRARY_TTL_S = 120

_pool = ThreadPoolExecutor(max_workers=4)
_emby_server_id = ""
_library_cache = {"at": 0.0, "movies": {}, "series": {}}


# ---------------------------------------------------------------- helpers
def _h(s):
    return html.escape(str(s or ""))


def _year(x):
    return f" ({x})" if x else ""


def _arr_get(base, key, path, **params):
    r = requests.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _arr_post(base, key, path, body):
    r = requests.post(f"{base}/api/v3/{path}", headers={"X-Api-Key": key},
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _emby_get(path, **params):
    params["api_key"] = EMBY_KEY
    r = requests.get(f"{EMBY_API}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def emby_link(item_id):
    return f"{EMBY_WEB}/web/index.html#!/item?id={item_id}&serverId={_emby_server_id}"


def library_ids():
    """{tmdb_id: movie} y {tvdb_id: series} de lo que ya esta en Radarr/Sonarr (cache 2 min)."""
    now = time.time()
    if now - _library_cache["at"] > LIBRARY_TTL_S:
        movies = {m["tmdb_id"]: m for m in fetch_movies(RADARR_URL, RADARR_KEY)}
        series = {}
        for s in _arr_get(SONARR_URL, SONARR_KEY, "series"):
            eps = (s.get("statistics") or {}).get("episodeFileCount", 0)
            series[s.get("tvdbId")] = {"arr_id": s["id"], "has_file": eps > 0,
                                       "title": s.get("title"), "path": s.get("path", "")}
        _library_cache.update(at=now, movies=movies, series=series)
    return _library_cache["movies"], _library_cache["series"]


def emby_search(term):
    """Peliculas/series en Emby que casan con el termino, sin duplicados
    (los arboles /virtual devuelven el mismo titulo dos veces)."""
    data = _emby_get("Items", SearchTerm=term, IncludeItemTypes="Movie,Series",
                     Recursive="true", Fields="ProductionYear,ProviderIds,Path", Limit=20)
    seen, out = set(), []
    for it in data.get("Items", []):
        prov = it.get("ProviderIds") or {}
        key = (it["Type"], prov.get("Tmdb") or prov.get("Tvdb")
               or f"{it['Name']}|{it.get('ProductionYear')}")
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": it["Id"], "type": it["Type"], "name": it["Name"],
                    "year": it.get("ProductionYear"),
                    "tmdb": int(prov["Tmdb"]) if str(prov.get("Tmdb", "")).isdigit() else None,
                    "tvdb": int(prov["Tvdb"]) if str(prov.get("Tvdb", "")).isdigit() else None})
    return out


def do_search(term):
    """Emby + Radarr lookup + Sonarr lookup en paralelo."""
    f_emby = _pool.submit(emby_search, term)
    f_mov = _pool.submit(_arr_get, RADARR_URL, RADARR_KEY, "movie/lookup", term=term)
    f_ser = _pool.submit(_arr_get, SONARR_URL, SONARR_KEY, "series/lookup", term=term)
    f_lib = _pool.submit(library_ids)
    emby = f_emby.result()
    movies = f_mov.result()[:MAX_RESULTS]
    series = f_ser.result()[:MAX_RESULTS]
    lib_movies, lib_series = f_lib.result()
    return emby, movies, series, lib_movies, lib_series


def add_series_by_tvdb(tvdb_id, root):
    hits = _arr_get(SONARR_URL, SONARR_KEY, "series/lookup", term=f"tvdb:{tvdb_id}")
    if not hits:
        return None
    s = hits[0]
    s["qualityProfileId"] = SONARR_QUALITY_PROFILE
    s["rootFolderPath"] = root
    s["monitored"] = True
    s["seasonFolder"] = True
    s["tags"] = [ensure_tag(SONARR_URL, SONARR_KEY, TAG_LABEL)]
    s["addOptions"] = {"searchForMissingEpisodes": True}
    for season in s.get("seasons", []):
        season["monitored"] = season.get("seasonNumber", 0) > 0
    return _arr_post(SONARR_URL, SONARR_KEY, "series", s)


def do_add(kind, ext_id):
    """Anade a Radarr/Sonarr en la carpeta que dicta media_shelf. Devuelve (obj, shelf, why)."""
    if kind == "m":
        shelf, why = classify_shelf("movie", tmdb_id=ext_id)
        root = shelf_path(shelf)
        pathlib.Path(root).mkdir(parents=True, exist_ok=True)
        tag = ensure_tag(RADARR_URL, RADARR_KEY, TAG_LABEL)
        obj = add_movie(RADARR_URL, RADARR_KEY, ext_id, RADARR_QUALITY_PROFILE, root, tags=[tag])
    else:
        shelf, why = classify_shelf("tv", tvdb_id=ext_id)
        root = shelf_path(shelf)
        pathlib.Path(root).mkdir(parents=True, exist_ok=True)
        obj = add_series_by_tvdb(ext_id, root)
    _library_cache["at"] = 0  # que la proxima busqueda ya lo vea
    return obj, shelf, why


def do_research(kind, arr_id):
    if kind == "m":
        return _arr_post(RADARR_URL, RADARR_KEY, "command", {"name": "MoviesSearch", "movieIds": [arr_id]})
    return _arr_post(SONARR_URL, SONARR_KEY, "command", {"name": "SeriesSearch", "seriesId": arr_id})


# ---------------------------------------------------------------- pendientes (aviso al llegar)
def _load_pending():
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return {}


def _save_pending(p):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(p, indent=1, ensure_ascii=False))


def remember_pending(chat_id, kind, arr_id, title):
    p = _load_pending()
    p[f"{kind}:{arr_id}"] = {"chat_id": chat_id, "kind": kind, "arr_id": arr_id,
                             "title": title, "since": time.time()}
    _save_pending(p)


def _arrived(kind, arr_id):
    if kind == "m":
        m = _arr_get(RADARR_URL, RADARR_KEY, f"movie/{arr_id}")
        return bool(m.get("hasFile")), m
    s = _arr_get(SONARR_URL, SONARR_KEY, f"series/{arr_id}")
    return (s.get("statistics") or {}).get("episodeFileCount", 0) > 0, s


async def pending_loop(app):
    await asyncio.sleep(60)
    while True:
        try:
            p = _load_pending()
            changed = False
            for key, req in list(p.items()):
                try:
                    ok, obj = await asyncio.to_thread(_arrived, req["kind"], req["arr_id"])
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404:
                        ok, obj = None, None  # lo borraron del *arr
                    else:
                        raise
                if ok:
                    what = "pelicula" if req["kind"] == "m" else "serie (primer episodio)"
                    await app.bot.send_message(
                        req["chat_id"],
                        f"📥 <b>{_h(req['title'])}</b> ya se descargo ({what}). "
                        f"Emby la agrega en el proximo escaneo, en unos minutos.",
                        parse_mode=ParseMode.HTML)
                if ok or ok is None or time.time() - req["since"] > PENDING_GIVEUP_S:
                    if ok is False:
                        await app.bot.send_message(
                            req["chat_id"],
                            f"⌛ <b>{_h(req['title'])}</b> lleva 14 dias sin aparecer; "
                            f"dejo de vigilarla (sigue pedida en Radarr/Sonarr).",
                            parse_mode=ParseMode.HTML)
                    p.pop(key, None)
                    changed = True
            if changed:
                _save_pending(p)
        except Exception:
            log.exception("pending_loop")
        await asyncio.sleep(PENDING_POLL_S)


# ---------------------------------------------------------------- handlers
def _authorized(update):
    u = update.effective_user
    return bool(u) and u.id in ALLOWED


async def _deny(update):
    u = update.effective_user
    await update.effective_message.reply_text(
        f"🚫 No estas en la lista. Tu id de Telegram es <code>{u.id}</code> — "
        f"pidele al admin que lo anada a TELEGRAM_ALLOWED_IDS.",
        parse_mode=ParseMode.HTML)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        "🎬 <b>PIPEFLIX</b>\n\n"
        "Escribeme el nombre de una pelicula o serie y te digo si ya esta en Emby; "
        "si no, toca el boton y la pido a Radarr/Sonarr. Te aviso cuando se descargue.\n\n"
        "/pendientes — lo que pediste y aun no llega\n"
        "/id — tu id de Telegram",
        parse_mode=ParseMode.HTML)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu id: <code>{update.effective_user.id}</code>",
                                    parse_mode=ParseMode.HTML)


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    mine = [r for r in _load_pending().values() if r["chat_id"] == update.effective_chat.id]
    if not mine:
        return await update.message.reply_text("Nada pendiente 👌")
    lines = [f"• {'🎬' if r['kind'] == 'm' else '📺'} {_h(r['title'])}" for r in mine]
    await update.message.reply_text("⏳ <b>Pendientes</b>\n" + "\n".join(lines),
                                    parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return await _deny(update)
    term = update.message.text.strip()
    if len(term) < 2:
        return await update.message.reply_text("Escribe al menos 2 letras 🙂")
    wait = await update.message.reply_text(f"🔎 Buscando «{_h(term)}»…", parse_mode=ParseMode.HTML)
    try:
        emby, movies, series, lib_movies, lib_series = await asyncio.to_thread(do_search, term)
    except Exception as e:
        log.exception("search %r", term)
        return await wait.edit_text(f"💥 Fallo la busqueda: {_h(e)}", parse_mode=ParseMode.HTML)

    lines, buttons = [], []
    emby_tmdb = {e["tmdb"] for e in emby if e["tmdb"]}
    emby_tvdb = {e["tvdb"] for e in emby if e["tvdb"]}
    emby_names = {(e["name"].lower(), e["year"]) for e in emby}

    if emby:
        lines.append("✅ <b>Ya en Emby</b>")
        for e in emby:
            ico = "🎬" if e["type"] == "Movie" else "📺"
            lines.append(f"  {ico} <a href=\"{emby_link(e['id'])}\">{_h(e['name'])}{_year(e['year'])}</a>")

    for m in movies:
        tmdb = m.get("tmdbId")
        if tmdb in emby_tmdb or (m["title"].lower(), m.get("year")) in emby_names:
            continue
        label = f"🎬 {m['title']}{_year(m.get('year'))}"
        lib = lib_movies.get(tmdb)
        if lib and lib["has_file"]:
            lines.append(f"  💾 {_h(label[2:])} — descargada, esperando a Emby")
            continue
        if lib:
            buttons.append([InlineKeyboardButton(f"⏳ {label[2:]} · ya pedida, reintentar",
                                                 callback_data=f"rs:m:{lib['arr_id']}")])
        else:
            buttons.append([InlineKeyboardButton(label, callback_data=f"add:m:{tmdb}")])

    for s in series:
        tvdb = s.get("tvdbId")
        if tvdb in emby_tvdb or (s["title"].lower(), s.get("year")) in emby_names:
            continue
        label = f"📺 {s['title']}{_year(s.get('year'))}"
        lib = lib_series.get(tvdb)
        if lib and lib["has_file"]:
            lines.append(f"  💾 {_h(label[2:])} — descargada, esperando a Emby")
            continue
        if lib:
            buttons.append([InlineKeyboardButton(f"⏳ {label[2:]} · ya pedida, reintentar",
                                                 callback_data=f"rs:s:{lib['arr_id']}")])
        else:
            buttons.append([InlineKeyboardButton(label, callback_data=f"add:s:{tvdb}")])

    if buttons:
        lines.append("")
        lines.append("👇 Toca uno para pedirlo:")
    elif not emby:
        lines.append(f"🤷 No encontre nada para «{_h(term)}».")
    await wait.edit_text("\n".join(lines) or "🤷 Nada.", parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                         disable_web_page_preview=True)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not _authorized(update):
        await q.answer("No autorizado", show_alert=True)
        return
    action, kind, ext = q.data.split(":")
    ext = int(ext)
    await q.answer("Dale, un momento…")
    chat_id = update.effective_chat.id
    try:
        if action == "add":
            obj, shelf, why = await asyncio.to_thread(do_add, kind, ext)
            if not obj:
                return await q.message.reply_text("💥 Radarr/Sonarr no encontro ese id, raro.")
            title = f"{obj.get('title')}{_year(obj.get('year'))}"
            remember_pending(chat_id, kind, obj["id"], title)
            app = "Radarr" if kind == "m" else "Sonarr"
            await q.message.reply_text(
                f"➕ Pedida a {app}: <b>{_h(title)}</b>\n"
                f"📁 {shelf} ({_h(why)})\n"
                f"Ya esta buscando; te aviso cuando se descargue.",
                parse_mode=ParseMode.HTML)
        elif action == "rs":
            await asyncio.to_thread(do_research, kind, ext)
            _, obj = await asyncio.to_thread(_arrived, kind, ext)
            title = f"{obj.get('title')}{_year(obj.get('year'))}"
            remember_pending(chat_id, kind, ext, title)
            await q.message.reply_text(f"🔁 Busqueda relanzada para <b>{_h(title)}</b>; te aviso si llega.",
                                       parse_mode=ParseMode.HTML)
    except requests.HTTPError as e:
        body = ""
        try:
            body = "; ".join(x.get("errorMessage", "") for x in e.response.json())
        except Exception:
            pass
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 {app_name(kind)} respondio {e.response.status_code}: {_h(body or e)}",
                                   parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("button %s", q.data)
        await q.message.reply_text(f"💥 Error: {_h(e)}", parse_mode=ParseMode.HTML)


def app_name(kind):
    return "Radarr" if kind == "m" else "Sonarr"


async def post_init(app):
    global _emby_server_id
    try:
        _emby_server_id = (await asyncio.to_thread(_emby_get, "System/Info"))["Id"]
    except Exception:
        log.exception("Emby System/Info")
    PIDFILE.write_text(str(os.getpid()))
    app.create_task(pending_loop(app))
    log.info("listo; autorizados=%s", sorted(ALLOWED) or "NADIE (anade tu id a TELEGRAM_ALLOWED_IDS)")


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN vacio en .env")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help", "ayuda"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("pendientes", cmd_pending))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
