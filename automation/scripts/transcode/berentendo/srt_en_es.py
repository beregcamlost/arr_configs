#!/usr/bin/env python3
"""Traduce .en.srt -> .es.srt con el campeon CT2 en la 3090.

Replica EXACTAMENTE lo que hace el shim desplegado en debian (~/ct2_shim.py:78-97),
que es el unico camino probado en produccion. Las tres cosas que hay que copiar y que
NO son obvias, cada una pagada con una pasada ilegible el 2026-08-21:

  1. </s> TERMINAL en el source. Sin el, el modelo balbucea sin parar: "Mr. Hero," sale
     como "Sr. heroe, heroe, senor heroe, Sr. Sr. Heroe, heroe Sr.". El propio shim lo
     marca como "bug noche 1". Es LA causa; beam, length_penalty y max_decoding_length
     ya estaban bien.
  2. Traducir LINEA A LINEA dentro de la cue, no juntandolas. Asi se preservan los
     saltos, que en un subtitulo son parte del formato.
  3. Quitar las etiquetas ANTES de tocar el modelo: el NMT convierte <font face=...>
     en un reguero de interrogantes (el shim cita el bug de Farming S2 E01-E08).

Decodificacion igual a la viva: beam 8, length_penalty 0.8, no_repeat_ngram_size 3.
"""
import argparse
import os
import re
import sys

import ctranslate2
import sentencepiece as spm

MODELO = os.environ.get("NMT_MODEL_DIR", "/root/champ_extract")
BEAM = int(os.environ.get("CT2_BEAM", "8"))
LP = float(os.environ.get("CT2_LP", "0.8"))

TAGS = re.compile(r"<[^>]*>|\{\\[^}]*\}")
ESPACIOS = re.compile(r"[ \t]+")
# La dieresis no esta en el vocab target y sale como interrogante. Mismo arreglo que el shim.
FIX_U = re.compile("g\\s*⁇\\s*([ei])")


def leer_srt(ruta):
    with open(ruta, encoding="utf-8-sig", errors="replace") as fh:
        crudo = fh.read()
    crudo = crudo.replace("\r\n", "\n").replace("\r", "\n")
    bloques = []
    for bruto in re.split(r"\n\s*\n", crudo):
        lineas = [l for l in bruto.split("\n") if l.strip() != ""]
        if len(lineas) < 2:
            continue
        idx, tiempo, texto = lineas[0], lineas[1], lineas[2:]
        if "-->" not in tiempo:
            if "-->" in idx:
                tiempo, texto = idx, lineas[1:]
            else:
                continue
        bloques.append({"tiempo": tiempo.strip(), "lineas": [t.strip() for t in texto]})
    return bloques


def limpiar(linea):
    return ESPACIOS.sub(" ", TAGS.sub(" ", linea)).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entradas", nargs="+")
    ap.add_argument("--dir-salida", required=True)
    args = ap.parse_args()

    sp_src = spm.SentencePieceProcessor(os.path.join(MODELO, "source.spm"))
    sp_tgt = spm.SentencePieceProcessor(os.path.join(MODELO, "target.spm"))
    dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    tr = ctranslate2.Translator(MODELO, device=dev, compute_type="int8")
    print("modelo=%s device=%s beam=%d lp=%s" % (MODELO, dev, BEAM, LP), flush=True)

    os.makedirs(args.dir_salida, exist_ok=True)
    for ruta in args.entradas:
        bloques = leer_srt(ruta)
        if not bloques:
            print("VACIO %s" % ruta, flush=True)
            continue

        pendientes = []
        for bi, b in enumerate(bloques):
            b["limpias"] = [limpiar(l) for l in b["lineas"]]
            for li, t in enumerate(b["limpias"]):
                if t:
                    pendientes.append((bi, li, t))
        if not pendientes:
            print("SIN TEXTO %s" % ruta, flush=True)
            continue

        toks = [sp_src.encode(t, out_type=str) + ["</s>"] for _, _, t in pendientes]
        res = tr.translate_batch(
            toks, beam_size=BEAM, length_penalty=LP, no_repeat_ngram_size=3,
            max_decoding_length=max(12, 2 * max(len(t) for t in toks) + 10),
            max_batch_size=32,
        )
        for (bi, li, _), r in zip(pendientes, res):
            bloques[bi]["limpias"][li] = FIX_U.sub("gü\\1", sp_tgt.decode(r.hypotheses[0]))

        base = os.path.basename(ruta)
        for suf in (".en.srt", ".eng.srt"):
            if base.endswith(suf):
                base = base[: -len(suf)] + ".es.srt"
                break
        destino = os.path.join(args.dir_salida, base)
        with open(destino, "w", encoding="utf-8", newline="\n") as fh:
            for n, b in enumerate(bloques, 1):
                fh.write("%d\n%s\n%s\n\n" % (n, b["tiempo"], "\n".join(b["limpias"]).strip()))
        print("OK %d cues, %d lineas -> %s" % (len(bloques), len(pendientes), base), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
