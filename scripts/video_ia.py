# -*- coding: utf-8 -*-
"""Ponto de entrada único do motor:  python scripts/video_ia.py <comando> [args]

Cada comando é um script desta pasta e aceita --help. Os scripts também rodam
diretamente (python scripts/<script>.py), de qualquer diretório.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COMANDOS = {
    # etapa: (script, descrição)
    "analisar":        ("analisar.py", "transcrição com faster-whisper -> analise.json (texto por palavra)"),
    "alinhar":         ("alinhar_fonetico.py", "alinhamento fonético forçado (MMS_FA): a régua de tempo"),
    "limpar-gaps":     ("limpar_gaps.py", "remove inserções de áudio não explicado que são ruído"),
    "calibrar-fa":     ("calibrar_fa.py", "mede o viés do alinhador contra a energia (define pads)"),
    "cortar-silencio": ("cortar_silencio.py", "plano de corte de pausas (sem cortar palavra)"),
    "casar-roteiro":   ("casar_roteiro.py", "onde cada linha do roteiro foi dita (fuzzy)"),
    "melhor-de":       ("melhor_de.py", "montador best-of entre takes, frase a frase"),
    "bordas":          ("bordas.py", "fim/início de fala medido por energia"),
    "peca":            ("pipeline_peca.py", "pipeline de uma peça: plano | montar | aprovar"),
    "render":          ("render_edl.py", "EDL JSON -> vídeo (mecânico, re-executável)"),
    "legendas":        ("gerar_ass.py", "legendas ASS (palavra ativa) + portão de cobertura"),
    "transicoes":      ("transicoes.py", "zoom suave subpixel / blur / push nos cortes"),
    "rostos":          ("rostos.py", "detecção de rosto (YuNet) para enquadrar"),
    "musica":          ("musica.py", "cama musical em nível constante medido"),
    "capa":            ("capa.py", "capa e end card na identidade visual"),
    "exportar":        ("exportar.py", "normaliza um vídeo pronto para a spec vertical"),
    "lote":            ("lote.py", "lote com manifest e retomada idempotente"),
    "checar-corte":    ("checar_corte.py", "PORTÃO: palavra decepada/perdida, engasgo, repetição"),
    "checar-palavras": ("checar_palavras.py", "PORTÃO: o que não podia sumir / entrar"),
    "checar-legenda-rosto": ("checar_legenda_rosto.py", "PORTÃO: legenda sobre o rosto"),
    "auditar":         ("auditar_video.py", "auditoria visual: frames de vãos e emendas"),
    "calibrar-libass": ("calibrar_libass.py", "fator de escala PIL -> libass de uma fonte"),
}


def ajuda():
    print(__doc__)
    print("Comandos:")
    for nome, (script, desc) in COMANDOS.items():
        print(f"  {nome:<22} {desc}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "ajuda"):
        ajuda()
        return 0
    cmd = sys.argv[1]
    if cmd not in COMANDOS:
        print(f"comando desconhecido: {cmd}\n")
        ajuda()
        return 2
    script = AQUI / COMANDOS[cmd][0]
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(AQUI))
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
