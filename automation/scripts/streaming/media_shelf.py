"""Canonical shelf classifier for the media library.

One rule set, used by trending_add.py (what to do when adding) and by
librarian.py (what to do with what is already on disk).

Shelves, per media kind:
    movies / tv                 live action
    moviesanimated / tvanimated animation, non-asian
    moviesanime / tvanime       Japanese animation
    moviesdonghua / tvdonghua   Chinese animation
    moviesaeni / tvaeni         Korean animation

The asian split is by ORIGINAL LANGUAGE, not production country: a Japanese
co-production credit does not make a French film anime (The Red Turtle), and a
Ghibli distribution deal does not either. For series only, the broadcasting
country breaks the tie when the language does not — an anime commissioned in
English still airs on TV Tokyo (Lazarus).
"""
import json, os, pathlib, time, urllib.request

MEDIA_ROOT = "/APPBOX_DATA/storage/media"
CACHE_FILE = pathlib.Path("/config/berenstuff/automation/cache/tmdb_classify.json")

ANIMATION_GENRE_ID = 16
ANIMATED_GENRE_NAMES = {"animation", "anime"}
LANG_SHELF = {"ja": "anime", "zh": "donghua", "cn": "donghua", "ko": "aeni"}
# series only: where it airs, when the language is not decisive
COUNTRY_SHELF = {"JP": "anime", "CN": "donghua", "TW": "donghua", "HK": "donghua", "KR": "aeni"}

SHELVES = {
    "movie": {"live": "movies", "animated": "moviesanimated", "anime": "moviesanime",
              "donghua": "moviesdonghua", "aeni": "moviesaeni"},
    "tv":    {"live": "tv", "animated": "tvanimated", "anime": "tvanime",
              "donghua": "tvdonghua", "aeni": "tvaeni"},
}


def _cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(c):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(c))


def _fetch(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1)


def tmdb_meta(kind, tmdb_id=None, tvdb_id=None, api_key=None, cache=None):
    """Return {'lang','genres','countries'} or None when TMDB cannot answer."""
    api_key = api_key or os.environ.get("TMDB_API_KEY", "")
    own_cache = cache is None
    cache = _cache() if own_cache else cache
    key = f"{kind}:{tmdb_id or ('tvdb' + str(tvdb_id))}"
    if key in cache:
        return cache[key]
    data = None
    if tmdb_id:
        data = _fetch(f"https://api.themoviedb.org/3/{kind}/{tmdb_id}?api_key={api_key}")
    elif tvdb_id:
        found = _fetch(f"https://api.themoviedb.org/3/find/{tvdb_id}"
                       f"?api_key={api_key}&external_source=tvdb_id") or {}
        hits = found.get("tv_results") or found.get("movie_results") or []
        data = hits[0] if hits else None
    if not data:
        return None
    meta = {
        "lang": data.get("original_language"),
        "genres": [g["id"] for g in data.get("genres", [])] or data.get("genre_ids", []),
        "countries": data.get("origin_country")
                     or [c["iso_3166_1"] for c in data.get("production_countries", [])],
    }
    cache[key] = meta
    if own_cache:
        _save_cache(cache)
    return meta


def classify(kind, tmdb_id=None, tvdb_id=None, genre_names=None, api_key=None, cache=None):
    """Return (shelf_folder, reason). kind is 'movie' or 'tv'."""
    kind = "tv" if kind in ("tv", "series", "show") else "movie"
    genre_names = {g.lower() for g in (genre_names or [])}
    meta = tmdb_meta(kind, tmdb_id, tvdb_id, api_key, cache)

    if meta is None:
        animated = bool(ANIMATED_GENRE_NAMES & genre_names)
        return SHELVES[kind]["animated" if animated else "live"], "sin datos de TMDB"

    # TMDB's genre list decides. The names coming from Emby/TVDB are only a
    # fallback for when TMDB has nothing to say: they call Peter Rabbit 2
    # "Animation" because of its CGI rabbits, and it is a live action film.
    animated = ANIMATION_GENRE_ID in (meta["genres"] or [])
    if not animated:
        return SHELVES[kind]["live"], "no es animacion"

    lang = (meta["lang"] or "").lower()
    if lang in LANG_SHELF:
        return SHELVES[kind][LANG_SHELF[lang]], f"idioma original {lang}"
    if kind == "tv":
        for c in (meta["countries"] or []):
            if c in COUNTRY_SHELF:
                return SHELVES[kind][COUNTRY_SHELF[c]], f"idioma {lang} pero emite en {c}"
    return SHELVES[kind]["animated"], f"animacion, idioma {lang}"


def shelf_path(shelf):
    return f"{MEDIA_ROOT}/{shelf}"


def shelf_of_path(path):
    """Which shelf a file currently lives on, read straight off its path."""
    marker = f"{MEDIA_ROOT}/"
    return path.split(marker, 1)[1].split("/", 1)[0] if marker in path else None
