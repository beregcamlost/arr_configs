#!/usr/bin/env python3
"""Tandas 3090 (2026-07-05, Beren eligio 3090 para los ~210 needs_transcode).
Pull -> NVENC h264-8bit / AAC estereo 48k (= compliance del audit de mubuntu) ->
de-embed subs de texto en/es a sidecars, strip texto, conserva subs imagen ->
drop dubs que no sean idioma original ni es -> valida -> push atomico con backup
fuera de la biblioteca -> verifica -> rescan Sonarr/Radarr/Bazarr por titulo.

2026-08-11 (mejora C): deja de ser una tanda manual.
  - CLAIM en la DB de mubuntu (conversion_plan.claimed_by = 'gpu3090@<epoch>'):
    mientras el 3090 tiene un archivo, el cron de mubuntu no lo toca (su query
    solo acepta claimed_by IS NULL o 'mubuntu'). Se libera en exito, skip y fallo.
  - WRITE-BACK: tras el swap remoto inserta conversion_runs status='swapped',
    actualiza media_files (path/size/mtime/container) y BORRA probe_streams para
    que el audit vuelva a probar el archivo real (mismo principio que el fix B).
  - Solo toma reason='needs_transcode' (video pesado = NVENC); el audio_only
    barato se lo deja a mubuntu.
  - --auto: guard de GPU (no corre si estas jugando) + limite por corrida.
"""
import fcntl, json, os, re, shlex, subprocess, sys, time

MUB = "mubuntu"
STAGE_IN = "/mnt/d/emby/transcode3090/in"
STAGE_OUT = "/mnt/d/emby/transcode3090/out"
LOG = "/mnt/d/emby/transcode3090/tanda_%s.log" % time.strftime("%Y%m%d")
RBAK = "/APPBOX_DATA/storage/.transcode-3090-backups"
RDB = "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
CLAIM = "gpu3090@"
CLAIM_MAX_AGE_SEC = 21600   # 6 h: mismo umbral que release_stale_3090_claims.sh en mubuntu
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
LANG_MAP = {"English": "eng", "Japanese": "jpn", "Spanish": "spa", "Korean": "kor",
            "Chinese": "zho", "French": "fra", "German": "deu", "Italian": "ita",
            "Portuguese": "por", "Danish": "dan", "Swedish": "swe", "Norwegian": "nor"}
ES = {"es", "spa"}
MIN_FREE_GB = 60
AUTO_LIMIT = 4          # archivos por corrida en modo --auto
GPU_UTIL_MAX = 25       # % utilizacion: por encima = la GPU esta ocupada (juego)
GPU_MEM_MAX_MB = 8000   # MB en uso: por encima = hay un juego cargado (el escritorio
                        # normal de Windows ya ocupa 2-4 GB, por eso el umbral es alto)


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def ssh(cmd, timeout=300):
    return subprocess.run(["ssh", MUB, cmd], capture_output=True, text=True, timeout=timeout)


def rq(p):  # remote shell quote
    return shlex.quote(p)


def sq(s):  # sqlite string literal quote
    return "'" + str(s).replace("'", "''") + "'"


def dbq(sql, ro=False, timeout=120):
    """Ejecuta SQL en la DB de estado de mubuntu (por ssh)."""
    target = ("'file:%s?mode=ro'" % RDB) if ro else rq(RDB)
    r = ssh("sqlite3 -cmd '.timeout 20000' %s %s" % (target, rq(sql)), timeout=timeout)
    if r.returncode != 0:
        log("WARN sqlite rc=%d: %s" % (r.returncode, (r.stderr or "").strip()[:200]))
    return r


def gpu_state(samples=3, gap=4):
    """(util %, mem MB) del 3090 = peor de N muestras (un pico aislado no cuenta como
    'libre', y un frame suelto del escritorio no cuenta como 'ocupada')."""
    utils, mems = [], []
    for i in range(samples):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30)
            u, m = [int(x.strip()) for x in r.stdout.strip().splitlines()[0].split(",")]
            utils.append(u); mems.append(m)
        except Exception as e:
            log("WARN nvidia-smi: %s" % e)
            return -1, -1
        if i < samples - 1:
            time.sleep(gap)
    return max(utils), max(mems)


def heartbeat(estado, util=None, mem=None, nota=""):
    """Deja constancia en mubuntu de que la 3090 estuvo viva y que decidio.

    POR QUE: mubuntu no puede alcanzar a berentendo por HTTP — es la 3090 la que viene
    a buscar trabajo. Sin esto, "la 3090 lleva tres dias apagada" y "la 3090 mira cada
    hora y no hay nada que hacer" se ven exactamente igual desde mubuntu: cero claims.
    Con el latido, pipeline_health y el correo diario distinguen una de otra.
    """
    try:
        dbq("CREATE TABLE IF NOT EXISTS worker_heartbeat ("
            "worker TEXT PRIMARY KEY, last_seen TEXT, estado TEXT, "
            "gpu_util INTEGER, gpu_mem_mb INTEGER, nota TEXT);")
        dbq("INSERT INTO worker_heartbeat(worker,last_seen,estado,gpu_util,gpu_mem_mb,nota) "
            "VALUES('gpu3090',CURRENT_TIMESTAMP,%s,%s,%s,%s) "
            "ON CONFLICT(worker) DO UPDATE SET last_seen=CURRENT_TIMESTAMP, "
            "estado=excluded.estado, gpu_util=excluded.gpu_util, "
            "gpu_mem_mb=excluded.gpu_mem_mb, nota=excluded.nota;"
            % (sq(estado), int(util) if util is not None else "NULL",
               int(mem) if mem is not None else "NULL", sq(nota)))
    except Exception as e:
        log("WARN latido no enviado: %s" % e)


def release_claims():
    """Libera SOLO los claims nuestros que quedaron colgados (mas de CLAIM_MAX_AGE_SEC).

    BUG 2026-08-11: liberaba todos los 'gpu3090@%' sin mirar la edad. Cuando la tarea
    programada disparo una segunda instancia mientras la primera estaba bajando un
    archivo, esa segunda corrida borro el claim EN VUELO y el cron de mubuntu se puso
    a convertir la misma pelicula en CPU (2 vCPU, horas) en paralelo con el 3090.
    """
    cutoff = int(time.time()) - CLAIM_MAX_AGE_SEC
    r = dbq("UPDATE conversion_plan SET claimed_by=NULL WHERE claimed_by LIKE 'gpu3090@%%' "
            "AND CAST(substr(claimed_by,9) AS INTEGER) < %d; SELECT changes();" % cutoff)
    n = (r.stdout or "0").strip().splitlines()[-1] if r.stdout.strip() else "0"
    if n not in ("0", ""):
        log("claims viejos liberados: %s" % n)


def claim(mid):
    """Marca la fila como nuestra. True si la tomamos (nadie mas la tenia)."""
    if not mid:
        return True
    tag = CLAIM + str(int(time.time()))
    r = dbq("UPDATE conversion_plan SET claimed_by=%s WHERE media_id=%d AND claimed_by IS NULL; SELECT changes();"
            % (sq(tag), int(mid)))
    return (r.stdout or "").strip().splitlines()[-1:] == ["1"]


def unclaim(mid):
    if mid:
        dbq("UPDATE conversion_plan SET claimed_by=NULL WHERE media_id=%d;" % int(mid))


def writeback(mid, newpath, oldpath):
    """Tras el swap: registra el run, sincroniza media_files y borra los probes viejos
    para que el audit de mubuntu vuelva a probar el archivo REAL (fix del fantasma)."""
    if not mid:
        return
    st = ssh("stat -c '%s %Y' " + rq(newpath))
    try:
        size, mtime = st.stdout.split()
    except ValueError:
        log("WARN writeback: no pude stat %s" % newpath)
        size, mtime = "0", "0"
    cont = newpath.rsplit(".", 1)[-1].lower()
    run_id = "run_3090_" + time.strftime("%Y%m%d_%H%M%S")
    sql = "\n".join([
        "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error)",
        "VALUES(%s,%d,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'swapped',1,'');" % (sq(run_id), int(mid)),
        # media_files.path es UNIQUE: si ya existe otra fila con la ruta nueva
        # (resto de un remux anterior), la aparto antes de mover la nuestra.
        "UPDATE media_files SET path=path||'.replaced-3090-%d', deleted_at=CURRENT_TIMESTAMP"
        % int(time.time()),
        "  WHERE path=%s AND id<>%d;" % (sq(newpath), int(mid)),
        "UPDATE media_files SET path=%s, size_bytes=%s, mtime=%s, container=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%d;"
        % (sq(newpath), size, mtime, sq(cont), int(mid)),
        "DELETE FROM probe_streams WHERE media_id=%d;" % int(mid),
        "UPDATE conversion_plan SET eligible=0, priority=99, skip_reason='reaudit_after_3090',",
        "  claimed_by=NULL, plan_ts=CURRENT_TIMESTAMP WHERE media_id=%d;" % int(mid),
    ])
    r = dbq(sql)
    if r.returncode == 0:
        log("   DB write-back ok (media_id=%s)" % mid)
    else:
        log("   WARN write-back fallo media_id=%s — el audit de las 03:00 lo corrige" % mid)
        unclaim(mid)


def worklist(limit=0):
    """needs_transcode (video pesado) sin claim y sin conversion previa exitosa,
    mas nuevo primero (= lo que Beren acaba de bajar)."""
    q = ("SELECT cp.media_id || x'09' || m.path || x'09' || m.size_bytes || x'09' || m.media_type "
         "FROM conversion_plan cp JOIN media_files m ON m.id=cp.media_id "
         "LEFT JOIN (SELECT media_id, MAX(id) mx FROM conversion_runs GROUP BY media_id) r "
         "  ON r.media_id=cp.media_id "
         "LEFT JOIN conversion_runs cr ON cr.id=r.mx "
         "WHERE cp.eligible=1 AND cp.reason='needs_transcode' AND m.deleted_at IS NULL "
         "AND cp.claimed_by IS NULL "
         "AND COALESCE(cr.status,'') NOT IN ('swapped','running','attempt_limit_reached') "
         "ORDER BY COALESCE(NULLIF(m.added_ts,0), m.mtime) DESC")
    if limit:
        q += " LIMIT %d" % limit
    q += ";"
    r = dbq(q, ro=True)
    items = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            items.append({"mid": int(parts[0]), "path": parts[1],
                          "size": int(parts[2] or 0), "type": parts[3]})
    return items


def arr_maps():
    """path-prefix -> (originalLang iso, arr, id) desde Sonarr/Radarr."""
    m = {}
    r = ssh('source /config/berenstuff/.env >/dev/null 2>&1; curl -s -H "X-Api-Key: $SONARR_KEY" ${SONARR_URL:-http://127.0.0.1:8989/sonarr}/api/v3/series')
    try:
        for s in json.loads(r.stdout):
            m[s["path"].rstrip("/")] = (LANG_MAP.get((s.get("originalLanguage") or {}).get("name", ""), "eng"), "sonarr", s["id"])
    except Exception as e:
        log("WARN sonarr map: %s" % e)
    r = ssh('source /config/berenstuff/.env >/dev/null 2>&1; curl -s -H "X-Api-Key: $RADARR_KEY" ${RADARR_URL:-http://127.0.0.1:7878/radarr}/api/v3/movie')
    try:
        for s in json.loads(r.stdout):
            m[s["path"].rstrip("/")] = (LANG_MAP.get((s.get("originalLanguage") or {}).get("name", ""), "eng"), "radarr", s["id"])
    except Exception as e:
        log("WARN radarr map: %s" % e)
    return m


def title_meta(path, amap):
    p = os.path.dirname(path)
    while p and p != "/":
        if p.rstrip("/") in amap:
            return amap[p.rstrip("/")]
        p = os.path.dirname(p)
    return ("eng", None, None)


def probe(f):
    r = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_streams", "-show_format", f],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


def free_gb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def build_cmd(inf, outf, info, orig):
    v = [s for s in info["streams"] if s["codec_type"] == "video" and s.get("disposition", {}).get("attached_pic", 0) == 0]
    a = [s for s in info["streams"] if s["codec_type"] == "audio"]
    subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    if not v or not a:
        return None, "sin video o audio"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", inf]
    vs = v[0]
    cmd += ["-map", "0:%d" % vs["index"]]
    if vs.get("codec_name") == "h264" and vs.get("pix_fmt") == "yuv420p":
        vc = ["-c:v", "copy"]
    else:
        vc = ["-c:v", "h264_nvenc", "-profile:v", "high", "-pix_fmt", "yuv420p", "-preset", "p5", "-cq", "19"]
    cmd += vc
    keep = []
    tagged_orig = [s for s in a if s.get("tags", {}).get("language", "und") == orig]
    for s in a:
        lang = s.get("tags", {}).get("language", "und")
        if lang == orig or lang in ES or (lang == "und" and not tagged_orig):
            keep.append(s)
    if not keep:
        keep = [a[0]]
    ai = 0
    for s in keep:
        cmd += ["-map", "0:%d" % s["index"]]
        if s.get("codec_name") == "aac" and int(s.get("channels", 8)) <= 2 and s.get("sample_rate") == "48000":
            cmd += ["-c:a:%d" % ai, "copy"]
        else:
            cmd += ["-c:a:%d" % ai, "aac", "-ac:a:%d" % ai, "2", "-ar:a:%d" % ai, "48000", "-b:a:%d" % ai, "192k"]
        ai += 1
    si = 0
    for s in subs:
        if s.get("codec_name") not in TEXT_SUBS:
            cmd += ["-map", "0:%d" % s["index"], "-c:s:%d" % si, "copy"]
            si += 1
    cmd += ["-map_chapters", "0", outf]
    dropped = len(a) - len(keep)
    return cmd, "v=%s a_keep=%d a_drop=%d img_subs=%d" % (vc[1], len(keep), dropped, si)


def extract_sidecars(inf, info, base_remote):
    """Extrae subs de texto en/es a sidecars locales; devuelve [(local, remote)]."""
    outs = []
    for s in info["streams"]:
        if s["codec_type"] != "subtitle" or s.get("codec_name") not in TEXT_SUBS:
            continue
        lang2 = {"eng": "en", "spa": "es"}.get(s.get("tags", {}).get("language", ""), None)
        if not lang2:
            continue
        disp = s.get("disposition", {})
        suffix = lang2 + (".forced" if disp.get("forced") else "") + (".sdh" if disp.get("hearing_impaired") else "")
        remote = "%s.%s.srt" % (base_remote, suffix)
        r = ssh("test -f %s && echo YES || echo NO" % rq(remote))
        if r.stdout.strip() == "YES":
            continue
        local = os.path.join(STAGE_OUT, "sc_%d_%s.srt" % (s["index"], suffix))
        rr = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", inf,
                             "-map", "0:%d" % s["index"], "-c:s", "srt", "-f", "srt", local],
                            capture_output=True, text=True)
        if rr.returncode == 0 and os.path.exists(local) and os.path.getsize(local) > 500:
            outs.append((local, remote))
    return outs


def dur(info):
    try:
        return float(info["format"]["duration"])
    except Exception:
        return -1


def real_video_end(f):
    """Ultimo PTS de video = fin real del contenido (format.duration a veces viene inflada)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0", f],
                       capture_output=True, text=True)
    vals = [float(x) for x in r.stdout.split() if x.strip()]
    return vals[-1] if vals else -1


def process(item, amap):
    path = item["path"]
    mid = item.get("mid")
    name = os.path.basename(path)
    log("== %s (%.1f GB)" % (name, item["size"] / 1e9))
    if free_gb("/mnt/d") < MIN_FREE_GB:
        log("STOP: disco local < %d GB libres" % MIN_FREE_GB)
        return "stop"
    if not claim(mid):
        log("SKIP: otro worker ya lo tiene (claim)")
        return "skip"
    st = ssh("stat -c '%s %Y' " + rq(path))
    if st.returncode != 0:
        log("SKIP: no existe remoto")
        unclaim(mid)
        return "skip"
    size0, mtime0 = st.stdout.split()
    # Pre-chequeo remoto: el plan usa probes del audit de las 03:00 — si el cron de
    # mubuntu ya convirtio este archivo hoy, saltarlo sin gastar transferencia.
    rp = ssh("ffprobe -v error -show_entries stream=codec_name,codec_type,channels,sample_rate,pix_fmt -of csv=p=0 " + rq(path))
    rows = [l.split(",") for l in rp.stdout.splitlines() if l]
    v_ok = any(r[0] == "h264" and r[1] == "video" and "yuv420p" in r for r in rows)
    # csv de ffprobe para audio: codec_name,codec_type,sample_rate,channels
    a_bad = [r for r in rows if len(r) > 1 and r[1] == "audio" and not (r[0] == "aac" and len(r) > 3 and int(r[3] or 9) <= 2)]
    text_sub = [r for r in rows if len(r) > 1 and r[1] == "subtitle" and r[0] in TEXT_SUBS]
    if v_ok and rows and not a_bad and not text_sub:
        log("SKIP: ya compliant en remoto (probe del plan stale)")
        unclaim(mid)
        return "skip"
    orig, arr, arrid = title_meta(path, amap)
    inf = os.path.join(STAGE_IN, name)
    r = subprocess.run(["scp", "-q", MUB + ":" + path, inf])
    if r.returncode != 0:
        log("FAIL scp pull")
        unclaim(mid)
        return "fail"
    info = probe(inf)
    base_remote = re.sub(r"\.[^.]+$", "", path)
    outf = os.path.join(STAGE_OUT, re.sub(r"\.[^.]+$", "", name) + ".mkv")
    cmd, desc = build_cmd(inf, outf, info, orig)
    if not cmd:
        log("SKIP: %s" % desc)
        os.remove(inf)
        unclaim(mid)
        return "skip"
    log("   plan: %s orig=%s" % (desc, orig))
    sidecars = extract_sidecars(inf, info, base_remote)
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log("FAIL ffmpeg: %s" % r.stderr[-300:])
        os.remove(inf)
        unclaim(mid)
        return "fail"
    oinfo = probe(outf)
    d0, d1 = dur(info), dur(oinfo)
    ok_v = any(s.get("codec_name") == "h264" and s.get("pix_fmt") == "yuv420p" for s in oinfo["streams"] if s["codec_type"] == "video")
    ok_a = all(s.get("codec_name") == "aac" and int(s.get("channels", 9)) <= 2 for s in oinfo["streams"] if s["codec_type"] == "audio")
    no_text = not any(s.get("codec_name") in TEXT_SUBS for s in oinfo["streams"] if s["codec_type"] == "subtitle")
    dur_ok = d0 > 0 and abs(d0 - d1) <= 2
    if ok_v and ok_a and no_text and not dur_ok:
        # format.duration del source a veces viene inflada (padding de contenedor);
        # aceptar si el output cuadra con el fin REAL del video del source (ultimo PTS).
        real0 = real_video_end(inf)
        if real0 > 0 and abs(real0 - d1) <= 2:
            log("   nota: source dur inflada %.1f, real %.1f, output %.1f -> OK" % (d0, real0, d1))
            dur_ok = True
    if not (ok_v and ok_a and no_text and dur_ok):
        log("FAIL validacion: v=%s a=%s notext=%s dur=%.1f/%.1f" % (ok_v, ok_a, no_text, d0, d1))
        os.remove(inf); os.remove(outf)
        unclaim(mid)
        return "fail"
    st2 = ssh("stat -c '%s %Y' " + rq(path))
    if st2.stdout.split() != [size0, mtime0]:
        log("SKIP: el remoto cambio durante la conversion (carrera con el cron)")
        os.remove(inf); os.remove(outf)
        unclaim(mid)
        return "skip"
    newremote = base_remote + ".mkv"
    tmpremote = newremote + ".tmp-3090"
    r = subprocess.run(["scp", "-q", outf, MUB + ":" + tmpremote])
    if r.returncode != 0:
        log("FAIL scp push")
        os.remove(inf); os.remove(outf)
        unclaim(mid)
        return "fail"
    bak = RBAK + "/" + name
    mv = ("mkdir -p %s && mv %s %s && mv %s %s" %
          (rq(RBAK), rq(path), rq(bak), rq(tmpremote), rq(newremote)))
    r = ssh(mv)
    if r.returncode != 0:
        log("FAIL swap remoto: %s" % r.stderr[-200:])
        unclaim(mid)
        return "fail"
    rv = ssh("ffprobe -v error -show_entries format=duration -of csv=p=0 " + rq(newremote))
    if not rv.stdout.strip() or abs(float(rv.stdout.strip()) - d1) > 2:
        log("ROLLBACK: verify remoto fallo")
        # BUGFIX (2026-07-28): si el source era .mkv, newremote == path, y el viejo
        # "mv bak path && rm newremote" restauraba y ACTO SEGUIDO borraba el archivo.
        if os.path.normpath(newremote) != os.path.normpath(path):
            ssh("rm -f %s; mv -f %s %s" % (rq(newremote), rq(bak), rq(path)))
        else:
            ssh("mv -f %s %s" % (rq(bak), rq(path)))
        unclaim(mid)
        return "fail"
    for local, remote in sidecars:
        subprocess.run(["scp", "-q", local, MUB + ":" + remote])
        os.remove(local)
    os.remove(inf); os.remove(outf)
    writeback(mid, newremote, path)
    log("   OK %.0f s -> %s (+%d sidecars) bak listo" % (time.time() - t0, os.path.basename(newremote), len(sidecars)))
    return (arr, arrid)


def notify(touched):
    bz = "source /config/berenstuff/.env >/dev/null 2>&1; KEY=$(grep -oP 'apikey: *\\K\\S+' /opt/bazarr/data/config/config.yaml | head -1); "
    for arr, arrid in touched:
        if not arrid:
            continue
        if arr == "sonarr":
            ssh('source /config/berenstuff/.env >/dev/null 2>&1; curl -s -o /dev/null -X POST -H "X-Api-Key: $SONARR_KEY" -H "Content-Type: application/json" -d \'{"name":"RescanSeries","seriesId":%d}\' ${SONARR_URL:-http://127.0.0.1:8989/sonarr}/api/v3/command' % arrid)
            ssh(bz + 'curl -s -o /dev/null -X PATCH -H "X-API-KEY: $KEY" "${BAZARR_URL:-http://127.0.0.1:6767/bazarr}/api/series?seriesid=%d&action=scan-disk"' % arrid)
        else:
            ssh('source /config/berenstuff/.env >/dev/null 2>&1; curl -s -o /dev/null -X POST -H "X-Api-Key: $RADARR_KEY" -H "Content-Type: application/json" -d \'{"name":"RescanMovie","movieId":%d}\' ${RADARR_URL:-http://127.0.0.1:7878/radarr}/api/v3/command' % arrid)
            ssh(bz + 'curl -s -o /dev/null -X PATCH -H "X-API-KEY: $KEY" "${BAZARR_URL:-http://127.0.0.1:6767/bazarr}/api/movies?radarrid=%d&action=scan-disk"' % arrid)


def main():
    # Una sola corrida a la vez: la tarea programada puede disparar otra instancia
    # mientras esta sigue bajando un archivo (paso el 2026-08-11).
    lock_fh = open("/tmp/tanda3090.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("SKIP corrida: ya hay otra en curso (lock)")
        return

    os.makedirs(STAGE_IN, exist_ok=True)
    os.makedirs(STAGE_OUT, exist_ok=True)
    argv = sys.argv[1:]
    auto = "--auto" in argv
    force = "--force" in argv
    argv = [a for a in argv if a not in ("--auto", "--force")]

    if auto and not force:
        util, mem = gpu_state()
        if util >= GPU_UTIL_MAX or mem >= GPU_MEM_MAX_MB:
            log("SKIP corrida: GPU ocupada (util=%s%% mem=%sMB) — probablemente estas jugando" % (util, mem))
            heartbeat("gpu_ocupada", util, mem, "no tomo trabajo: la tarjeta esta en uso")
            return

    if len(argv) > 1 and argv[0] == "--paths":
        items = [{"path": l.strip(), "size": 0, "type": "?", "mid": None} for l in open(argv[1]) if l.strip()]
        limit = 0
    else:
        limit = int(argv[0]) if argv else (AUTO_LIMIT if auto else 0)
        release_claims()
        items = worklist(limit)
    log("worklist: %d needs_transcode (mas nuevo primero)%s" % (len(items), " limit=%d" % limit if limit else ""))
    if not items:
        log("nada que hacer")
        heartbeat("libre", nota="viva y disponible, cola de transcode vacia")
        return
    heartbeat("trabajando", nota="%d archivos tomados en esta corrida" % len(items[: limit or len(items)]))
    amap = arr_maps()
    log("arr map: %d titulos" % len(amap))
    done = fails = 0
    touched = set()
    for it in items[: limit or len(items)]:
        try:
            res = process(it, amap)
        except Exception as e:
            log("EXC %s: %s" % (it["path"], e))
            unclaim(it.get("mid"))
            res = "fail"
        if res == "stop":
            break
        if res == "fail":
            fails += 1
            if fails >= 5:
                log("STOP: 5 fallas acumuladas")
                break
        elif isinstance(res, tuple):
            done += 1
            touched.add(res)
        if auto and not force:
            util, mem = gpu_state()
            if util >= GPU_UTIL_MAX or mem >= GPU_MEM_MAX_MB:
                log("STOP: la GPU se ocupo (util=%s%% mem=%sMB) — dejo el resto para la proxima" % (util, mem))
                break
    notify(touched)
    log("TANDA DONE ok=%d fails=%d titulos_notificados=%d" % (done, fails, len(touched)))


if __name__ == "__main__":
    main()
