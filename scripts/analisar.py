# -*- coding: utf-8 -*-
"""Passo 1 do pipeline: analisa um vídeo falado e grava analise.json com
timestamps por palavra (faster-whisper + VAD) e blocos de fala derivados.

Uso:
  python analisar.py VIDEO [--saida DIR] [--modelo small] [--device cuda]
                     [--idioma pt] [--pausa-bloco 0.45] [--force]

Saída (em DIR, default <video>_edicao/):
  analise.json  — metadados + palavras [{w,s,e,p}] + falas (blocos) + pausas

Cache: se analise.json já existe e o vídeo não mudou (tamanho+mtime), não
retranscreve — use --force para obrigar.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import probe, carregar_json, salvar_json, fail

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def blocos_de_fala(palavras: list[dict], pausa_bloco: float) -> list[dict]:
    """Agrupa palavras em blocos contínuos de fala: um gap entre palavras maior
    que `pausa_bloco` fecha o bloco. É a base do corte de silêncio e das legendas."""
    blocos = []
    atual = None
    for p in palavras:
        if atual is None:
            atual = {"s": p["s"], "e": p["e"], "palavras": [p]}
        elif p["s"] - atual["e"] <= pausa_bloco:
            atual["e"] = max(atual["e"], p["e"])
            atual["palavras"].append(p)
        else:
            blocos.append(atual)
            atual = {"s": p["s"], "e": p["e"], "palavras": [p]}
    if atual:
        blocos.append(atual)
    for b in blocos:
        b["texto"] = "".join(w["w"] for w in b["palavras"]).strip()
        b["s"], b["e"] = round(b["s"], 3), round(b["e"], 3)
    return blocos


def _tentar_transcricao(video: Path, modelo: str, dev: str, ct: str, idioma: str):
    """Carrega o modelo E consome o generator inteiro — erros de CUDA (dll
    ausente, VRAM) estouram no transcribe/iteração, não no construtor, então o
    fallback precisa envolver tudo."""
    from faster_whisper import WhisperModel
    m = WhisperModel(modelo, device=dev, compute_type=ct)
    # Guardas anti-alucinação (docs/licoes.md): sem elas o
    # Whisper repete frases em loop ou inventa texto sobre silêncio/ruído — e este
    # texto vira ponto de corte e legenda sem nenhum erro visível.
    # NÃO usar hallucination_silence_threshold: em teste, ele fez o
    # Whisper DESCARTAR a primeira fala legítima após ~2s de silêncio inicial —
    # e o corte de silêncio então come fala real. A defesa contra alucinação fica
    # com condition_on_previous_text=False + temperature=0 + checagens pós-hoc.
    segments, info = m.transcribe(str(video), language=idioma or None,
                                  word_timestamps=True, vad_filter=True,
                                  vad_parameters={"min_silence_duration_ms": 250},
                                  condition_on_previous_text=False,
                                  temperature=0.0,
                                  compression_ratio_threshold=2.4,
                                  log_prob_threshold=-1.0,
                                  no_speech_threshold=0.6)
    palavras, segs = [], []
    for seg in segments:
        segs.append({"s": round(seg.start, 3), "e": round(seg.end, 3), "texto": seg.text.strip()})
        for w in seg.words or []:
            palavras.append({"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3),
                            "p": round(w.probability, 2)})
    return palavras, segs, info


def transcrever(video: Path, modelo: str, device: str, idioma: str):
    if device in ("cuda", "auto"):
        try:
            palavras, segs, info = _tentar_transcricao(video, modelo, "cuda", "float16", idioma)
            palavras.sort(key=lambda p: p["s"])  # word timestamps do Whisper podem sair da ordem
            return palavras, segs, "cuda", info
        except Exception as e:
            print(f"AVISO: transcrição em CUDA falhou ({type(e).__name__}: {e}) — "
                  f"refazendo em CPU/int8...", file=sys.stderr)
    palavras, segs, info = _tentar_transcricao(video, modelo, "cpu", "int8", idioma)
    palavras.sort(key=lambda p: p["s"])
    return palavras, segs, "cpu", info


def suspeitas_alucinacao(segs: list[dict]) -> list[dict]:
    """Sinaliza segmentos com cara de alucinação: velocidade de fala implausível
    para PT-BR ou trigrama repetido 3+ vezes. Vai para revisão, não é corte."""
    achados = []
    for s in segs:
        dur = s["e"] - s["s"]
        pals = s["texto"].lower().split()
        trigramas = [" ".join(pals[i:i + 3]) for i in range(len(pals) - 2)]
        repete = trigramas and max(trigramas.count(t) for t in set(trigramas)) >= 3
        # cps só é confiável em segmento >= 2s (timing de segmento curto do
        # Whisper é impreciso e fala rápida real chega a ~28 cps)
        cps = len(s["texto"]) / dur if dur >= 2.0 else None
        if repete or (cps is not None and (cps < 3 or cps > 30)):
            achados.append({"s": s["s"], "e": s["e"], "texto": s["texto"][:80],
                            "motivo": "trigrama repetido" if repete else f"cps={cps:.0f}"})
    return achados


def analisar(video: Path, saida: Path, modelo: str, device: str, idioma: str,
             pausa_bloco: float, force: bool) -> dict:
    video = video.resolve()
    if not video.exists():
        fail(f"vídeo não encontrado: {video}")
    saida.mkdir(parents=True, exist_ok=True)
    alvo = saida / "analise.json"
    st = video.stat()
    assinatura = {"tamanho": st.st_size, "mtime": int(st.st_mtime)}

    if alvo.exists() and not force:
        try:
            antiga = carregar_json(alvo)
        except Exception:
            antiga = None  # cache corrompido (queda no meio da escrita): retranscreve
        if antiga and antiga.get("assinatura") == assinatura:
            print(f"[cache] {alvo} já cobre este vídeo — pulando transcrição (use --force para refazer).")
            return antiga

    meta = probe(video)
    if not meta["tem_audio"]:
        analise = {"video": str(video), "assinatura": assinatura, "meta": meta,
                   "sem_audio": True, "palavras": [], "falas": [], "pausas": []}
        salvar_json(alvo, analise)
        print(f"AVISO: vídeo sem trilha de áudio — analise.json gravado vazio em {alvo}")
        return analise

    t0 = time.time()
    palavras, segs, dev, info = transcrever(video, modelo, device, idioma)
    falas = blocos_de_fala(palavras, pausa_bloco)

    pausas = []
    if falas:
        if falas[0]["s"] > 0.05:
            pausas.append({"s": 0.0, "e": falas[0]["s"], "tipo": "inicio"})
        for a, b in zip(falas, falas[1:]):
            pausas.append({"s": a["e"], "e": b["s"], "tipo": "meio"})
        if meta["duracao"] - falas[-1]["e"] > 0.05:
            pausas.append({"s": falas[-1]["e"], "e": round(meta["duracao"], 3), "tipo": "fim"})

    suspeitas = suspeitas_alucinacao(segs)
    analise = {"video": str(video), "assinatura": assinatura, "meta": meta,
               "transcricao": {"modelo": modelo, "device": dev, "idioma": idioma,
                               "tempo_s": round(time.time() - t0, 1)},
               "palavras": palavras, "segmentos": segs, "falas": falas, "pausas": pausas,
               "suspeitas_alucinacao": suspeitas}
    salvar_json(alvo, analise)

    tot_pausa = sum(p["e"] - p["s"] for p in pausas)
    print(f"OK: {len(palavras)} palavras, {len(falas)} blocos de fala, "
          f"{len(pausas)} pausas ({tot_pausa:.1f}s de silêncio em {meta['duracao']:.1f}s) "
          f"[{dev}, {analise['transcricao']['tempo_s']}s] -> {alvo}")
    if suspeitas:
        print(f"AVISO: {len(suspeitas)} segmento(s) com cara de alucinação do Whisper — "
              f"revisar antes de cortar/legendar (ver suspeitas_alucinacao no analise.json):")
        for s in suspeitas[:5]:
            print(f"  {s['s']:.1f}-{s['e']:.1f}s [{s['motivo']}] {s['texto']}")
    return analise


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--saida", default=None, help="pasta de trabalho (default <video>_edicao/)")
    ap.add_argument("--modelo", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--idioma", default="pt")
    ap.add_argument("--pausa-bloco", type=float, default=0.45,
                    help="gap (s) entre palavras que fecha um bloco de fala")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    video = Path(a.video)
    saida = Path(a.saida) if a.saida else video.parent / f"{video.stem}_edicao"
    analisar(video, saida, a.modelo, a.device, a.idioma, a.pausa_bloco, a.force)


if __name__ == "__main__":
    main()
