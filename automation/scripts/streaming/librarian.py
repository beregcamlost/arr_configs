#!/usr/bin/env python3
"""Keep every title on the shelf its metadata says it belongs to.

Two ways in:

  on-import   Radarr/Sonarr run this right after they import something. If the
              title landed on the wrong shelf, it gets moved before anyone sees
              it there.
  scan        Nightly sweep over everything already in the library, for the
              ones added by hand, or whose metadata changed after the fact.

Moving is done through Radarr/Sonarr (moveFiles=true) so their databases stay
truthful, and Emby's watch state is carried across the move by hand, because
Emby treats a moved file as a new item and would drop it.
"""
import argparse, json, os, pathlib, sys, time, urllib.parse, urllib.request

sys.path.insert(0, "/config/berenstuff/automation/scripts")
from streaming.media_shelf import classify, shelf_path, shelf_of_path
from streaming import emby_userdata as eud

LOG = pathlib.Path("/config/berenstuff/automation/logs/librarian.log")
STATE_DIR = pathlib.Path("/config/berenstuff/automation/backups/librarian")
NIGHTLY_LIMIT = 25          # a metadata glitch must not restructure the library
LIB_IDS = ["3", "12589"]    # Peliculas y Series: entre las dos ven todo


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def env(*names, **kw):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return kw.get("default")


def arr_req(url, key, method="GET", body=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def emby(path, **params):
    params["api_key"] = os.environ["EMBY_API_KEY"]
    url = "%s%s?%s" % (os.environ["EMBY_URL"], path, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def playing_now():
    """Paths currently being watched - those are left alone."""
    busy = set()
    try:
        for s in emby("/Sessions") or []:
            p = (s.get("NowPlayingItem") or {}).get("Path") or ""
            if p:
                busy.add(p)
                busy.add(pathlib.Path(p).parent.name)
    except Exception as e:
        log("  aviso: no pude leer las sesiones activas (%s); por seguridad no muevo nada" % e)
        return None
    return busy


def collect_targets():
    """Every movie/series, with where it is and where it should be."""
    out = []
    sources = (
        ("movie", os.environ["RADARR_URL"], os.environ["RADARR_KEY"], "/api/v3/movie"),
        ("tv", os.environ["SONARR_URL"], os.environ["SONARR_KEY"], "/api/v3/series"),
    )
    for kind, url, key, ep in sources:
        for it in arr_req(url + ep, key) or []:
            path = it.get("path") or ""
            cur = shelf_of_path(path)
            if not cur:
                continue
            tmdb = str(it["tmdbId"]) if it.get("tmdbId") else None
            tvdb = str(it["tvdbId"]) if it.get("tvdbId") else None
            want, why = classify(kind, tmdb, tvdb,
                                 genre_names=list(it.get("genres") or []))
            out.append({"kind": kind, "title": it.get("title"), "arr_id": it["id"],
                        "path": path, "current": cur, "want": want, "why": why,
                        "obj": it, "url": url, "key": key})
    return out


def move_one(t):
    """Hand the move to Radarr/Sonarr so their database stays truthful."""
    dest_root = shelf_path(t["want"])
    pathlib.Path(dest_root).mkdir(parents=True, exist_ok=True)
    obj = t["obj"]
    obj["path"] = "%s/%s" % (dest_root, pathlib.Path(t["path"]).name)
    obj["rootFolderPath"] = dest_root
    endpoint = "movie" if t["kind"] == "movie" else "series"
    arr_req("%s/api/v3/%s/%s?moveFiles=true" % (t["url"], endpoint, t["arr_id"]),
            t["key"], "PUT", obj)
    log("  [MOVIDO] %s: %s -> %s (%s)" % (t["title"], t["current"], t["want"], t["why"]))


def wait_for_scan(timeout=1800):
    """Emby re-indexes in the background; the watch state has nowhere to land
    until it has finished, so wait for the task instead of guessing a sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            running = [x for x in (emby("/ScheduledTasks") or [])
                       if x.get("State") == "Running" and "Scan" in x.get("Name", "")]
        except Exception:
            running = []
        if not running:
            return True
        time.sleep(20)
    log("  aviso: el escaneo de Emby seguia corriendo tras %ss" % timeout)
    return False


def apply_moves(targets, dry_run=True):
    """Move a batch, carrying everyone's watch state across."""
    if not targets:
        return 0
    if dry_run:
        for t in targets:
            log("  [AVISO] %s: %s -> %s (%s)"
                % (t["title"], t["current"], t["want"], t["why"]))
        return 0

    users = [u["Id"] for u in emby("/Users")]
    log("  respaldando el estado de visto de %s usuarios..." % len(users))
    before = eud.collect(os.environ["EMBY_URL"], os.environ["EMBY_API_KEY"], users, LIB_IDS)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snap = STATE_DIR / ("userdata-%s.json" % stamp)
    snap.write_text(json.dumps(before))
    log("  respaldo: %s registros -> %s"
        % (sum(len(v) for v in before.values()), snap))

    moved = 0
    for t in targets:
        try:
            move_one(t)
            moved += 1
        except Exception as e:
            log("  [FALLO] %s: %s" % (t["title"], e))

    if moved:
        emby("/Library/Refresh")
        wait_for_scan()
        applied, missing = eud.restore(os.environ["EMBY_URL"], os.environ["EMBY_API_KEY"],
                                       before, LIB_IDS, report=log)
        log("  estado de visto devuelto: %s aplicados, %s sin destino" % (applied, missing))
    return moved


def cmd_scan(args):
    targets = collect_targets()
    wrong = [t for t in targets if t["current"] != t["want"]]
    log("revisados %s titulos; mal ubicados: %s" % (len(targets), len(wrong)))
    if not wrong:
        return 0
    busy = playing_now()
    if busy is None:
        return 1
    todo = []
    for t in wrong:
        if t["path"] in busy or pathlib.Path(t["path"]).name in busy:
            log("  [OMITIDO] %s: alguien lo esta viendo ahora" % t["title"])
            continue
        todo.append(t)
    if len(todo) > NIGHTLY_LIMIT and not args.no_limit:
        log("  tope de seguridad: %s mal ubicados, muevo %s esta vez"
            % (len(todo), NIGHTLY_LIMIT))
        todo = todo[:NIGHTLY_LIMIT]
    apply_moves(todo, dry_run=args.dry_run)
    return 0


def cmd_on_import(args):
    ev = env("radarr_eventtype", "sonarr_eventtype", default="?")
    if ev == "Test":
        log("evento Test recibido: la conexion con el *arr funciona")
        log("  variables visibles: " + ", ".join(sorted(
            k for k in os.environ if k.startswith(("radarr_", "sonarr_")))))
        return 0
    if ev not in ("Download", "MovieAdded", "SeriesAdd", "Rename", "MovieFileImported"):
        log("evento '%s' ignorado" % ev)
        return 0
    is_movie = bool(env("radarr_eventtype"))
    kind = "movie" if is_movie else "tv"
    arr_id = env("radarr_movie_id", "sonarr_series_id")
    tmdb = env("radarr_movie_tmdbid", "sonarr_series_tmdbid")
    tvdb = env("sonarr_series_tvdbid")
    path = env("radarr_movie_path", "sonarr_series_path", default="")
    title = env("radarr_movie_title", "sonarr_series_title", default="?")
    if not arr_id or not path:
        log("evento %s sin id/ruta utilizable; no hago nada" % ev)
        return 0
    want, why = classify(kind, tmdb, tvdb)
    cur = shelf_of_path(path)
    if cur == want:
        log("%s: ya esta en su sitio (%s)" % (title, cur))
        return 0
    log("%s: llego a '%s' y le toca '%s' (%s)" % (title, cur, want, why))
    url = os.environ["RADARR_URL"] if is_movie else os.environ["SONARR_URL"]
    key = os.environ["RADARR_KEY"] if is_movie else os.environ["SONARR_KEY"]
    ep = "movie" if is_movie else "series"
    obj = arr_req("%s/api/v3/%s/%s" % (url, ep, arr_id), key)
    t = {"kind": kind, "title": title, "arr_id": int(arr_id), "path": path,
         "current": cur, "want": want, "why": why, "obj": obj, "url": url, "key": key}
    apply_moves([t], dry_run=args.dry_run)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Put every title on its right shelf.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("scan", help="sweep the whole library")
    s.add_argument("--apply", dest="dry_run", action="store_false", default=True)
    s.add_argument("--no-limit", action="store_true")
    s.set_defaults(func=cmd_scan)
    o = sub.add_parser("on-import", help="run from a Radarr/Sonarr custom script")
    o.add_argument("--apply", dest="dry_run", action="store_false", default=True)
    o.set_defaults(func=cmd_on_import)
    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
