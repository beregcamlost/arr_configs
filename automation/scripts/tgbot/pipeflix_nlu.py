#!/usr/bin/env python3
"""pipeflix_nlu.py — entiende lo que la gente escribe en el bot (sin comandos).

Dos capas:
  1. reglas (regex en espanol/ingles, 0 ms, sin red): cubren las formas habituales
     "arregla los subs de dune", "que esta bajando", "borra scary movie 2", "temporada 3 de dark"...
  2. Claude (opcional): si ninguna regla encaja y la frase parece una peticion (>= 4 palabras,
     pregunta, verbo al inicio) se le pide al modelo que clasifique en el mismo catalogo de
     intenciones y saque titulo/temporada/idioma. Se activa solo con ANTHROPIC_API_KEY en .env
     (modelo en PIPEFLIX_NLU_MODEL, por defecto claude-opus-5). Sin clave, la capa 2 no existe y
     todo lo que no encaja se trata como busqueda de titulo (como antes).

understand(text) -> dict con SIEMPRE estas llaves:
  intent  : una de INTENTS (o "search")
  title   : titulo tal cual lo escribio (None si no aplica)
  season  : int | None        lang: clave de ops.LANGS | None
  name    : nombre de log / tarea (intents log, task)
  hours   : ventana para "recent" (24/48/168)
  quality : True si pidio mejor calidad (releases)
  via     : "rule" | "llm" | "none"
"""
import json
import logging
import os
import re
import unicodedata

log = logging.getLogger("pipeflix_nlu")

INTENTS = {
    "search":     "buscar o pedir una pelicula o serie por su nombre (lo mas comun)",
    "add_season": "pedir una temporada concreta de una serie",
    "subs_fix":   "conseguir, arreglar o mejorar los subtitulos en espanol de un titulo",
    "subs_bad":   "los subtitulos de un titulo estan mal: desincronizados, atrasados, en otro idioma, mal traducidos",
    "translate":  "traducir los subtitulos de un titulo con nuestro modelo",
    "cover":      "arreglar la caratula, portada, poster o metadatos de un titulo",
    "covers_all": "barrido general de caratulas faltantes",
    "releases":   "buscar otra copia de un titulo (en un idioma concreto o de mejor calidad)",
    "delete":     "borrar o quitar un titulo del servidor",
    "queue":      "que se esta descargando ahora, la cola, cuanto falta",
    "recent":     "que llego nuevo, novedades, lo ultimo agregado",
    "wanted":     "que titulos faltan de subtitulos en espanol",
    "torrents":   "estado de transmission / torrents",
    "status":     "salud del sistema, del pipeline o del servidor",
    "sessions":   "quien esta viendo algo ahora",
    "restart":    "reiniciar emby",
    "pending":    "lo que el usuario pidio y aun no llega",
    "reports":    "reportes o tickets abiertos",
    "users":      "usuarios vinculados",
    "stats":      "cuantas peliculas/series hay, espacio libre, estadisticas",
    "log":        "ver la cola de un log",
    "task":       "correr una tarea del pipeline",
    "help":       "ayuda, que sabe hacer el bot",
    "menu":       "menu de botones",
    "chat":       "saludo, agradecimiento o charla sin ninguna accion",
}

EMPTY = {"intent": "search", "title": None, "season": None, "lang": None, "name": None, "hours": None,
         "quality": False, "via": "none"}


def fold(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------- limpieza previa
_POLITE = re.compile(
    r"^(?:(?:hola|oye|oe|epa|ey|hey|buenas|buenos dias|buenas tardes|buenas noches|porfa|por favor|pls|please|"
    r"me puedes|me podrias|puedes|podrias|podrian|pueden|se puede|seria posible|necesito que|necesito|quiero que|"
    r"quisiera que|me gustaria que|hay que|ayudame a|ayuda a|bot|pipeflix)[,\s]+)+", re.I)
_TRAIL = re.compile(r"[\s,]*(?:por favor|porfa|porfis|pls|please|gracias|thanks|cuando puedas|si se puede|xfa)[\s.!?]*$", re.I)


def clean(text):
    t = re.sub(r"[¿¡\"“”«»]+", " ", text or "").strip()
    t = re.sub(r"\s+", " ", t)
    for _ in range(2):
        t = _POLITE.sub("", t)
        t = _TRAIL.sub("", t)
    return t.strip(" .!?,")


# ---------------------------------------------------------------- reglas
_LANG_WORDS = r"espa[nñ]ol|latino|castellano|ingl[eé]s|franc[eé]s|japon[eé]s|italiano|alem[aá]n|portugu[eé]s|brasile[nñ]o|coreano|chino|mandar[ií]n|ruso|hindi"
_SUBS = r"(?:sub(?:t[ií]tulos?|s)?|subtitulado|subtitulos)"
_ART = r"(?:los\s+|las\s+|el\s+|la\s+|unos\s+|un\s+|sus\s+)?"
_FIXV = (r"(?:arregla|arreglar|arreglen|arreglame|mejora|mejorar|repara|reparar|busca|buscar|buscame|consigue|"
         r"conseguir|consigueme|baja|bajar|bajame|descarga|descargar|revisa|revisar|corrige|corregir|sincroniza|"
         r"sincronizar|pon|poner|ponle|ponme|agrega|agregar|agregale|a[nñ]ade|a[nñ]adele|genera|generar|activa|activar|"
         r"quiero|necesito|falta|faltan|pide|pedir|pideme|dame|manda|mandar|trae|traer)\w*")
_SEASON = re.compile(r"\b(?:temporada|temp|season|t)\s*\.?\s*(\d{1,2})\b|\bs(\d{2})(?:e\d{1,3})?\b", re.I)
_DEL_MEDIA = r"(?:la\s+|el\s+)?(?:peli(?:cula)?\s+|serie\s+)?(?:de\s+)?"

_RULES = [
    # --- sistema / listas (sin titulo)
    ("restart", re.compile(r"^(?:reinicia|reiniciar|restart|resetea|reset|reboot)\w*\s+(?:el\s+|al\s+)?(?:emby|servidor|server)\s*$", re.I)),
    ("status", re.compile(r"^(?:estado|status|salud|sistema|health|pipeline|diagnostico)$|^(?:como|que tal)\s+(?:esta|va|anda|andan|van)\s+(?:todo|el sistema|el servidor|el emby|emby|la cosa|las cosas)|^(?:todo bien|hay problemas|algo (?:esta )?fallando|que (?:esta )?fall\w+)", re.I)),
    ("sessions", re.compile(r"^sesiones$|qui[eé]n(?:es)?\s+(?:esta|estan|anda|andan)\s+viendo|que\s+estan\s+viendo|viendo ahora|hay alguien viendo|alguien\s+(?:esta\s+)?viendo|hay gente viendo", re.I)),
    ("queue", re.compile(r"^(?:cola|descargas|bajadas|queue|downloads)$|que\s+(?:se\s+)?esta\w*\s+(?:bajando|descargando)|que\s+(?:hay|esta|tienes)\s+en\s+(?:la\s+)?cola|como\s+va\w*\s+(?:la|las|esa|esas)?\s*(?:descarga|cola|bajada)s?|estado\s+de\s+(?:la\s+cola|las\s+descargas)|^cuanto\s+(?:le\s+)?falta\s+(?:a|para)\s+(?:que\s+(?:baje|llegue)\s+)?(.+)$", re.I)),
    ("torrents", re.compile(r"^(?:torrents?|transmission)(?:\s+(?:estancados|parados|atascados|colgados))?$|(?:estado|lista)\s+de\s+(?:los\s+)?torrents|torrents\s+(?:estancados|parados|atascados)", re.I)),
    ("recent", re.compile(r"^(?:recientes|novedades|nuevo|lo nuevo|estrenos|lo ultimo|ultimo|que hay de nuevo|que hay nuevo)$|que\s+(?:hay\s+(?:de\s+)?)?nuevo|que\s+(?:llego|entro|se\s+agrego|subieron|subiste|agregaron|agregaste|cayo|bajo)\b|(?:lo\s+)?ultim[oa]s?\s+(?:que\s+)?(?:llego|llegaron|agregad[oa]s?|entradas)|novedades", re.I)),
    ("wanted", re.compile(r"(?:que|cuales|cuantas?)\s+(?:pelis|peliculas|series|titulos|cosas|episodios)?\s*(?:faltan?|esta[n]?\s+sin|no\s+tienen?|siguen?\s+sin)\s+(?:de\s+)?" + _SUBS + r"|" + _SUBS + r"\s+(?:faltantes|pendientes|que\s+faltan)|faltantes\s+de\s+" + _SUBS + r"|^sin\s+" + _SUBS + r"$|pendientes\s+de\s+" + _SUBS + r"|^" + _SUBS + r"$", re.I)),
    ("covers_all", re.compile(r"^(?:arregla|corrige|repara|refresca|actualiza|revisa)\w*\s+(?:las\s+)?(?:car[aá]tulas|portadas|posters|im[aá]genes)(?:\s+(?:faltantes|que\s+faltan|rotas))?$|^car[aá]tulas\s+(?:faltantes|rotas|que\s+faltan)$|^car[aá]tulas$", re.I)),
    ("pending", re.compile(r"^(?:pendientes|mis\s+pendientes|que\s+(?:he\s+)?pedi|lo\s+que\s+pedi|mis\s+peticiones|mis\s+pedidos|mis\s+solicitudes)$|que\s+(?:falta|pasa|paso)\s+con\s+(?:lo\s+que\s+pedi|mis\s+pedidos)|(?:ya\s+)?llego\s+lo\s+que\s+pedi", re.I)),
    ("reports", re.compile(r"^(?:reportes|tickets|quejas|problemas\s+reportados|reportes\s+abiertos|que\s+reportaron)$", re.I)),
    ("users", re.compile(r"^(?:usuarios|users|lista\s+de\s+usuarios|quien(?:es)?\s+(?:esta|estan)\s+vinculad\w+|cuantos\s+usuarios(?:\s+hay)?)$", re.I)),
    ("stats", re.compile(r"^(?:stats|estadisticas|numeros|resumen)$|cuant[oa]s\s+(?:pelis|peliculas|series|episodios|titulos|cosas)\s+(?:hay|tenemos|tienes)|cuanto\s+(?:espacio|disco)|espacio\s+(?:libre|queda|disponible)|tama[nñ]o\s+de\s+la\s+biblioteca", re.I)),
    ("log", re.compile(r"^(?:log|logs)\s+(?:de\s+|del\s+)?([\w.-]+)$|^(?:muestra|ver|dame|ense[nñ]a|mira)\w*\s+(?:el\s+)?log\s+(?:de\s+|del\s+)?([\w.-]+)$", re.I)),
    ("task", re.compile(r"^(?:tareas|tasks|scripts)$|^(?:corre|correr|ejecuta|ejecutar|lanza|lanzar|dispara|disparar)\s+(?:la\s+tarea\s+|el\s+script\s+|la\s+de\s+|el\s+)?([\w-]+)$", re.I)),
    ("help", re.compile(r"^(?:ayuda|help|que\s+(?:puedes|sabes)\s+hacer|como\s+funciona\w*|que\s+haces|comandos|instrucciones|que\s+te\s+puedo\s+pedir)$", re.I)),
    ("menu", re.compile(r"^(?:menu|opciones|panel|botones)$", re.I)),
    ("chat", re.compile(r"^(?:hola|buenas|hey|ey|gracias|muchas\s+gracias|ok|oka|vale|dale|listo|perfecto|genial|jaja+|jeje+|ty|thanks|thank\s+you|buenos\s+dias|buenas\s+tardes|buenas\s+noches|que\s+tal|como\s+estas|todo\s+bien|👍|🙏|❤️|si|no)$", re.I)),
    # --- sobre un titulo
    ("delete", re.compile(r"^(?:borra|borrar|borrame|elimina|eliminar|quita|quitar|quitame|saca|sacar|remueve|remover|bota|botar)\w*\s+" + _DEL_MEDIA + r"(.+)$", re.I)),
    ("cover", re.compile(r"^(?:arregla|arreglar|corrige|corregir|repara|reparar|refresca|refrescar|actualiza|actualizar|cambia|cambiar)\w*\s+(?:la\s+|el\s+)?(?:car[aá]tula|portada|poster|imagen|metadata|metadatos|info)\s+(?:de\s+|del\s+|a\s+)?(.+)$|^(?:la\s+)?(?:car[aá]tula|portada|poster)\s+de\s+(.+?)\s+(?:esta|es|sale|se\s+ve)\s+(?:mal|rota|fea|equivocada|incorrecta|de\s+otra)\w*$", re.I)),
    ("subs_bad", re.compile(r"^(?:los\s+|las\s+)?" + _SUBS + r"\s+(?:de\s+|del\s+)(.+?)\s+(?:estan|van|salen|quedan|se\s+ven|estan\s+en|son)\s+(?:mal|desincronizad|atrasad|adelantad|desfasad|corrid|fuera|pesim|horribl|malisim|fatal|en\s+ingles|en\s+otro\s+idioma|incompletos|mal\s+traducid)\w*|^(.+?)\s+tiene\s+(?:los\s+)?" + _SUBS + r"\s+(?:mal|desincronizados|en\s+ingles|malos|corridos|desfasados|mal\s+traducidos)|^(?:sincroniza|resincroniza|ajusta|alinea|cuadra)\w*\s+" + _ART + _SUBS + r"\s+(?:de\s+|del\s+)(.+)$", re.I)),
    ("translate", re.compile(r"^(?:traduce|traducir|traduceme|traduzcan|tradu[zc]\w*)\s+" + _ART + r"(?:" + _SUBS + r"\s+)?(?:de\s+|del\s+|a\s+|para\s+)?(.+)$", re.I)),
    ("subs_fix", re.compile(r"^" + _FIXV + r"\s+" + _ART + r"(?:mejores\s+)?" + _SUBS + r"(?:\s+(?:en\s+)?(?:espa[nñ]ol|es|latino|castellano))?\s+(?:de\s+|del\s+|a\s+|para\s+|en\s+)?(.+)$", re.I)),
    ("subs_fix", re.compile(r"^(.+?)\s+(?:no\s+tiene|no\s+trae|no\s+tienen|sin|le\s+faltan?|falta[n]?|no\s+le\s+salen|no\s+salen|no\s+aparecen|no\s+se\s+ven|no\s+carga[n]?|viene\s+sin|esta\s+sin)\s+" + _ART + _SUBS + r"(?:\s+(?:en\s+)?(?:espa[nñ]ol|latino|castellano))?$", re.I)),
    ("subs_fix", re.compile(r"^" + _SUBS + r"(?:\s+(?:en\s+)?(?:espa[nñ]ol|latino|castellano))?\s+(?:de\s+|del\s+|para\s+)(.+)$", re.I)),
    ("releases", re.compile(r"^(?:busca|buscar|buscame|consigue|conseguir|baja|bajar|hay|quiero|pon|poner|cambia|cambiar|reemplaza|mejora|mejorar)\w*\s+(?:otra|una|mejor|nueva|la)\s+(?:copia|version|calidad|release|descarga)\s+(?:de\s+|del\s+|para\s+)?(.+)$|^(.+?)\s+(?:en\s+)?(?:mejor\s+calidad|1080p?|4k|uhd|hd|se\s+ve\s+mal|se\s+ve\s+pixelad\w*|esta\s+en\s+mala\s+calidad)$", re.I)),
    ("add_season", re.compile(r"^(?:" + _FIXV + r"\s+)?(?:la\s+)?(?:temporada|temp|season)\s*(\d{1,2})\s+(?:de\s+|del\s+)(.+)$", re.I)),
]

_TASK_ALIASES = {"salud": "salud", "estante": "estante", "previews": "previews", "huerfanas": "huerfanas", "huerfanos": "huerfanas",
                 "torrents": "torrents", "faststart": "faststart", "pipeline": "pipeline", "traductor": "traductor",
                 "librarian": "librarian", "bot": "bot-reiniciar", "health": "pipeline"}


def _first(m):
    for g in m.groups():
        if g:
            return g.strip(" .!?¡¿,")
    return None


def parse_rules(text):
    """-> dict como EMPTY (via='rule') o None."""
    t = clean(text)
    if not t:
        return None
    for name, rx in _RULES:
        m = rx.match(t)
        if not m:
            continue
        r = dict(EMPTY, intent=name, via="rule")
        arg = _first(m)
        if name in ("delete", "cover", "subs_bad", "translate", "subs_fix", "releases"):
            r["title"] = arg
            if name == "releases":
                r["quality"] = bool(re.search(r"calidad|1080|4k|uhd|hd|pixel|se ve mal", t, re.I))
        elif name == "queue":
            r["title"] = arg  # "cuanto falta a X" -> filtra la cola por ese titulo
        elif name == "add_season":
            r["season"] = int(m.group(1))
            r["title"] = m.group(2).strip(" .!?¡¿,")
        elif name == "recent":
            r["hours"] = 24 if re.search(r"\bhoy\b", t, re.I) else 168 if re.search(r"semana", t, re.I) else 48
        elif name == "log":
            r["name"] = arg
        elif name == "task":
            key = fold(arg or "")
            r["name"] = next((v for k, v in _TASK_ALIASES.items() if k in key), None) if arg else None
        return r
    return None


# ---------------------------------------------------------------- capa 2: Claude (opcional)
NLU_MODEL = os.environ.get("PIPEFLIX_NLU_MODEL", "claude-opus-5")
_client = None
_SYSTEM = None
_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "title": {"type": ["string", "null"], "description": "nombre de la pelicula o serie tal cual la escribio la persona, sin articulos sobrantes; null si no hay"},
        "season": {"type": ["integer", "null"]},
        "lang": {"type": ["string", "null"], "enum": ["espanol", "ingles", "frances", "japones", "italiano", "aleman", "portugues", "coreano", "chino", "ruso", "hindi", None]},
        "name": {"type": ["string", "null"], "description": "nombre de log o tarea (solo intents log/task)"},
        "hours": {"type": ["integer", "null"], "description": "ventana en horas para 'recent': 24 si dijo hoy, 168 si semana, si no null"},
        "quality": {"type": "boolean", "description": "true si pide mejor calidad / otra copia por calidad"},
    },
    "required": ["intent", "title", "season", "lang", "name", "hours", "quality"],
    "additionalProperties": False,
}


def _system_prompt():
    global _SYSTEM
    if _SYSTEM is None:
        cat = "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
        _SYSTEM = ("Eres el clasificador de PIPEFLIX, un bot de Telegram para un servidor Emby casero (peliculas y series) "
                   "con Radarr, Sonarr, Bazarr (subtitulos) y un traductor propio de subtitulos en->es. La gente escribe en "
                   "espanol (a veces ingles) de forma informal. Clasifica el mensaje en UNA intencion del catalogo y extrae "
                   "el titulo, temporada e idioma si los hay. Si es solo el nombre de una pelicula o serie (o 'quiero ver X'), "
                   "es 'search'. Si duda entre search y otra cosa, prefiere search. 'subs' = subtitulos.\n\nCatalogo:\n" + cat)
    return _SYSTEM


def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=12.0, max_retries=1)
    return _client


def parse_llm(text):
    """-> dict como EMPTY (via='llm') o None si no hay clave / fallo."""
    if not llm_available():
        return None
    try:
        r = _anthropic().messages.create(
            model=NLU_MODEL, max_tokens=300,
            system=[{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text[:500]}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        if r.stop_reason == "refusal":
            return None
        raw = next(b.text for b in r.content if b.type == "text")
        d = json.loads(raw)
    except Exception as e:
        log.warning("nlu llm fallo: %s", e)
        return None
    out = dict(EMPTY, via="llm")
    for k in ("intent", "title", "season", "lang", "name", "hours", "quality"):
        if d.get(k) is not None:
            out[k] = d[k]
    if out["intent"] not in INTENTS:
        out["intent"] = "search"
    if out["intent"] == "task" and out["name"]:
        key = fold(out["name"])
        out["name"] = next((v for k, v in _TASK_ALIASES.items() if k in key), None)
    return out


_LOOKS_LIKE_REQUEST = re.compile(
    r"\?|^(?:me|puedes|podrias|quiero|quisiera|necesito|hay|como|que|cual|cuando|cuanto|donde|por que|porque|"
    r"oye|ayuda|arregla|busca|pon|baja|dame|dime|muestra|revisa|corre|ejecuta|borra|quita|traduce|reinicia|"
    r"cambia|manda|envia|avisa|no\s|se\s|el\s|la\s|los\s|las\s|mi\s|mis\s)", re.I)


def understand(text):
    """Reglas primero; Claude si la frase parece peticion y no encajo; si no, busqueda."""
    r = parse_rules(text)
    if r:
        return r
    t = clean(text)
    if llm_available() and (len(t.split()) >= 4 or _LOOKS_LIKE_REQUEST.match(t)):
        r = parse_llm(t)
        if r:
            return r
    return dict(EMPTY, intent="search", title=None, via="none")


if __name__ == "__main__":  # smoke: python3 pipeflix_nlu.py "frase" ...
    import sys
    for s in sys.argv[1:] or ["arregla los subs de dune", "que esta bajando", "borra scary movie 2", "temporada 3 de dark",
                              "los subs de moana estan desincronizados", "que llego hoy", "merlina", "dune en frances",
                              "cuanto falta a superman", "busca mejor calidad de barbie", "log de faststart", "corre salud",
                              "quiero ver la pelicula de barbie", "the boys no tiene subtitulos", "subs de merlina",
                              "hola", "que hay nuevo esta semana", "reinicia emby", "quien esta viendo", "cuantas pelis hay"]:
        print(f"{s!r:55} -> {parse_rules(s)}")
