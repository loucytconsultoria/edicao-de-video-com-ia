# -*- coding: utf-8 -*-
"""AUDITORIA VISUAL POR FRAMES — o que o portao de audio nao ve.

Por que existe: a revisao humana apontou, numa peca ja aprovada pelos portoes
de audio, um trecho silencioso com a mao do apresentador mexendo no celular. O
portao mede palavra, emenda e energia de voz: um trecho sem fala com a mao em
quadro passa por todas as conferencias. Audio limpo nao e video limpo.

O que este script mede, por peca:

 A. TRECHO SEM FALA. Vaos > 0,7s no audio final. Sao os candidatos naturais a
    gesto perdido, mao em quadro, olhar fora, celular. Extrai um frame do meio
    de cada um para inspecao visual.

 B. MOVIMENTO DURANTE O SILENCIO. Diferenca media entre frames consecutivos
    dentro do vao. Silencio + movimento alto = alguem se mexendo sem falar,
    que e exatamente o defeito relatado. Silencio + movimento baixo = pausa
    natural, tudo bem.

 C. SALTO NA EMENDA. Nas emendas do cutmap, compara o frame imediatamente
    antes com o imediatamente depois. Diferenca alta = corte visivel (pulo de
    enquadramento, mudanca de pose). O agrupamento por enquadramento reduz
    isso, mas nao elimina — dentro do mesmo grupo a pessoa ainda muda de postura.

 D. PRIMEIRO E ULTIMO FRAME. Abertura com a pessoa ainda se ajeitando ou fecho
    com ela ja relaxando sao os dois pontos que mais denunciam edicao.

Saida: um JSON com as medidas e uma pasta de frames PNG por peca, para conferir
com os olhos. A medicao aponta ONDE olhar; quem decide e o olho — o mesmo
principio do resto do metodo (a regua mede, a decisao e informada).

Uso:
    python auditar_video.py VIDEO.mp4 --render PASTA_RENDER [--saida PASTA_FRAMES]

PASTA_RENDER segue a convencao do pipeline_peca.py: `analise_corte/analise.json`
(palavras medidas na saida) e, se houver, `edl_efetivo.json` (para achar as
emendas). Sem a analise, o script so extrai as pontas.
Exit: 0 sem suspeitas | 2 ha frames suspeitos para OLHAR (nao e reprovacao
automatica: a medicao diz onde olhar, quem decide e o olho).
"""
import argparse, glob, json, os, subprocess, sys, tempfile

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")

LIM_VAO = 0.70            # s sem fala que merece um olhar
LIM_MOV = 6.0             # diferenca media (0-255) que conta como "se mexendo"
LIM_SALTO = 12.0          # diferenca entre os lados de uma emenda


def _frames(video, tempos, pasta, prefixo):
    """Extrai um PNG por instante pedido. Devolve [(t, caminho)]."""
    os.makedirs(pasta, exist_ok=True)
    saidas = []
    for i, t in enumerate(tempos):
        cam = os.path.join(pasta, f"{prefixo}_{i:02d}_{t:07.2f}s.png")
        if not os.path.exists(cam):
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}",
                            "-i", video, "-frames:v", "1", "-vf", "scale=360:-1",
                            "-y", cam], capture_output=True)
        if os.path.exists(cam):
            saidas.append((round(t, 2), cam))
    return saidas


def _dif_media(video, t0, t1, n=6):
    """Diferenca media entre frames consecutivos amostrados em [t0, t1]."""
    import numpy as np
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), f"videoia_mov_{os.getpid()}")
    os.makedirs(tmp, exist_ok=True)
    if t1 - t0 < 0.15:
        return 0.0
    passos = [t0 + (t1 - t0) * k / (n - 1) for k in range(n)]
    ims = []
    for k, t in enumerate(passos):
        cam = os.path.join(tmp, f"f{k}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}",
                        "-i", video, "-frames:v", "1", "-vf", "scale=192:-1,format=gray",
                        "-y", cam], capture_output=True)
        if os.path.exists(cam):
            ims.append(np.asarray(Image.open(cam), dtype="float32"))
            os.remove(cam)
    if len(ims) < 2:
        return 0.0
    difs = [float(np.mean(np.abs(ims[k + 1] - ims[k]))) for k in range(len(ims) - 1)
            if ims[k + 1].shape == ims[k].shape]
    return round(max(difs) if difs else 0.0, 2)


def _dif_par(video, ta, tb):
    """Diferenca entre o frame em ta e o frame em tb (usada nas emendas)."""
    import numpy as np
    from PIL import Image
    tmp = os.path.join(tempfile.gettempdir(), f"videoia_par_{os.getpid()}")
    os.makedirs(tmp, exist_ok=True)
    ims = []
    for k, t in enumerate((ta, tb)):
        cam = os.path.join(tmp, f"p{k}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}",
                        "-i", video, "-frames:v", "1", "-vf", "scale=192:-1,format=gray",
                        "-y", cam], capture_output=True)
        if os.path.exists(cam):
            ims.append(np.asarray(Image.open(cam), dtype="float32"))
            os.remove(cam)
    if len(ims) < 2 or ims[0].shape != ims[1].shape:
        return 0.0
    return round(float(np.mean(np.abs(ims[1] - ims[0]))), 2)


def _duracao(video):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def auditar(peca_mp4, render, saida):
    nome = os.path.splitext(os.path.basename(peca_mp4))[0]
    an = os.path.join(render, "analise_corte", "analise.json")
    if os.path.exists(an):
        d = json.load(open(an, encoding="utf-8"))
        pal = [w for w in d["palavras"] if w["w"].strip() and (w["e"] - w["s"]) >= 0.05]
        dur = d["meta"]["duracao"]
    else:
        pal, dur = [], _duracao(peca_mp4)
    pasta = os.path.join(saida, nome)

    # A/B. vaos sem fala + movimento dentro deles
    vaos = []
    if pal:
        if pal[0]["s"] > LIM_VAO:
            vaos.append((0.0, pal[0]["s"]))
        for x, y in zip(pal, pal[1:]):
            if y["s"] - x["e"] > LIM_VAO:
                vaos.append((x["e"], y["s"]))
        if dur - pal[-1]["e"] > LIM_VAO:
            vaos.append((pal[-1]["e"], dur))
    achados_vao = []
    for (va, vb) in vaos:
        mov = _dif_media(peca_mp4, va, vb)
        achados_vao.append({"ini": round(va, 2), "fim": round(vb, 2),
                            "dur": round(vb - va, 2), "movimento": mov,
                            "suspeito": mov >= LIM_MOV})
    frames_vao = _frames(peca_mp4, [(a + b) / 2 for a, b in vaos], pasta, "vao")

    # C. emendas: onde o cutmap junta dois trechos
    edl = os.path.join(render, "edl_efetivo.json")
    emendas = []
    if os.path.exists(edl):
        ed = json.load(open(edl, encoding="utf-8"))
        acc = 0.0
        for c in ed["clips"]:
            for (a_, b_) in (c.get("cutmap") or [[c["in"], c["out"]]]):
                if acc > 0.05:
                    emendas.append(round(acc, 3))
                acc += (b_ - a_)
    achados_emenda = []
    for t in emendas:
        s = _dif_par(peca_mp4, max(0.0, t - 0.10), min(dur - 0.05, t + 0.10))
        achados_emenda.append({"t": t, "salto": s, "suspeito": s >= LIM_SALTO})
    frames_emenda = _frames(peca_mp4,
                            [x for t in emendas for x in (max(0.0, t - 0.12), min(dur - 0.05, t + 0.12))],
                            pasta, "emenda")

    # D. pontas
    frames_ponta = _frames(peca_mp4, [0.05, min(0.6, dur), max(0.0, dur - 0.6),
                                      max(0.0, dur - 0.08)], pasta, "ponta")

    return {"peca": nome, "duracao": round(dur, 2),
            "vaos_sem_fala": achados_vao,
            "vaos_suspeitos": sum(1 for v in achados_vao if v["suspeito"]),
            "emendas": achados_emenda,
            "emendas_suspeitas": sum(1 for e in achados_emenda if e["suspeito"]),
            "frames": {"vao": [c for _, c in frames_vao],
                       "emenda": [c for _, c in frames_emenda],
                       "ponta": [c for _, c in frames_ponta]}}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("videos", nargs="+", help="peca(s) a auditar")
    ap.add_argument("--render", default=None,
                    help="pasta de render da peca (default: pasta do video)")
    ap.add_argument("--saida", default=None, help="pasta dos frames (default: <render>/_visual)")
    a = ap.parse_args()
    rel = []
    for mp4 in a.videos:
        render = a.render or os.path.dirname(os.path.abspath(mp4))
        SAIDA = a.saida or os.path.join(render, "_visual")
        os.makedirs(SAIDA, exist_ok=True)
        nome = os.path.splitext(os.path.basename(mp4))[0]
        r = auditar(mp4, render, SAIDA)
        rel.append(r)
        if "erro" in r:
            print(f"{nome}: {r['erro']}")
            continue
        print(f"{nome}  ({r['duracao']:.0f}s)")
        for v in r["vaos_sem_fala"]:
            marca = "  <== SEM FALA COM MOVIMENTO" if v["suspeito"] else ""
            print(f"   vao {v['ini']:7.2f}-{v['fim']:7.2f} ({v['dur']:.2f}s) "
                  f"movimento {v['movimento']:5.2f}{marca}")
        for e in r["emendas"]:
            if e["suspeito"]:
                print(f"   emenda {e['t']:7.2f}s salto visual {e['salto']:.1f}  <== CORTE VISIVEL")
    json.dump(rel, open(os.path.join(SAIDA, "_relatorio.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n_v = sum(r.get("vaos_suspeitos", 0) for r in rel)
    n_e = sum(r.get("emendas_suspeitas", 0) for r in rel)
    print("=" * 60)
    print(f"VISUAL: {n_v} vao(s) sem fala com movimento, {n_e} emenda(s) com salto, "
          f"em {len(rel)} peca(s). Frames em {SAIDA}")
    return 2 if (n_v or n_e) else 0


if __name__ == "__main__":
    sys.exit(main())
