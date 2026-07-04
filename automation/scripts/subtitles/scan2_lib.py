# SYNCED COPY (2026-07-04) — canonico: berentendo D:\emby\subtitle-pipeline\overnight\scan2.py
# Tras editar el canonico, re-copiar aqui. Lo importa subtitle_intake_gate.py (score/non_target_line).
#!/usr/bin/env python3
"""Library-wide ES subtitle QUALITY audit v2 (capa 0 + sync del plan LIBRARY_QA_SWEEP_PLAN.md).
Pulls every .es.srt + .en.srt sibling from mubuntu in one tar stream (read-only) and scores
each ES sub: structure (leak/truncation/empties/repetition), Spain-Spanish markers, mojibake,
font tags, fansub credits, Portuguese contamination, and es<->en sync offset. Writes ranked
report.jsonl/report.md + fixlist.jsonl (subredo-compatible jobs for SEVERE/RED with EN source)."""
import argparse, json, os, re, statistics, subprocess, tarfile

MUB = "mubuntu"
ROOTS = ["/APPBOX_DATA/storage/media/tv",
         "/APPBOX_DATA/storage/media/tvanimated",
         "/APPBOX_DATA/storage/media/movies"]

def pull_tar(localtar):
    roots = " ".join("'%s'" % r for r in ROOTS)
    cmd = ("find " + roots + r" \( -name '*.es.srt' -o -name '*.en.srt' -o -name '*.en.sdh.srt' \) -print0 "
           "| tar --null -cf - --no-recursion -T -")
    with open(localtar, "wb") as f:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", MUB, cmd], stdout=f)
    return p.returncode

TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")

def parse_cues(t):
    """-> list of (start_ms, end_ms, [lines])"""
    t = t.replace("\r\n", "\n").lstrip("﻿").strip()
    cues = []
    for blk in re.split(r"\n[ \t]*\n", t):
        lines = blk.split("\n")
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = TIME.search(lines[ti])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = ((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3]
        e = ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7]
        cues.append((s, e, lines[ti + 1:]))
    return cues

EN_STOP = set("the you and that this what with have not for are was your his her they them from just about would there here when will been were".split())
ES_STOP = set("que de la el no en un por con para una los las se su le lo es esta pero como mas muy bien aqui cuando donde porque si tu yo ella esto eso nada todo".split())
TAG = re.compile(r"</?[ibu]>|</?font[^>]*>|\{[^}]*\}")
SPAIN = re.compile(r"\b(vosotr[oa]s|habéis|queréis|tenéis|sabéis|podéis|vuestr[oa]s?|coged|venid|mirad|escuchad|apetece)\b", re.I)
MOJI = re.compile(r"Ã[©³±­¡§]|â€|�")
CRED = re.compile(r"opensubtitles|addic7ed|subdivx|tusubtitulo|subscene|traducido por|sincronizado por|subt[ií]tulos por|una traducci[oó]n de|www\.|\.com\b|\.org\b", re.I)
PT = re.compile(r"\b(você|vocês|não|obrigado|isso é|coisas|nós)\b", re.I)
FONT = re.compile(r"<font", re.I)
# --- Detectores nuevos (2026-07-04, postmortem Farming Life S2: E10/E12 salieron GREEN) ---
# Scaffolding de un pase LLM escrito al SRT ("English:/Spanish:/Corrected:").
SCAFF = re.compile(r"^\s*(?:English|Spanish|Corrected|Translation|Traducci[oó]n|E[NS] cue)\s*:", re.I)
# Profanidad fuerte inyectada por el NMT donde el EN alineado no la tiene ("shite"->"mierda").
PROF_ES = re.compile(r"\b(mierda|joder|carajo|coño|cabr[oó]n|put[ao]s?|pendej[oa]|verga|chingad\w*|follar|polla|gilipollas)\b", re.I)
# formas explicitas: \w* matchearia romaji ("shite") y anularia la deteccion.
# incluye fuckin/freakin (apostrofe) y exclamaciones religiosas (christ -> "carajo" legitimo)
PROF_EN = re.compile(r"\b(shit(?:s|ty|head)?|bullshit|fuck(?:s|ing|in|ed|er|ers)?|motherfuck(?:er|ers|ing|in)?|damn(?:s|ed|it)?|goddamn(?:it|ed)?|dammit|bitch(?:es|y)?|assholes?|bastards?|crap(?:py)?|dick(?:s|head)?|cocks?|pussy|whores?|sluts?|piss(?:ed|es|ing)?|cunts?|pricks?|hell|screw(?:ed|ing|s)?|heck|darn(?:ed|it)?|dang|free?k(?:ing|in)|frigg(?:ing|in)|bloody|jeez|geez|christ|jesus|dumbass|jackass|arseholes?|wank(?:er|ing)?|bollocks|bugger)\b", re.I)
# Señal de español: tildes/eñes/aperturas bastan para descartar "idioma no objetivo".
ES_MARK = re.compile(r"[áéíóúñü¿¡]", re.I)
# Vocabulario ES sin tilde (frecuentes) + morfologia — el ES_STOP chico solo no
# alcanza: "Puedo mover mi cuerpo" no tiene NINGUNA palabra de ES_STOP.
ES_VOCAB = set(("""
a y o al del mi tu sus te me nos les ya he ha han hay ser es son era fue estar
este ese esa eso esta estos esas estas soy eres somos estoy estas esta estamos
tengo tienes tiene tenemos tienen puedo puedes puede podemos pueden quiero
quieres quiere queremos vamos voy vas va van ven haz dime dame toma mira oye
bueno buena buenos buenas bien mal mas menos mucho mucha muchos muchas poco
poca pocos pocas tan tanto tanta todos todas otro otra otros otras mismo misma
ahora luego siempre nunca hoy ayer manana aqui alli alla cerca lejos antes
despues entonces mientras durante hasta desde entre sobre bajo contra segun sin
gracias por favor senor senora senorita chico chica nino nina hombre mujer
gente persona personas amigo amiga casa pueblo mundo vida dia dias noche tarde
vez veces cosa cosas algo alguien nadie nada todo cada cual quien cuanto como
donde cuando porque para pero pues asi tal vez claro verdad cierto seguro
parece creo pienso siento espera espero necesito quiere decir dice dijo hacer
hago haces hace hicimos hecho ir voy fui ido venir vino saber se sabes sabe
ver veo ves visto dar dio dado tener tuvo poner puso salir entrar llegar pasar
paso quedar deja dejar llamar llama trabajo comer comida agua fuego tierra
aunque sea sean seas fuera demasiado nuestro nuestra nuestros nuestras tus has
hemos debes debe deber haber hijo hija conmigo contigo primer primero segundo
segunda tercer tercero uno dos tres cuatro cinco seis siete ocho nueve diez
once doce veinte cien mil joven viejo vieja nuevo nueva puerta padre madre
""").split())
ES_SUFF = re.compile(r"[a-z]{2,}(?:cion|ciones|mente|dades?|aba|aban|ando|iendo|amos|emos|aron|ieron|eria|arse|erse|irse|ad[oa]s?|id[oa]s?)\b")
SDH_LINE = re.compile(r"^[\[(♪].*[\])♪]$")

def core(s):
    return TAG.sub("", s).strip()

def englishish(s):
    w = re.findall(r"[A-Za-zÀ-ſ']+", s.lower())
    if len(w) < 2:
        return False
    en = sum(x in EN_STOP for x in w)
    es = sum(x in ES_STOP for x in w)
    return en >= 2 and en > es

def non_target_line(l, en_lines):
    """Linea que no parece ES ni EN (romaji, indonesio...) y no es passthrough
    deliberado (identica a una linea del EN fuente)."""
    if ES_MARK.search(l) or re.search(r"\d", l):
        return False
    if l == l.upper():
        return False  # carteles/signs en mayusculas: rareza de proveedor, no calidad
    if SDH_LINE.match(l.strip()):
        return False  # descripciones SDH: [suena radio policial]
    low = l.lower()
    w = re.findall(r"[a-z']+", low)
    if len(w) < 3:
        return False
    if len(set(w)) == 1:
        return False  # cantos/onomatopeya: "botas, botas, botas"
    words_orig = re.findall(r"[A-Za-zÀ-ſ']+", l)
    caps = sum(1 for x in words_orig if x[:1].isupper())
    if caps >= 2 and caps >= 0.67 * len(words_orig):
        return False  # mayoria Title Case: nombres propios
    if any(x in ES_STOP or x in ES_VOCAB or x in EN_STOP or x.rstrip("s") in ES_VOCAB
           for x in w):
        return False
    if ES_SUFF.search(low):
        return False
    return low not in en_lines

def series_of(path):
    parts = path.split("/")
    for key in ("tv", "tvanimated", "movies"):
        if key in parts:
            i = parts.index(key)
            if i + 1 < len(parts):
                return ("%s/%s" % (key, parts[i + 1]))
    return "?"

def sync_offset(es_cues, en_cues):
    """Median |delta| ms between each ES cue midpoint and the nearest EN cue midpoint."""
    if not es_cues or not en_cues:
        return None
    en_mid = sorted((s + e) // 2 for s, e, _ in en_cues)
    import bisect
    deltas = []
    for s, e, _ in es_cues:
        m = (s + e) // 2
        i = bisect.bisect_left(en_mid, m)
        best = min(abs(m - en_mid[j]) for j in (i - 1, i) if 0 <= j < len(en_mid))
        deltas.append(best)
    return statistics.median(deltas)

def bump(sev, to):
    order = ["GREEN", "YELLOW", "RED", "SEVERE"]
    return to if order.index(to) > order.index(sev) else sev

def score(es_text, en_text, es_mtime=None, en_mtime=None):
    es_cues = parse_cues(es_text)
    en_cues = parse_cues(en_text) if en_text else None
    lines = [core(l) for _, _, ls in es_cues for l in ls]
    nonempty = [l for l in lines if l]
    total = max(1, len(lines))
    flags, sev = [], "GREEN"

    if len(es_cues) < 5:
        flags.append("almost_empty(%d)" % len(es_cues)); sev = "SEVERE"
    if en_cues:
        if len(es_cues) < 0.5 * len(en_cues):
            flags.append("truncated(es=%d/en=%d)" % (len(es_cues), len(en_cues))); sev = "SEVERE"
        elif abs(len(es_cues) - len(en_cues)) > max(5, 0.1 * len(en_cues)):
            # different providers segment cues differently; alone this is NOT a quality signal
            flags.append("cue_mismatch(es=%d/en=%d)" % (len(es_cues), len(en_cues)))
            sev = bump(sev, "YELLOW")

    leak = sum(englishish(l) for l in nonempty)
    if leak / total > 0.25:
        flags.append("english_leak(%.0f%%)" % (100 * leak / total)); sev = "SEVERE"
    elif leak / total > 0.05:
        flags.append("english_leak(%.0f%%)" % (100 * leak / total)); sev = bump(sev, "RED")

    empty = sum(1 for l in lines if not l)
    if empty / total > 0.3:
        flags.append("empty(%.0f%%)" % (100 * empty / total)); sev = "SEVERE"
    elif empty / total > 0.05:
        flags.append("empty(%d)" % empty); sev = bump(sev, "YELLOW")

    rep = 0
    for l in nonempty:
        ws = l.split()
        if ws and max((ws.count(x) for x in set(ws)), default=0) >= 4:
            rep += 1
    if rep > 3:
        flags.append("repetition(%d)" % rep); sev = bump(sev, "YELLOW")

    spain = sum(1 for l in nonempty if SPAIN.search(l))
    if spain >= 3:
        flags.append("spain_es(%d)" % spain); sev = bump(sev, "RED")
    elif spain:
        flags.append("spain_es(%d)" % spain); sev = bump(sev, "YELLOW")

    moji = sum(1 for l in nonempty if MOJI.search(l))
    if moji >= 3:
        flags.append("mojibake(%d)" % moji); sev = bump(sev, "RED")

    pt = sum(1 for l in nonempty if PT.search(l))
    if pt / max(1, len(nonempty)) > 0.05:
        flags.append("portuguese(%d)" % pt); sev = bump(sev, "SEVERE")

    cred = sum(1 for l in nonempty if CRED.search(l))
    if cred:
        flags.append("credits_ads(%d)" % cred); sev = bump(sev, "YELLOW")

    fonts = sum(1 for _, _, ls in es_cues for l in ls if FONT.search(l))
    if fonts / max(1, len(es_cues)) > 0.5:
        flags.append("font_tags"); sev = bump(sev, "YELLOW")

    # scaffolding de LLM escrito al SRT (E10 Farming: 70 cues, y aun asi era GREEN)
    scaff = sum(1 for l in nonempty if SCAFF.search(l))
    if scaff >= 2:
        flags.append("scaffold(%d)" % scaff); sev = "SEVERE"
    elif scaff:
        flags.append("scaffold(%d)" % scaff); sev = bump(sev, "RED")

    # lineas en idioma no objetivo (ni ES ni EN: romaji, indonesio...) — se exime
    # el passthrough deliberado (linea identica a una del EN fuente)
    en_lines = set()
    if en_cues:
        for _, _, ls in en_cues:
            for l in ls:
                en_lines.add(core(l).lower())
    nt = sum(1 for l in nonempty if non_target_line(l, en_lines))
    if nt >= 10:
        flags.append("non_target(%d)" % nt); sev = bump(sev, "RED")
    elif nt >= 4:
        flags.append("non_target(%d)" % nt); sev = bump(sev, "YELLOW")

    # profanidad fuerte en ES sin equivalente en el cue EN alineado (NMT alucinando).
    # RED solo con fuente EN 100% limpia (Farming: karaoke romaji); si el EN trae
    # palabrotas es contenido adulto y el ES intensificado es estilo -> YELLOW.
    prof = en_prof_total = 0
    if en_cues:
        import bisect
        en_sorted = sorted(((s + e) // 2, " ".join(ls)) for s, e, ls in en_cues)
        en_mids = [m for m, _ in en_sorted]
        en_prof_total = sum(1 for _, t in en_sorted if PROF_EN.search(t))
        for s, e, ls in es_cues:
            txt = " ".join(core(l) for l in ls)
            if not PROF_ES.search(txt):
                continue
            m = (s + e) // 2
            i = bisect.bisect_left(en_mids, m)
            near = [j for j in (i - 1, i, i + 1)
                    if 0 <= j < len(en_mids) and abs(en_mids[j] - m) <= 2000]
            if near and not any(PROF_EN.search(en_sorted[j][1]) for j in near):
                prof += 1
    # RED solo corroborado (non_target/scaffold en el mismo archivo = firma NMT tipo
    # Farming). EN limpio + ES con palabrotas SOLO puede ser EN censurado (South Park)
    # con traduccion humana libre -> YELLOW.
    if prof >= 2 and en_prof_total == 0 and (nt >= 4 or scaff):
        flags.append("profanity_injected(%d)" % prof); sev = bump(sev, "RED")
    elif prof:
        flags.append("profanity_injected(%d)" % prof); sev = bump(sev, "YELLOW")

    # ES mas viejo que el EN actual en disco (churn de release -> stale/desync)
    if es_mtime and en_mtime and en_mtime > es_mtime + 3600:
        flags.append("stale_es(%.1fd)" % ((en_mtime - es_mtime) / 86400.0)); sev = bump(sev, "YELLOW")

    off = sync_offset(es_cues, en_cues) if en_cues else None
    if off is not None:
        if off > 2500:
            flags.append("sync(%.1fs)" % (off / 1000)); sev = "SEVERE"
        elif off > 1000:
            flags.append("sync(%.1fs)" % (off / 1000)); sev = bump(sev, "RED")

    return sev, flags, len(es_cues), off

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="/mnt/d/emby/subtitle-pipeline/work/scan2")
    ap.add_argument("--skip-pull", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    tarpath = os.path.join(a.workdir, "srts.tar")
    if not a.skip_pull or not os.path.exists(tarpath):
        print("pulling tar...", flush=True)
        rc = pull_tar(tarpath)
        print("pull rc=%d size=%d" % (rc, os.path.getsize(tarpath)), flush=True)

    files, mtimes = {}, {}
    with tarfile.open(tarpath, "r") as tf:
        for m in tf.getmembers():
            if m.isfile():
                p = "/" + m.name.lstrip("/")
                files[p] = tf.extractfile(m).read().decode("utf-8", "replace")
                mtimes[p] = m.mtime

    rows = []
    for path, text in sorted(files.items()):
        if not path.endswith(".es.srt"):
            continue
        base = path[:-7]
        en_path = next((base + s for s in (".en.srt", ".en.sdh.srt") if base + s in files), None)
        en = files.get(en_path) if en_path else None
        sev, flags, ncues, off = score(text, en, es_mtime=mtimes.get(path),
                                       en_mtime=mtimes.get(en_path) if en_path else None)
        rows.append({"path": path, "series": series_of(path), "sev": sev, "flags": flags,
                     "cues": ncues, "sync_ms": off, "has_en": en is not None,
                     "en_path": en_path})

    with open(os.path.join(a.workdir, "report.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # keep-local (tags Sonarr/Radarr + manuales): se reporta pero JAMAS entra al
    # fixlist — regenerar con overnight/build_keep_local.py
    keep_paths = []
    kl_file = "/mnt/d/emby/subtitle-pipeline/manifests/keep_local.json"
    if os.path.exists(kl_file):
        keep_paths = [e["path"].rstrip("/") + "/"
                      for e in json.load(open(kl_file, encoding="utf-8"))["paths"]]

    jobs, needs_en, kept = [], [], []
    for r in rows:
        if r["sev"] in ("SEVERE", "RED"):
            base = r["path"][:-7]
            job = {"ep": base.rsplit("/", 1)[-1], "series": r["series"],
                   "video": base + ".mkv", "es_remote": r["path"],
                   "old_es_remote": r["path"], "en_kind": "sidecar",
                   "en_remote": r["en_path"] or (base + ".en.srt"), "why": r["flags"]}
            if any(r["path"].startswith(k) for k in keep_paths):
                kept.append(job)
            else:
                (jobs if r["has_en"] else needs_en).append(job)
    with open(os.path.join(a.workdir, "fixlist.jsonl"), "w") as f:
        for j in jobs:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    with open(os.path.join(a.workdir, "needs_en.jsonl"), "w") as f:
        for j in needs_en:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    with open(os.path.join(a.workdir, "fixlist_keep_local.jsonl"), "w") as f:
        for j in kept:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")

    bysev = {}
    for r in rows:
        bysev[r["sev"]] = bysev.get(r["sev"], 0) + 1
    agg = {}
    for r in rows:
        d = agg.setdefault(r["series"], {"n": 0, "SEVERE": 0, "RED": 0, "YELLOW": 0, "GREEN": 0})
        d["n"] += 1; d[r["sev"]] += 1
    worst = sorted(agg.items(), key=lambda kv: (-(kv[1]["SEVERE"] * 3 + kv[1]["RED"]), kv[0]))
    with open(os.path.join(a.workdir, "report.md"), "w") as f:
        f.write("# Scan2 %s\n\ntotal es.srt=%d | %s\n\n" % (
            "-".join(ROOTS[0].split("/")[-1:]), len(rows),
            " ".join("%s=%d" % kv for kv in sorted(bysev.items()))))
        f.write("fixlist(SEVERE+RED con EN)=%d | needs_en=%d | keep_local excluidos=%d\n\n"
                % (len(jobs), len(needs_en), len(kept)))
        f.write("## Peores series (SEVERE*3+RED)\n\n| serie | n | SEV | RED | YEL |\n|---|---|---|---|---|\n")
        for s, d in worst[:40]:
            if d["SEVERE"] + d["RED"] == 0:
                break
            f.write("| %s | %d | %d | %d | %d |\n" % (s, d["n"], d["SEVERE"], d["RED"], d["YELLOW"]))
        f.write("\n## Peores archivos\n\n")
        sevrank = {"SEVERE": 0, "RED": 1}
        bad = [r for r in rows if r["sev"] in sevrank]
        bad.sort(key=lambda r: (sevrank[r["sev"]], r["series"]))
        for r in bad[:200]:
            f.write("- [%s] %s — %s\n" % (r["sev"], r["path"].rsplit("/", 1)[-1], ", ".join(r["flags"])))
    print("done: %d files, %s; fixlist=%d needs_en=%d keep_local=%d" % (
        len(rows), bysev, len(jobs), len(needs_en), len(kept)), flush=True)

if __name__ == "__main__":
    main()
