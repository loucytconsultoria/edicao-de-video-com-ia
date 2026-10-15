# -*- coding: utf-8 -*-
"""Transições entre os blocos de um corte já montado, SEM mudar a duração nem
o áudio: tudo acontece nos quadros do próprio corte, sem precisar dos brutos.

Modos (--modo):
  zoom_suave  (padrão) o bloco que entra nasce 3,5% maior e assenta em 0,55 s,
              com curva cúbica e recorte SUBPIXEL (filtro perspective). O
              cérebro lê "outra câmera", sem tranco. Versões anteriores usavam
              7% em 0,30 s com scale+crop inteiros: o quadro anda em degraus de
              2 px e a revisão humana reprovou como "tremido e exagerado".
  blur_cross  4 quadros antes do corte a imagem desfoca (sigma até 16) e 4
              quadros depois volta a ficar nítida, com um respiro de zoom de
              3,5% no meio: a emenda deixa de ser seca sem virar dissolve.
  push        o bloco que entra desliza da direita por cima do último quadro do
              bloco anterior (0,22 s, ease-out cúbico).

Cortes: lista de segundos (--cortes 3.2,7.9) ou um JSON de planos (--planos,
lista [{"dur": s, "zoom": true}], cortes = durações acumuladas). Por padrão os
cortes declarados são AJUSTADOS à mudança de cena medida no vídeo (o plano
declarado erra por um quadro); --sem-snap desliga.

--area topo: só a metade de cima do quadro se move (tela dividida em que o
painel de baixo deve atravessar o corte contínuo).

Uso: python transicoes.py BASE.mp4 SAIDA.mp4 --modo zoom_suave --cortes 3.2,7.9
Saída: vídeo reencodado (NVENC com fallback x264), áudio copiado (push recodifica)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import VENC, ffmpeg, probe

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ZOOM_AMP, ZOOM_DUR = 0.035, 0.55


def _dims(video):
    m = probe(video)
    return int(m["largura"]), int(m["altura"]), float(m.get("fps") or 30.0)


def expr_zoom(cortes, amp, dur_t, W, H):
    """Zoom em sino centrado no corte (usado só no blur_cross, onde o desfoque
    esconde o degrau de pixel do scale)."""
    termos = [f"if(between(t,{T - dur_t / 2:.3f},{T + dur_t / 2:.3f}),"
              f"sin(PI*(t-{T - dur_t / 2:.3f})/{dur_t:.3f}),0)" for T in cortes]
    Z = f"(1+{amp}*(" + "+".join(termos) + "))"
    return (f"scale=w='2*round({W}*{Z}/2)':h='2*round({H}*{Z}/2)':eval=frame:flags=bilinear,"
            f"crop={W}:{H}:(in_w-{W})/2:(in_h-{H})/2,setsar=1")


def zoom_suave(video, cortes, saida, amp=ZOOM_AMP, dur_t=ZOOM_DUR, area=None):
    """O perspective recorta em posição subpixel (interpolação cúbica) e só age
    na janela da transição; fora dela o quadro fica intacto."""
    W, H, fps = _dims(video)
    n = max(2, int(round(dur_t * fps)))
    filtros = []
    for T in cortes:
        f0 = int(round(T * fps))
        # in = índice do quadro + 1; u vai de 0 a 1 na janela; z = amp*(1-u)^3
        z = f"{amp}*pow(1-(in-1-{f0})/{n},3)"
        d = f"(1-1/(1+{z}))/2"
        filtros.append(
            f"perspective=x0='W*{d}':y0='H*{d}':x1='W-W*{d}':y1='H*{d}':"
            f"x2='W*{d}':y2='H-H*{d}':x3='W-W*{d}':y3='H-H*{d}':interpolation=cubic:eval=frame:"
            f"enable='between(n,{f0},{f0 + n - 1})'")
    cadeia = ",".join(filtros) if filtros else "null"
    if area == "topo":
        h2 = H // 2
        fc = (f"[0:v]split=2[a][b];[a]crop={W}:{h2}:0:0,{cadeia}[t];"
              f"[b]crop={W}:{H - h2}:0:{h2}[u];[t][u]vstack,setsar=1[v]")
    else:
        fc = f"[0:v]{cadeia},setsar=1[v]"
    wk = tempfile.mkdtemp(prefix="videoia_zs_")
    script = os.path.join(wk, "fc.txt")
    open(script, "w", encoding="ascii").write(fc)
    ffmpeg(["-i", os.path.abspath(video), "-filter_complex_script", "fc.txt",
            "-map", "[v]", "-map", "0:a?", *VENC, "-c:a", "copy", "-movflags", "+faststart",
            os.path.abspath(saida)], cwd=wk)
    shutil.rmtree(wk, ignore_errors=True)


def blur_cross(video, cortes, saida):
    W, H, fps = _dims(video)
    wk = tempfile.mkdtemp(prefix="videoia_tr_")
    f = 1.0 / fps
    linhas = ["0.0 gblur sigma 0;"]
    perfil = [3, 7, 12, 16, 16, 11, 6, 2, 0]      # -4..+4 quadros ao redor do corte
    for T in cortes:
        for k, s in enumerate(perfil):
            linhas.append(f"{T + (k - 4) * f:.4f} gblur sigma {s};")
    open(os.path.join(wk, "cmds.txt"), "w", encoding="ascii").write("\n".join(linhas) + "\n")
    vf = "sendcmd=f=cmds.txt,gblur=sigma=0:steps=2," + expr_zoom(cortes, 0.035, 0.26, W, H)
    ffmpeg(["-i", os.path.abspath(video), "-vf", vf, *VENC, "-c:a", "copy",
            "-movflags", "+faststart", os.path.abspath(saida)], cwd=wk)
    shutil.rmtree(wk, ignore_errors=True)


def push(video, cortes, saida, dur_t=0.22):
    W, H, fps = _dims(video)
    wk = tempfile.mkdtemp(prefix="videoia_push_")
    total = probe(video)["duracao"]
    bordas = [0.0] + list(cortes) + [total]
    clipes = []
    for k in range(len(bordas) - 1):
        t0, t1 = bordas[k], bordas[k + 1]
        out = os.path.join(wk, f"c{k:02d}.mkv")
        if k == 0:
            ffmpeg(["-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", video, "-vf", "setsar=1", *VENC,
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", out])
        else:
            still = os.path.join(wk, f"s{k:02d}.png")
            ffmpeg(["-ss", f"{t0 - 1.5 / fps:.4f}", "-i", video, "-frames:v", "1", still])
            fc = (f"[0:v]scale={W}:{H},setsar=1,fps={fps}[bg];"
                  f"[bg][1:v]overlay=x='{W}*pow(1-min(t/{dur_t},1),3)':y=0:shortest=1:eval=frame[v]")
            ffmpeg(["-loop", "1", "-framerate", str(fps), "-i", still,
                    "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", video,
                    "-filter_complex", fc, "-map", "[v]", "-map", "1:a", *VENC,
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-t", f"{t1 - t0:.3f}", out])
        clipes.append(out)
    lista = os.path.join(wk, "lista.txt")
    open(lista, "w", encoding="utf-8").write("".join(f"file '{c.replace(os.sep, '/')}'\n" for c in clipes))
    ffmpeg(["-f", "concat", "-safe", "0", "-i", lista, "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", saida])
    shutil.rmtree(wk, ignore_errors=True)


def cortes_medidos(video, limiar=0.25):
    """Instantes de mudança de cena MEDIDOS no vídeo."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", video, "-vf",
                        f"select='gt(scene,{limiar})',showinfo", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [float(x) for x in re.findall(r"pts_time:([0-9.]+)", r.stderr)]


def snap(cortes, medidos, tol=0.12):
    out = []
    for c in cortes:
        perto = [m for m in medidos if abs(m - c) <= tol]
        out.append(round(min(perto, key=lambda m: abs(m - c)), 4) if perto else c)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video"); ap.add_argument("saida")
    ap.add_argument("--modo", choices=("zoom_suave", "blur_cross", "push"), default="zoom_suave")
    ap.add_argument("--cortes", help="segundos separados por vírgula")
    ap.add_argument("--planos", help="JSON [{dur, zoom}] (cortes = acumulado das durações)")
    ap.add_argument("--area", choices=("quadro", "topo"), default="quadro")
    ap.add_argument("--sem-snap", action="store_true", help="não ajustar os cortes à mudança de cena medida")
    a = ap.parse_args()
    if a.planos:
        pl = json.load(open(a.planos, encoding="utf-8"))
        acc, cortes = 0.0, []
        for p, prox in zip(pl[:-1], pl[1:]):
            acc += p["dur"]
            if prox.get("zoom", True):       # o montador pode marcar corte seco por plano
                cortes.append(round(acc, 3))
    elif a.cortes:
        cortes = [float(x) for x in a.cortes.split(",") if x.strip()]
    else:
        cortes = []
    if not cortes:
        # Plano único: não há emenda, logo não há transição. Sem esta saída a
        # expressão de zoom virava uma soma vazia e o ffmpeg abortava.
        shutil.copy2(a.video, a.saida)
        print(f"{a.saida}: plano único, sem emenda — nenhuma transição a aplicar (cópia do base)")
        return
    if not a.sem_snap:
        medidos = cortes_medidos(a.video)
        ajustados = snap(cortes, medidos)
        print("cortes declarados:", cortes, "\ncortes medidos:  ", ajustados)
        cortes = ajustados
    if a.modo == "zoom_suave":
        zoom_suave(a.video, cortes, a.saida, area=("topo" if a.area == "topo" else None))
    else:
        {"blur_cross": blur_cross, "push": push}[a.modo](a.video, cortes, a.saida)
    print(f"{a.saida}: {a.modo} em {len(cortes)} cortes: {cortes}")


if __name__ == "__main__":
    main()
