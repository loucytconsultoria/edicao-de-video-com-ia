# -*- coding: utf-8 -*-
"""Demonstração ponta a ponta com dados SINTÉTICOS (sem modelo pesado, sem
mídia real): prova que o motor planeja, renderiza, legenda e confere.

O que ela faz, numa pasta temporária (ou em --saida):
  1. gera um "bruto" 16:9 de 11 s com ffmpeg (testsrc2) e uma trilha de áudio
     sintética: cada PALAVRA de um roteiro de treinamento vira um pulso de tom,
     com silêncio entre frases e uma pausa morta de 2,2 s no meio;
  2. escreve o analise.json com os tempos dessas palavras (o papel que o
     faster-whisper + alinhamento fonético cumprem num vídeo real);
  3. PLANO: cortar_silencio.py imprime o que será cortado (corte.json);
  4. APROVAÇÃO: o plano é mostrado e aprovado (automática aqui; --pausar
     espera Enter, como faria um revisor);
  5. RENDER: EDL -> render_edl.py (9:16 com fundo desfocado, legenda karaokê
     com palavra ativa, barra de progresso, end card, loudnorm);
  6. CONFERÊNCIA medida na saída: relatório do render, duração prevista x real,
     cobertura da legenda (cada palavra com exatamente 1 evento, nenhuma
     legenda no silêncio), energia de áudio em cada palavra remapeada (nenhuma
     palavra decepada), pausa morta removida, destaque da legenda visível no
     frame;
  7. transição de zoom suave nas emendas (duração e áudio intocados);
  8. cama musical sintética a 3,5% da voz, medida por ebur128;
  9. auditoria visual de frames;
 10. pipeline de peça com governança: plano -> montar -> aprovar RECUSADO
     (portões de áudio não rodaram) -> aprovar com override declarado.

Uso:  python exemplos/demo_sintetica.py [--saida PASTA] [--pausar] [--manter]
Requer: ffmpeg com libass, Python com numpy e Pillow. Não baixa nada.
Exit 0 = todas as verificações passaram.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPTS = RAIZ / "scripts"
sys.path.insert(0, str(SCRIPTS))

import numpy as np  # noqa: E402

from comum import carregar_json, probe, salvar_json  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PY = sys.executable
SR = 48000

# Roteiro de um microtreinamento. (frase, início da 1ª palavra em s)
FRASES = [
    ("Bem-vindo ao treinamento de segurança.", 0.60),
    ("Hoje vamos revisar o backup do servidor.", 4.90),   # depois de 2,2 s de pausa morta
    ("Salve o arquivo antes de sair.", 8.05),
]
DURACAO = 11.0


def palavras_sinteticas() -> list[dict]:
    """Tempo de cada palavra proporcional ao número de letras (~70 ms/letra)."""
    pal = []
    for frase, t in FRASES:
        for w in frase.split():
            dur = max(0.18, min(0.60, 0.07 * len(w.strip(".,"))))
            pal.append({"w": " " + w, "s": round(t, 3), "e": round(t + dur, 3), "p": 0.98})
            t += dur + 0.06
    return pal


def gerar_audio(pal: list[dict], wav: Path) -> None:
    """Pulso de tom por palavra (envelope suave) sobre um piso de ruído baixo."""
    n = int(DURACAO * SR)
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.0015, n)                       # "ar de sala"
    for k, p in enumerate(pal):
        a, b = int(p["s"] * SR), int(p["e"] * SR)
        t = np.arange(b - a) / SR
        f0 = 180 + 25 * (k % 5)                        # "entonação" variando
        env = np.sin(np.pi * np.arange(b - a) / (b - a)) ** 0.5
        x[a:b] += 0.35 * env * (np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(2 * np.pi * 2 * f0 * t))
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def gerar_musica(wav: Path, dur: float) -> None:
    """Acorde sustentado (cama musical sintética)."""
    t = np.arange(int(dur * SR)) / SR
    x = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.18, 329.63)) / 3
    pcm = (0.5 * x * 32767).astype(np.int16)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def ff(args, cwd=None):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-y", "-nostdin", "-loglevel", "error", *map(str, args)],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])


def roda(args, ok=(0,)):
    r = subprocess.run([PY, *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print("    " + (r.stdout.strip() or "(sem saída)").replace("\n", "\n    "))
    if r.returncode not in ok:
        print(r.stderr[-2500:], file=sys.stderr)
        raise SystemExit(f"falhou (exit {r.returncode}): {' '.join(map(str, args))}")
    return r


def pcm_mono(video: Path, sr=16000) -> np.ndarray:
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(video), "-vn", "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.float32)


def rms(x: np.ndarray, a: float, b: float, sr=16000) -> float:
    s = x[int(a * sr):int(b * sr)]
    return float(np.sqrt(np.mean(s ** 2))) if len(s) else 0.0


def maior_silencio(x: np.ndarray, ate: float, sr=16000, jan=0.01, lim=0.01) -> float:
    n = int(sr * jan)
    env = np.sqrt(np.mean(x[:int(ate * sr) // n * n].reshape(-1, n) ** 2, axis=1))
    melhor = cur = 0
    for v in env:
        cur = cur + 1 if v < lim else 0
        melhor = max(melhor, cur)
    return melhor * jan


class Verif:
    def __init__(self):
        self.itens = []

    def __call__(self, nome, ok, detalhe=""):
        self.itens.append({"verificacao": nome, "ok": bool(ok), "detalhe": detalhe})
        print(f"  [{'OK ' if ok else 'FALHOU'}] {nome}" + (f" — {detalhe}" if detalhe else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--saida", default=None, help="pasta de trabalho (default: temporária)")
    ap.add_argument("--pausar", action="store_true", help="espera Enter na aprovação do plano")
    ap.add_argument("--manter", action="store_true", help="não apaga a pasta temporária")
    a = ap.parse_args()
    base = Path(a.saida) if a.saida else Path(tempfile.mkdtemp(prefix="videoia_demo_"))
    base.mkdir(parents=True, exist_ok=True)
    v = Verif()
    print(f"Pasta de trabalho: {base}\n")

    # 1-2. bruto sintético + analise.json -------------------------------------
    print("[1] gerando bruto sintético (testsrc2 16:9 + fala sintética em pulsos)...")
    pal = palavras_sinteticas()
    gerar_audio(pal, base / "voz.wav")
    bruto = base / "bruto.mp4"
    ff(["-f", "lavfi", "-i", f"testsrc2=s=1280x720:r=30:d={DURACAO}", "-i", base / "voz.wav",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-shortest", bruto])
    pasta_an = base / "bruto_edicao"
    salvar_json(pasta_an / "analise.json", {
        "video": str(bruto.resolve()), "meta": probe(bruto),
        "transcricao": {"modelo": "sintetico", "idioma": "pt"},
        "palavras": pal, "alinhamento_fonetico": {"sintetico": True}})
    (base / "roteiro_exemplo.txt").write_text("\n".join(f for f, _ in FRASES) + "\n", encoding="utf-8")
    n1 = len(FRASES[0][0].split())
    morta = (pal[n1 - 1]["e"], pal[n1]["s"])          # silêncio entre a 1ª e a 2ª frase
    print(f"    {len(pal)} palavras em {len(FRASES)} frases; pausa morta de "
          f"{morta[1] - morta[0]:.2f}s entre a 1ª e a 2ª frase")

    # 3. plano ----------------------------------------------------------------
    print("\n[2] PLANO de corte (nada é renderizado aqui):")
    roda([SCRIPTS / "cortar_silencio.py", pasta_an])
    plano = carregar_json(pasta_an / "corte.json")
    corte_morta = [c for c in plano["cortes"] if c["de"] >= morta[0] - 0.01 and c["ate"] <= morta[1] + 0.01]
    removido = sum(c["removido"] for c in corte_morta)
    palavra_cortada = [p["w"].strip() for p in pal for c in plano["cortes"]
                       if min(p["e"], c["ate"]) - max(p["s"], c["de"]) > 0]
    v("plano encurta a pausa morta sem tocar em palavra",
      removido >= 0.6 * (morta[1] - morta[0]) and not palavra_cortada,
      f"{removido:.2f}s de {morta[1] - morta[0]:.2f}s removidos; "
      f"{plano['duracao_original']:.2f}s -> {plano['duracao_prevista']:.2f}s")

    # 4. aprovação humana -------------------------------------------------------
    print("\n[3] APROVAÇÃO do plano")
    if a.pausar:
        input("    Revise o plano acima e tecle Enter para aprovar (Ctrl+C cancela)... ")
    else:
        print("    (demo: aprovação automática; use --pausar para aprovar manualmente)")

    # 5. render ---------------------------------------------------------------
    print("\n[4] RENDER mecânico do EDL aprovado (preview 1080x1920)...")
    edl = {
        "saida": {"arquivo": "peca_demo.mp4", "pasta_trabalho": "peca_demo_render"},
        "identidade": str(RAIZ / "config" / "identidade_exemplo.json"),
        "clips": [{"src": str(bruto.resolve()), "analise": str(pasta_an.resolve()),
                   "modo_916": "blur", "cortar_silencio": True}],
        "cortar_silencio": plano["parametros"],
        "legendas": {"estilo": "karaoke", "tamanho": 88, "calar_silencio": 0.30},
        "overlays": [{"tipo": "barra_progresso", "altura": 12}],
        "endcard": {"texto": "FIM DO MÓDULO 1", "subtitulo": "exemplo.com", "dur": 1.5},
        "loudnorm": True,
    }
    edl_path = base / "edl.json"
    salvar_json(edl_path, edl)
    roda([SCRIPTS / "render_edl.py", edl_path, "--qualidade", "preview", "--forcar"])
    saida = base / "peca_demo.mp4"
    wk = base / "peca_demo_render"

    # 6. conferência medida na saída -------------------------------------------
    print("\n[5] CONFERÊNCIA medida na saída:")
    rel = carregar_json(wk / "relatorio.json")
    v("relatório do render sem problemas", not rel["problemas"], "; ".join(rel["problemas"]) or
      f"{rel['duracao_real']:.2f}s, {rel['segmentos']} segmentos")
    real = probe(saida)
    esperado = plano["duracao_prevista"] + 1.5
    v("duração da saída = plano + end card (±0,15 s)", abs(real["duracao"] - esperado) <= 0.15,
      f"real {real['duracao']:.2f}s x esperado {esperado:.2f}s")
    v("formato vertical 1080x1920 com áudio", real.get("largura") == 1080 and real.get("altura") == 1920
      and real["tem_audio"], f"{real.get('largura')}x{real.get('altura')}")

    from gerar_ass import verificar_cobertura
    ass_txt = (wk / "legendas.ass").read_text(encoding="utf-8-sig")
    pal_out = carregar_json(wk / "legenda_palavras.json")["palavras"]
    cob = verificar_cobertura(ass_txt, pal_out, 0.30)
    v("legenda: cada palavra com exatamente 1 evento, nada aceso no silêncio",
      not cob["problemas"] and cob["palavras"] == len(pal),
      "; ".join(cob["problemas"]) or f"{cob['palavras']} palavras, {cob['eventos']} eventos")

    x = pcm_mono(saida)
    piso = rms(x, 0, 0.05) if len(x) else 0.0
    fracas = [p["w"].strip() for p in pal_out
              if rms(x, p["s"] + 0.25 * (p["e"] - p["s"]), p["e"] - 0.25 * (p["e"] - p["s"])) < 0.02]
    v("nenhuma palavra decepada (energia presente no miolo de cada palavra remapeada)",
      not fracas and len(pal_out) == len(pal), f"{len(pal_out)} palavras medidas; fracas: {fracas}")
    ms = maior_silencio(x, plano["duracao_prevista"] - 0.3)
    v("pausa morta removida (maior silêncio no conteúdo < 0,8 s)", ms < 0.8, f"maior silêncio {ms:.2f}s")

    from PIL import Image
    meio = pal_out[len(pal_out) // 2]
    fr = base / "frame_legenda.png"
    ff(["-ss", f"{(meio['s'] + meio['e']) / 2:.3f}", "-i", saida, "-frames:v", "1", fr])
    img = np.asarray(Image.open(fr).convert("RGB")).astype(int)[760:960]   # faixa da legenda (centro, y~860)
    alvo = np.array([0xF2, 0xB1, 0x34])
    destaque = int((np.abs(img - alvo).sum(axis=2) < 60).sum())
    v("palavra ativa destacada na cor da identidade, queimada no frame", destaque > 400,
      f"{destaque} px na cor de destaque na faixa da legenda em t={(meio['s'] + meio['e']) / 2:.2f}s")

    # 7. transição ------------------------------------------------------------
    print("\n[6] TRANSIÇÃO zoom suave nas emendas...")
    acc, emendas = 0.0, []
    for c in plano["cutmap"][:-1]:
        acc += c["src_out"] - c["src_in"]
        emendas.append(round(acc, 3))
    trans = base / "peca_demo_transicao.mp4"
    roda([SCRIPTS / "transicoes.py", saida, trans, "--modo", "zoom_suave",
          "--cortes", ",".join(map(str, emendas)), "--sem-snap"])
    pt = probe(trans)
    v("transição não muda duração nem áudio", abs(pt["duracao"] - real["duracao"]) < 0.05 and pt["tem_audio"],
      f"{pt['duracao']:.2f}s x {real['duracao']:.2f}s, emendas em {emendas}")

    # 8. música ---------------------------------------------------------------
    print("\n[7] CAMA MUSICAL a 3,5% da voz (nível constante, medido)...")
    gerar_musica(base / "trilha.wav", 6.0)
    roda([SCRIPTS / "musica.py", "--peca", saida, "--faixa", base / "trilha.wav",
          "--saida", base / "final_musica", "--relatorio", base / "musica.json"])
    med = carregar_json(base / "musica.json")[saida.name]
    v("cama mixada no alvo relativo à voz medida", med["ok"] and abs(med["db"] - 20 * math.log10(0.035)) < 0.01,
      f"voz {med['voz_lufs']} LUFS, cama alvo {med['cama_alvo_lufs']} LUFS ({med['db']} dB)")

    # 9. auditoria visual -------------------------------------------------------
    print("\n[8] AUDITORIA VISUAL de frames (onde olhar)...")
    salvar_json(wk / "analise_corte" / "analise.json",
                {"meta": {"duracao": real["duracao"]}, "palavras": pal_out})
    r = roda([SCRIPTS / "auditar_video.py", saida, "--render", wk], ok=(0, 2))
    frames = list((wk / "_visual").rglob("*.png"))
    v("auditoria extraiu frames das pontas e vãos para conferência", len(frames) >= 4,
      f"{len(frames)} frames (exit {r.returncode}; 2 = há trecho sem fala para um humano olhar — "
      f"aqui, a entrada do end card, que é intencional)")

    # 10. pipeline com governança ------------------------------------------------
    print("\n[9] PIPELINE DE PEÇA com humano no circuito: plano -> montar -> aprovar")
    cas = {"roteiro": "roteiro_exemplo.txt", "takes": ["bruto_edicao"], "takes_usados": ["bruto_edicao"],
           "frases": len(FRASES), "frases_faltando": [], "cobertura_media": 1.0,
           "dur_util": plano["duracao_prevista"], "trocas_de_take": 0,
           "clips": [{"take": 0, "src": str(bruto.resolve()), "analise": str(pasta_an.resolve()),
                      "in": plano["cutmap"][0]["src_in"], "out": plano["cutmap"][-1]["src_out"],
                      "cutmap": [[c["src_in"], c["src_out"]] for c in plano["cutmap"]],
                      "frases": [1, len(FRASES)], "nome_take": "bruto_edicao",
                      "dur_util": plano["duracao_prevista"]}]}
    salvar_json(base / "casamento.json", cas)
    pp = SCRIPTS / "pipeline_peca.py"
    roda([pp, "plano", "--casamento", base / "casamento.json"])
    roda([pp, "montar", "--casamento", base / "casamento.json", "--roteiro", base / "roteiro_exemplo.txt",
          "--nome", "peca01", "--trabalho", base / "trabalho", "--legenda", "karaoke",
          "--sem-transcricao"], ok=(0,))
    r = roda([pp, "aprovar", "--trabalho", base / "trabalho", "--nome", "peca01",
              "--final", base / "final", "--revisor", "revisor-demo"], ok=(2,))
    v("aprovação RECUSADA sem os portões de áudio", r.returncode == 2, r.stderr.strip().splitlines()[-1][:140])
    roda([pp, "aprovar", "--trabalho", base / "trabalho", "--nome", "peca01", "--final", base / "final",
          "--revisor", "revisor-demo", "--override", "demo sintética: sem fala real para alinhar"])
    carimbo = carregar_json(base / "final" / "peca01.APROVADA.json")
    v("aprovação com override declarado gera carimbo auditável", "override" in carimbo and carimbo["sha"],
      f"carimbo: revisor={carimbo['revisor']}, edl_sha={carimbo['edl_sha']}, override registrado")

    # resumo --------------------------------------------------------------------
    ok = all(i["ok"] for i in v.itens)
    salvar_json(base / "demo_resultado.json", {"ok": ok, "verificacoes": v.itens})
    print(f"\n{'=' * 64}\n{sum(i['ok'] for i in v.itens)}/{len(v.itens)} verificações OK "
          f"-> {base / 'demo_resultado.json'}")
    if not a.saida and not a.manter:
        shutil.rmtree(base, ignore_errors=True)
        print("(pasta temporária apagada; use --manter ou --saida para guardar os vídeos)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
