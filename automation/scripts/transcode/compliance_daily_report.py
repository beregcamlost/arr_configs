#!/usr/bin/env python3
"""FASE 5 - Correo diario del pipeline: que entro, que se ajusto solo, que esta
oculto ahora mismo y hace cuanto, y que fallo.

Lee todo de la vista compliance_state (FASE 2) y de conversion_runs. No inventa
estado propio: si algo no aparece aqui es porque no aparece en el contrato.

Uso:
  compliance_daily_report.py            envia el correo
  compliance_daily_report.py --dry-run  lo imprime y no envia
"""
import argparse
import html
import os
import sqlite3
import subprocess
import sys
import time

STATE_DIR = os.environ.get("STATE_DIR", "/APPBOX_DATA/storage/.transcode-state-media")
DB_PATH = os.environ.get("COMPLIANCE_DB", os.path.join(STATE_DIR, "library_codec_state.db"))
SENDMAIL = "/config/berenstuff/automation/scripts/bin/sendmail.py"
DEST = os.environ.get("COMPLIANCE_REPORT_TO", "antoniojose.figueroaf@gmail.com")
BODY_FILE = "/tmp/compliance_daily_body.html"
LOG_PATH = "/config/berenstuff/automation/logs/compliance_daily_report.log"
MEDIA_ROOT = "/APPBOX_DATA/storage/media/"


def log(msg):
    line = "%s [informe-diario] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def one(con, sql, args=(), default=0):
    r = con.execute(sql, args).fetchone()
    return default if r is None or r[0] is None else r[0]


def corto(path, n=58):
    p = path.replace(MEDIA_ROOT, "")
    return p if len(p) <= n else "..." + p[-(n - 3):]


def fila(icono, texto, dato):
    return (
        '<tr>'
        '<td style="padding:7px 10px;border-bottom:1px solid #e5e7eb">%s %s</td>'
        '<td style="padding:7px 10px;border-bottom:1px solid #e5e7eb;text-align:right">'
        '<b>%s</b></td></tr>' % (icono, html.escape(texto), html.escape(str(dato)))
    )


def recoger():
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    d = {}

    d["total"] = one(con, "SELECT COUNT(*) FROM compliance_state")
    d["conforme"] = one(con, "SELECT COUNT(*) FROM compliance_state WHERE estado='conforme'")
    d["aparcado"] = one(con, "SELECT COUNT(*) FROM compliance_state WHERE estado='aparcado'")
    d["encolado"] = one(con, "SELECT COUNT(*) FROM compliance_state WHERE estado IN ('encolado','trabajando')")
    d["fallido"] = one(con, "SELECT COUNT(*) FROM compliance_state WHERE estado='fallido'")
    d["eta"] = one(con, "SELECT COALESCE(ROUND(SUM(eta_min)),0) FROM compliance_state "
                        "WHERE estado IN ('encolado','trabajando')")

    # que entro en 24 h
    d["entraron"] = one(con,
        "SELECT COUNT(*) FROM media_files WHERE deleted_at IS NULL "
        "AND mtime > strftime('%s','now','-1 day')")

    # que se ajusto solo en 24 h
    d["ajustados"] = one(con,
        "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped' "
        "AND end_ts > datetime('now','-1 day')")
    d["fallos24"] = one(con,
        "SELECT COUNT(*) FROM conversion_runs WHERE status IN ('failed','attempt_limit_reached') "
        "AND COALESCE(end_ts,start_ts) > datetime('now','-1 day')")

    # que esta oculto AHORA
    try:
        d["ocultos"] = q(con,
            "SELECT path, tag, tagged_ts, "
            "  ROUND((julianday('now')-julianday(tagged_ts))*24.0,1) AS horas "
            "FROM emby_tag_state ORDER BY tagged_ts")
    except sqlite3.Error:
        d["ocultos"] = []

    # lo que fallo y sigue fallido
    d["fallidos"] = q(con,
        "SELECT path, motivo FROM compliance_state WHERE estado='fallido' LIMIT 8")

    # la 3090
    try:
        r = con.execute("SELECT estado, nota, "
                        "ROUND((julianday('now')-julianday(last_seen))*24.0,1) AS horas "
                        "FROM worker_heartbeat WHERE worker='gpu3090'").fetchone()
        d["gpu"] = dict(r) if r else None
    except sqlite3.Error:
        d["gpu"] = None

    con.close()
    return d


def construir(d):
    pct = round(100.0 * d["conforme"] / d["total"], 1) if d["total"] else 0.0
    n_ocultos = sum(1 for r in d["ocultos"] if r["tag"] == "ARREGLANDO")
    n_marcados = len(d["ocultos"]) - n_ocultos

    if d["fallido"] or d["fallos24"]:
        veredicto = "Hay %d fallo(s) que necesitan tu ojo." % (d["fallido"] or d["fallos24"])
        color = "#b91c1c"
    elif d["encolado"]:
        veredicto = "Todo en orden. %d archivo(s) en cola, ~%d min por delante." % (d["encolado"], d["eta"])
        color = "#4f46e5"
    else:
        veredicto = "Todo conforme o aparcado a proposito. Nada pendiente."
        color = "#15803d"

    filas = [
        fila("📥", "Entraron en 24 h", d["entraron"]),
        fila("🔧", "Se ajustaron solos", d["ajustados"]),
        fila("🙈" if n_ocultos else "✅", "Ocultos ahora mismo", n_ocultos),
        fila("🏷️", "Visibles pero marcados", n_marcados),
        fila("🔴" if d["fallido"] else "✅", "Fallidos sin resolver", d["fallido"]),
        fila("📊", "Conformes", "%d de %d (%s%%)" % (d["conforme"], d["total"], pct)),
        fila("🅿️", "Aparcados por decision tuya", d["aparcado"]),
    ]

    secciones = []

    if d["ocultos"]:
        lineas = []
        for r in d["ocultos"][:8]:
            aviso = " ⚠️" if r["tag"] == "ARREGLANDO" and r["horas"] and r["horas"] > 3 else ""
            lineas.append("%s · %s h · %s%s" % (r["tag"], r["horas"], corto(r["path"]), aviso))
        secciones.append(("🙈 Que esta tapado ahora", "<br>".join(html.escape(x) for x in lineas)))

    if d["fallidos"]:
        lineas = ["%s — %s" % (corto(r["path"]), r["motivo"]) for r in d["fallidos"]]
        secciones.append(("🔴 Fallidos", "<br>".join(html.escape(x) for x in lineas)))

    if d["gpu"]:
        g = d["gpu"]
        if g["horas"] is not None and g["horas"] > 24:
            txt = "No reporta hace <b>%s h</b>. Si se acumula trabajo pesado, ahi se queda." % g["horas"]
        else:
            txt = "Vista hace <b>%s h</b> — %s. %s" % (
                g["horas"], html.escape(g["estado"] or "?"), html.escape(g["nota"] or ""))
        secciones.append(("🎮 La 3090", txt))
    else:
        secciones.append(("🎮 La 3090", "Sin latido registrado todavia."))

    bloques = "".join(
        '<h3 style="font-size:14px;margin:18px 0 6px;color:#4f46e5">%s</h3>'
        '<p style="font-size:14px;margin:0;line-height:1.5">%s</p>' % (t, c)
        for t, c in secciones
    )

    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;'
        'margin:0 auto;color:#1f2937">'
        '<div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);'
        'border-radius:12px 12px 0 0;padding:18px 22px">'
        '<span style="color:#fff;font-size:18px;font-weight:700">🎬 Pipeline Emby — %s</span></div>'
        '<div style="background:#f9fafb;border:1px solid #e5e7eb;border-top:0;'
        'border-radius:0 0 12px 12px;padding:20px 22px">'
        '<p style="font-size:15px;margin:0 0 14px;color:%s"><b>%s</b></p>'
        '<table style="width:100%%;border-collapse:collapse;font-size:14px">%s</table>'
        '%s'
        '<p style="font-size:13px;color:#6b7280;margin:18px 0 0">— Claudia 💜</p>'
        '</div></div>'
        % (time.strftime("%d/%m"), color, html.escape(veredicto), "".join(filas), bloques)
    )


def asunto(d):
    if d["fallido"] or d["fallos24"]:
        return "mubuntu: %d fallos en el pipeline" % (d["fallido"] or d["fallos24"])
    if d["encolado"]:
        return "mubuntu: %d en cola, resto conforme" % d["encolado"]
    return "mubuntu: todo conforme, nada pendiente"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    d = recoger()
    cuerpo = construir(d)
    subj = asunto(d)

    if args.dry_run:
        print("ASUNTO:", subj)
        print(cuerpo)
        return 0

    with open(BODY_FILE, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(cuerpo)

    r = subprocess.run(
        ["python3", SENDMAIL, "--to", DEST, "--subject", subj,
         "--body-file", BODY_FILE, "--html"],
        capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        log("ERROR al enviar (rc=%d): %s" % (r.returncode, (r.stderr or "").strip()[:300]))
        return r.returncode
    log("enviado: %s" % subj)
    return 0


if __name__ == "__main__":
    sys.exit(main())
