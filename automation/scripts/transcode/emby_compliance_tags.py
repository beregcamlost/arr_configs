#!/usr/bin/env python3
"""FASE 3 - Emby refleja el estado de conformidad.

Regimen hibrido decidido por Beren (2026-08-21), por TIPO DE TRABAJO:

  ARREGLANDO  trabajo corto (remux / de-embed, -c copy)  -> se OCULTA hasta que termine
  PENDIENTE   trabajo largo (transcode real)             -> se VE, pero queda marcado

El ocultamiento se hace con Policy.BlockedTags por usuario: no mueve un solo archivo,
no rompe Radarr/Sonarr/Bazarr y se revierte borrando el tag. Validado en vivo el
2026-08-21: un item etiquetado paso de visible (1) a invisible (0) para una cuenta con
BlockedTags=['ARREGLANDO'], y volvio al quitar el bloqueo.

La cuenta admin NUNCA se bloquea: Beren ve la cola completa.

Subcomandos:
  sync             etiqueta / desetiqueta segun compliance_state
  failsafe         destapa lo que lleve mas de N horas oculto y avisa
  apply-policies   pone BlockedTags=['ARREGLANDO'] en las cuentas no-admin
  report           que hay etiquetado ahora mismo
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get("STATE_DIR", "/APPBOX_DATA/storage/.transcode-state-media")
DB_PATH = os.environ.get("COMPLIANCE_DB", os.path.join(STATE_DIR, "library_codec_state.db"))
LOG_PATH = os.environ.get("COMPLIANCE_TAG_LOG", "/config/berenstuff/automation/logs/emby_compliance_tags.log")

TAG_OCULTO = "ARREGLANDO"
TAG_MARCADO = "PENDIENTE"
TAGS = (TAG_OCULTO, TAG_MARCADO)

# Tope duro: si un cambio quisiera ocultar mas que esto de golpe, algo se rompio en el
# planner y preferimos no desaparecer media biblioteca. Se avisa y no se aplica.
MAX_OCULTOS = int(os.environ.get("COMPLIANCE_MAX_OCULTOS", "200"))


def log(msg):
    line = "%s [emby-tags] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class Emby:
    def __init__(self):
        self.url = os.environ["EMBY_URL"].rstrip("/")
        self.key = os.environ["EMBY_API_KEY"]

    def _call(self, path, body=None, method=None):
        url = self.url + path + ("&" if "?" in path else "?") + "api_key=" + self.key
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()[:300].decode("utf8", "replace")

    def items(self):
        """path -> {'id': ...} para todo episodio y pelicula.

        OJO (verificado 2026-08-21): pedir Fields=TagItems en una respuesta de LISTA no
        devuelve nada — Emby solo puebla TagItems en el GET de un item suelto. Por eso el
        inventario de lo etiquetado se saca con items_by_tag(), no de aqui. Confiar en
        TagItems aqui hacia que el sync nunca quitara un tag (del=0 siempre).
        """
        st, data = self._call(
            "/Items?Recursive=true&IncludeItemTypes=Episode,Movie"
            "&Fields=Path&EnableImages=false"
        )
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError("Emby /Items fallo: %s %s" % (st, data))
        out = {}
        for it in data.get("Items", []):
            path = it.get("Path")
            if not path:
                continue
            out[path] = {"id": str(it["Id"])}
        return out

    def items_by_tag(self, tag):
        """path -> emby_id de todo lo que lleva ese tag AHORA MISMO en Emby."""
        st, data = self._call(
            "/Items?Recursive=true&IncludeItemTypes=Episode,Movie"
            "&Tags=%s&Fields=Path&EnableImages=false" % urllib.parse.quote(tag)
        )
        if st != 200 or not isinstance(data, dict):
            raise RuntimeError("Emby /Items?Tags=%s fallo: %s %s" % (tag, st, data))
        return {it["Path"]: str(it["Id"]) for it in data.get("Items", []) if it.get("Path")}

    def add_tag(self, item_id, tag):
        return self._call("/Items/%s/Tags/Add" % item_id, {"Tags": [{"Name": tag}]})[0]

    def del_tag(self, item_id, tag):
        return self._call("/Items/%s/Tags/Delete" % item_id, {"Tags": [{"Name": tag}]})[0]

    def users(self):
        st, data = self._call("/Users")
        if st != 200:
            raise RuntimeError("Emby /Users fallo: %s %s" % (st, data))
        return data

    def set_policy(self, uid, policy):
        return self._call("/Users/%s/Policy" % uid, policy)[0]

    def refresh_library(self):
        return self._call("/Library/Refresh", {})[0]


def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def deseado(con):
    """media_id -> (path, tag) para lo que DEBE estar etiquetado."""
    rows = con.execute(
        "SELECT media_id, path, estado, clase FROM compliance_state "
        "WHERE estado IN ('encolado','trabajando','fallido')"
    ).fetchall()
    want = {}
    for r in rows:
        if r["estado"] == "fallido":
            # Fallido se MARCA pero nunca se oculta: si algo se rompio, Beren tiene que
            # poder verlo, y un usuario que ya lo tenia no debe perderlo de la vista.
            tag = TAG_MARCADO
        elif r["clase"] == "rapido":
            tag = TAG_OCULTO
        else:
            tag = TAG_MARCADO
        want[r["media_id"]] = (r["path"], tag)
    return want


def cmd_sync(args):
    con = connect_db()
    emby = Emby()
    want = deseado(con)
    catalogo = emby.items()

    n_ocultar = sum(1 for _, t in want.values() if t == TAG_OCULTO)
    if n_ocultar > MAX_OCULTOS and not args.force:
        log("ABORTO: se querian ocultar %d items (tope %d). Algo huele mal en el planner. "
            "Usa --force si de verdad es lo que quieres." % (n_ocultar, MAX_OCULTOS))
        return 2

    # Lo que Emby tiene etiquetado HOY, preguntandole por tag (una consulta por tag).
    # Esta es la fuente de verdad del inventario: incluye tags que quedaron de corridas
    # anteriores aunque nuestra tabla emby_tag_state se haya perdido.
    actuales = {}
    id_por_path = {}
    for t in TAGS:
        for path, iid in emby.items_by_tag(t).items():
            actuales.setdefault(path, set()).add(t)
            id_por_path[path] = iid

    add_ops, del_ops, sin_item = [], [], []
    deseado_por_path = {}
    for mid, (path, tag) in want.items():
        deseado_por_path[path] = (mid, tag)
        info = catalogo.get(path)
        if info is None:
            sin_item.append(path)
            continue
        puestos = actuales.get(path, set())
        if tag not in puestos:
            add_ops.append((mid, info["id"], path, tag))
        for otro in TAGS:
            if otro != tag and otro in puestos:
                del_ops.append((mid, info["id"], path, otro))

    # Todo lo etiquetado que YA NO deberia estarlo (conforme, aparcado o desaparecido):
    # aqui es donde se cierra el 3.4 — al terminar la conversion el tag se cae solo.
    for path, tags in actuales.items():
        if path in deseado_por_path:
            continue
        iid = id_por_path.get(path) or (catalogo.get(path) or {}).get("id")
        if not iid:
            continue
        for t in tags:
            del_ops.append((None, iid, path, t))

    log("sync: deseados=%d (ocultar=%d marcar=%d) add=%d del=%d sin_item_en_emby=%d"
        % (len(want), n_ocultar, len(want) - n_ocultar, len(add_ops), len(del_ops), len(sin_item)))
    for path in sin_item[:10]:
        log("  aviso: sin item en Emby: %s" % path)

    if args.dry_run:
        for mid, iid, path, tag in add_ops[:40]:
            log("  [dry] +%s %s" % (tag, path))
        for mid, iid, path, tag in del_ops[:40]:
            log("  [dry] -%s %s" % (tag, path))
        return 0

    hechos = 0
    for mid, iid, path, tag in add_ops:
        st = emby.add_tag(iid, tag)
        if st in (200, 204):
            con.execute(
                "INSERT INTO emby_tag_state(media_id,emby_id,tag,path,tagged_ts) "
                "VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(media_id) DO UPDATE SET "
                "emby_id=excluded.emby_id, tag=excluded.tag, path=excluded.path, "
                "tagged_ts=CASE WHEN emby_tag_state.tag=excluded.tag THEN emby_tag_state.tagged_ts "
                "ELSE CURRENT_TIMESTAMP END",
                (mid, iid, tag, path))
            hechos += 1
        else:
            log("  ERROR add %s en %s: %s" % (tag, path, st))
    for mid, iid, path, tag in del_ops:
        st = emby.del_tag(iid, tag)
        if st in (200, 204):
            con.execute("DELETE FROM emby_tag_state WHERE emby_id=? AND tag=?", (iid, tag))
            hechos += 1
        else:
            log("  ERROR del %s en %s: %s" % (tag, path, st))
    con.commit()

    if hechos:
        emby.refresh_library()
    log("sync: %d operaciones aplicadas" % hechos)
    return 0


def cmd_failsafe(args):
    """3.5 - nada se queda invisible porque un script murio."""
    con = connect_db()
    emby = Emby()
    rows = con.execute(
        "SELECT media_id, emby_id, tag, path, tagged_ts, "
        "  ROUND((julianday('now')-julianday(tagged_ts))*24.0,1) AS horas "
        "FROM emby_tag_state WHERE tag=? "
        "  AND (julianday('now')-julianday(tagged_ts))*24.0 > ?",
        (TAG_OCULTO, args.horas)).fetchall()
    if not rows:
        log("failsafe: nada oculto por mas de %s h" % args.horas)
        return 0
    for r in rows:
        st = emby.del_tag(r["emby_id"], r["tag"])
        if st in (200, 204):
            con.execute("DELETE FROM emby_tag_state WHERE media_id=?", (r["media_id"],))
            log("FAILSAFE destapado tras %s h: %s" % (r["horas"], r["path"]))
        else:
            log("FAILSAFE ERROR al destapar %s: %s" % (r["path"], st))
    con.commit()
    emby.refresh_library()
    log("failsafe: %d items destapados - REVISAR por que se quedaron colgados" % len(rows))
    return 1


def cmd_apply_policies(args):
    emby = Emby()
    cambios = 0
    for u in emby.users():
        pol = u.get("Policy", {})
        if pol.get("IsAdministrator"):
            log("admin intacto (ve la cola completa): %s" % u["Name"])
            continue
        actual = list(pol.get("BlockedTags") or [])
        if TAG_OCULTO in actual:
            continue
        if args.dry_run:
            log("[dry] bloquearia %s en %s" % (TAG_OCULTO, u["Name"]))
            continue
        pol["BlockedTags"] = sorted(set(actual + [TAG_OCULTO]))
        st = emby.set_policy(u["Id"], pol)
        if st in (200, 204):
            cambios += 1
        else:
            log("ERROR politica en %s: %s" % (u["Name"], st))
    log("apply-policies: %d cuentas actualizadas" % cambios)
    return 0


def cmd_report(args):
    con = connect_db()
    rows = con.execute(
        "SELECT tag, COUNT(*) n, MIN(tagged_ts) mas_viejo FROM emby_tag_state GROUP BY tag"
    ).fetchall()
    if not rows:
        print("nada etiquetado ahora mismo")
    for r in rows:
        print("%-11s %4d items   el mas viejo desde %s" % (r["tag"], r["n"], r["mas_viejo"]))
    print("")
    for r in con.execute(
            "SELECT tag, path, tagged_ts FROM emby_tag_state ORDER BY tagged_ts LIMIT 25"):
        print("  %-11s %s  (%s)" % (r["tag"], r["path"], r["tagged_ts"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="salta el tope de %d ocultos" % MAX_OCULTOS)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("failsafe")
    p.add_argument("--horas", type=float, default=6.0)
    p.set_defaults(func=cmd_failsafe)

    p = sub.add_parser("apply-policies")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply_policies)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args()
    try:
        sys.exit(args.func(args))
    except Exception as exc:  # noqa: BLE001
        log("FATAL: %s: %s" % (type(exc).__name__, exc))
        sys.exit(3)


if __name__ == "__main__":
    main()
