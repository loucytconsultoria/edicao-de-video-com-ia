# -*- coding: utf-8 -*-
"""Corte de silêncio guiado por transcrição: remove pausas longas SEM cortar no
meio de palavra — os pontos de corte são sempre fronteiras de palavras com
folga (padding), a última palavra antes de cada corte termina inteira, e o
silêncio antes da primeira fala é removido.

Uso (requer analise.json gerado por analisar.py):
  python cortar_silencio.py PASTA_EDICAO [--render SAIDA.mp4] [opções]

Sem --render, só grava corte.json (plano de corte + cutmap) e imprime o plano —
para o fluxo "planejar → aprovar → renderizar".

Parâmetros de ritmo (defaults calibrados para fala de Reels):
  --pausa-min 0.6    só corta pausas maiores que isso (menores são ritmo natural)
  --pad-depois 0.18  respiro mantido DEPOIS da última palavra de um bloco
  --pad-antes 0.10   respiro mantido ANTES da primeira palavra do bloco seguinte
                     (pausa restante em cada corte = pad_depois + pad_antes)
  --pausa-inicio 0.1 silêncio mantido antes da primeira palavra do vídeo
  --pad-fim 0.35     respiro depois da última palavra do vídeo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import (ENC_A, QUALIDADES, carregar_json, duracao_cutmap, fail,
                   ffmpeg, salvar_json)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAUSA_LONGA_REVISAR = 1.5  # acima disso pode ser pausa dramática intencional


def montar_cutmap(palavras: list[dict], duracao: float, pausa_min: float,
                  pad_antes: float, pad_depois: float, pausa_inicio: float,
                  pad_fim: float) -> tuple[list[dict], list[dict]]:
    """Retorna (cutmap, cortes_feitos). Nunca corta dentro de [s,e] de palavra."""
    if not palavras:
        return [{"src_in": 0.0, "src_out": duracao}], []
    # word timestamps do Whisper ocasionalmente saem da ordem em borda de
    # segmento — sem ordenar, uma palavra fora de ordem seria deletada do vídeo
    palavras = sorted(palavras, key=lambda p: p["s"])

    # blocos de fala usando pausa_min como limiar: só o que passa disso é corte
    blocos = []
    atual = [palavras[0]["s"], palavras[0]["e"]]
    for p in palavras[1:]:
        if p["s"] - atual[1] > pausa_min:
            blocos.append(atual)
            atual = [p["s"], p["e"]]
        else:
            atual[1] = max(atual[1], p["e"])
    blocos.append(atual)

    cutmap, cortes = [], []
    for i, (bs, be) in enumerate(blocos):
        ini = max(0.0, bs - (pausa_inicio if i == 0 else pad_antes))
        fim = min(duracao, be + (pad_fim if i == len(blocos) - 1 else pad_depois))
        if fim <= ini:
            continue
        if cutmap and ini <= cutmap[-1]["src_out"]:
            cutmap[-1]["src_out"] = max(cutmap[-1]["src_out"], fim)  # padding encostou: funde
        else:
            if cutmap:
                cortes.append({"de": cutmap[-1]["src_out"], "ate": ini,
                               "removido": round(ini - cutmap[-1]["src_out"], 3)})
            cutmap.append({"src_in": round(ini, 3), "src_out": round(fim, 3)})
    if cutmap and cutmap[0]["src_in"] > 0.01:
        cortes.insert(0, {"de": 0.0, "ate": cutmap[0]["src_in"],
                          "removido": cutmap[0]["src_in"], "tipo": "inicio"})
    if cutmap and duracao - cutmap[-1]["src_out"] > 0.01:
        cortes.append({"de": cutmap[-1]["src_out"], "ate": round(duracao, 3),
                       "removido": round(duracao - cutmap[-1]["src_out"], 3), "tipo": "fim"})
    return cutmap, cortes


def render_cutmap(video: Path, cutmap: list[dict], saida: Path, workdir: Path,
                  declick: float = 0.006) -> None:
    """Extrai cada trecho re-encodado (frame-accurate) e concatena.
    Cada segmento é sincronizado de forma independente — sem drift acumulado."""
    segdir = workdir / "segmentos"
    segdir.mkdir(parents=True, exist_ok=True)
    lista = []
    for i, c in enumerate(cutmap):
        seg = segdir / f"seg_{i:03d}.mp4"
        dur = c["src_out"] - c["src_in"]
        af = f"afade=t=in:st=0:d={declick},afade=t=out:st={max(0.0, dur - declick):.4f}:d={declick}" \
            if declick > 0 else "anull"
        # -ss antes do -i (seek rápido, reseta pts p/ 0) + -t na saída (duração
        # exata). Não usar -to de entrada: com -ss ele é comparado contra pts
        # originais e produz segmentos com duração errada.
        ffmpeg(["-ss", f"{c['src_in']:.3f}", "-i", str(video), "-t", f"{dur:.3f}",
                "-af", af, *QUALIDADES["final"]["v"], "-pix_fmt", "yuv420p", *ENC_A,
                "-avoid_negative_ts", "make_zero", str(seg)])
        lista.append(seg)
    listapath = segdir / "concat.txt"
    listapath.write_text("".join(f"file '{s.name}'\n" for s in lista), encoding="utf-8")
    ffmpeg(["-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy",
            "-movflags", "+faststart", str(saida.resolve())], cwd=segdir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", help="pasta de edição contendo analise.json")
    ap.add_argument("--render", default=None, metavar="SAIDA.mp4",
                    help="além de planejar, renderiza o vídeo cortado")
    ap.add_argument("--pausa-min", type=float, default=0.6)
    ap.add_argument("--pad-antes", type=float, default=0.10)
    ap.add_argument("--pad-depois", type=float, default=0.18)
    ap.add_argument("--pausa-inicio", type=float, default=0.10)
    ap.add_argument("--pad-fim", type=float, default=0.35)
    ap.add_argument("--declick", type=float, default=0.006,
                    help="microfade de áudio (s) em cada borda de corte; 0 desliga")
    a = ap.parse_args()

    pasta = Path(a.pasta)
    aj = pasta / "analise.json"
    if not aj.exists():
        fail(f"analise.json não encontrado em {pasta} — rode analisar.py antes.")
    analise = carregar_json(aj)
    video = Path(analise["video"])
    dur = analise["meta"]["duracao"]
    if analise.get("sem_audio") or not analise["palavras"]:
        fail("vídeo sem fala transcrita — corte de silêncio por palavras não se aplica.")

    cutmap, cortes = montar_cutmap(analise["palavras"], dur, a.pausa_min,
                                   a.pad_antes, a.pad_depois, a.pausa_inicio, a.pad_fim)
    nova_dur = duracao_cutmap(cutmap)
    plano = {"video": str(video), "duracao_original": dur,
             "duracao_prevista": round(nova_dur, 3),
             "removido_total": round(dur - nova_dur, 3),
             "parametros": {"pausa_min": a.pausa_min, "pad_antes": a.pad_antes,
                            "pad_depois": a.pad_depois, "pausa_inicio": a.pausa_inicio,
                            "pad_fim": a.pad_fim},
             "cutmap": cutmap, "cortes": cortes}
    salvar_json(pasta / "corte.json", plano)

    print(f"Plano de corte: {len(cutmap)} trechos mantidos, {len(cortes)} cortes, "
          f"{dur:.1f}s -> {nova_dur:.1f}s (-{dur - nova_dur:.1f}s) | {pasta / 'corte.json'}")
    for c in cortes:
        aviso = "  << PAUSA LONGA: revisar se é dramática/intencional" \
            if c.get("tipo", "meio") == "meio" and c["removido"] > PAUSA_LONGA_REVISAR else ""
        print(f"  corte {c.get('tipo', 'meio'):6s} {c['de']:7.2f} -> {c['ate']:7.2f} (-{c['removido']:.2f}s){aviso}")

    if a.render:
        saida = Path(a.render)
        render_cutmap(video, cutmap, saida, pasta, a.declick)
        from comum import probe
        real = probe(saida)["duracao"]
        status = "OK" if abs(real - nova_dur) < 0.5 else "AVISO: divergência > 0.5s"
        print(f"Render: {saida} ({real:.2f}s, previsto {nova_dur:.2f}s) [{status}]")


if __name__ == "__main__":
    main()
