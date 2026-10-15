# -*- coding: utf-8 -*-
"""Casa as linhas de um ROTEIRO com a TRANSCRIÇÃO word-timestamped de um ou mais
vídeos brutos, devolvendo onde cada linha foi falada (in/out em segundos) —
base para montar o EDL de um corte dirigido por roteiro.

Uso:
  python casar_roteiro.py ROTEIRO.txt PASTA_EDICAO [PASTA_EDICAO2 ...]
                          [--score-min 60] [--fora-de-ordem] [--saida casamento.json]

- ROTEIRO.txt: uma linha por trecho desejado (linhas vazias e comentários # são
  ignorados). A ordem das linhas é a ordem desejada no corte final.
- PASTA_EDICAO: pasta(s) com analise.json (gerado por analisar.py) de cada bruto.
- Matching fuzzy (rapidfuzz partial_ratio_alignment) tolera improviso, hesitação
  e sinônimos leves; score < --score-min é reportado como NÃO ENCONTRADO em vez
  de aceitar um casamento ruim (armadilha clássica do matching exato).
- Cursor monotônico por padrão: cada linha só é procurada a partir do fim da
  anterior (desambigua frases repetidas). --fora-de-ordem desliga isso.

Saída: casamento.json com [{linha, video, in, out, score, texto_falado}] +
relatório impresso. As bordas in/out são início da 1ª e fim da última palavra
casada — o padding fica por conta do corte (cortar_silencio/render_edl).
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import carregar_json, fail, salvar_json

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "".join(c if c.isalnum() or c.isspace() else " " for c in t)


def montar_indice(palavras: list[dict]) -> tuple[str, list[int]]:
    """Concatena as palavras normalizadas numa string única e devolve, em
    paralelo, char_to_word[i] = índice da palavra dona do caractere i."""
    partes, char_to_word = [], []
    for i, p in enumerate(palavras):
        w = normalizar(p["w"]).strip()
        if not w:
            w = "_"
        partes.append(w)
        char_to_word.extend([i] * (len(w) + 1))  # +1 pelo espaço separador
    return " ".join(partes), char_to_word


def casar(linhas: list[str], fontes: list[dict], score_min: float,
          monotonico: bool) -> list[dict]:
    from rapidfuzz import fuzz
    indices = []
    for f in fontes:
        texto, c2w = montar_indice(f["palavras"])
        indices.append({"texto": texto, "c2w": c2w, **f})

    resultados = []
    cursores = {i: 0 for i in range(len(indices))}  # offset de char por fonte
    for n, linha in enumerate(linhas):
        alvo = normalizar(linha).strip()
        melhor = None
        for fi, idx in enumerate(indices):
            base = cursores[fi] if monotonico else 0
            trecho = idx["texto"][base:]
            if len(trecho) < 3:
                continue
            al = fuzz.partial_ratio_alignment(alvo, trecho)
            if al is None:
                continue
            cand = {"score": al.score, "fonte": fi,
                    "c_ini": base + al.dest_start, "c_fim": base + al.dest_end}
            if melhor is None or cand["score"] > melhor["score"]:
                melhor = cand
        if melhor is None or melhor["score"] < score_min:
            resultados.append({"linha": n + 1, "roteiro": linha, "encontrado": False,
                               "score": round(melhor["score"], 1) if melhor else 0.0})
            continue
        idx = indices[melhor["fonte"]]
        c2w = idx["c2w"]
        wi = c2w[min(melhor["c_ini"], len(c2w) - 1)]
        wf = c2w[min(max(melhor["c_fim"] - 1, 0), len(c2w) - 1)]
        p_ini, p_fim = idx["palavras"][wi], idx["palavras"][wf]
        resultados.append({
            "linha": n + 1, "roteiro": linha, "encontrado": True,
            "video": idx["video"], "in": p_ini["s"], "out": p_fim["e"],
            "score": round(melhor["score"], 1),
            "texto_falado": "".join(w["w"] for w in idx["palavras"][wi:wf + 1]).strip(),
        })
        if monotonico:
            cursores[melhor["fonte"]] = melhor["c_fim"]
    return resultados


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roteiro")
    ap.add_argument("pastas", nargs="+", help="pastas de edição com analise.json")
    ap.add_argument("--score-min", type=float, default=60.0)
    ap.add_argument("--fora-de-ordem", action="store_true",
                    help="permite casar linhas fora da ordem de gravação")
    ap.add_argument("--saida", default=None)
    a = ap.parse_args()

    linhas = [ln.strip() for ln in Path(a.roteiro).read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.strip().startswith("#")]
    if not linhas:
        fail(f"roteiro vazio: {a.roteiro}")

    fontes = []
    for pasta in a.pastas:
        aj = Path(pasta) / "analise.json"
        if not aj.exists():
            fail(f"analise.json não encontrado em {pasta} — rode analisar.py antes.")
        an = carregar_json(aj)
        if not an["palavras"]:
            print(f"AVISO: {pasta} sem palavras transcritas — ignorando.")
            continue
        fontes.append({"video": an["video"], "palavras": an["palavras"]})
    if not fontes:
        fail("nenhuma fonte com transcrição utilizável.")

    resultados = casar(linhas, fontes, a.score_min, not a.fora_de_ordem)
    saida = Path(a.saida) if a.saida else Path(a.pastas[0]) / "casamento.json"
    salvar_json(saida, {"roteiro": a.roteiro, "score_min": a.score_min,
                        "resultados": resultados})

    ok = [r for r in resultados if r["encontrado"]]
    print(f"Casamento: {len(ok)}/{len(resultados)} linhas encontradas -> {saida}")
    for r in resultados:
        if r["encontrado"]:
            v = Path(r["video"]).name
            print(f"  L{r['linha']:02d} [{r['score']:5.1f}] {v} {r['in']:7.2f}-{r['out']:7.2f}  {r['roteiro'][:50]}")
        else:
            print(f"  L{r['linha']:02d} [{r['score']:5.1f}] NÃO ENCONTRADO: {r['roteiro'][:60]}")
    if len(ok) < len(resultados):
        print("Linhas não encontradas: o apresentador pode não ter gravado esse trecho, "
              "ou falou muito diferente do roteiro — revisar manualmente na transcrição.")


if __name__ == "__main__":
    main()
