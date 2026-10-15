# -*- coding: utf-8 -*-
"""Remove do analise.json as palavras que o preenchimento de "audio nao
explicado" do alinhar_fonetico.py inseriu e que sao ruido/duplicata, nao fala
nova de verdade.

Por que existe: medido em tres lotes de gravacao real que essa etapa alucina
sobre respiracao/boca/silencio na maior parte das vezes — e ISSO INCLUI
insercoes com fa ALTO (>0.7), que sao duplicata ou fragmento de uma palavra
vizinha que ja existia ("Dizendo" fa=0.959 duplicando "dizendo"; "Ali" fa=0.372
era pedaco de "Aliás"), nao fala perdida. Um limiar de score sozinho nao pega
esses casos. Ver docs/licoes.md.

Regra: remove toda palavra tag fa-retranscrito/fa-span-partido cujo texto
normalizado (sem acento, sem pontuacao) e igual ao de uma palavra vizinha (de
QUALQUER instrumento) dentro de --janela segundos — isso pega os casos de
score alto que sao duplicata/fragmento. Do que sobra, remove tambem quem tem
fa abaixo de --limiar (ruido puro, sem duplicata evidente). Sempre imprime o
que descartou, pra conferencia — isto e um FILTRO, nao um portao silencioso.

Uso:
  python limpar_gaps.py PASTA_ANALISE [--limiar 0.35] [--janela 1.0] [--dry-run]

Grava de volta em analise.json (guarda o original em analise.json.pre_limpeza
na primeira vez, mesmo padrao do .pre_fa do alinhar_fonetico). --dry-run só
mostra o que seria removido, não grava.
"""
import argparse, json, os, re, sys, unicodedata

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TAGS_GAP = ("fa-retranscrito", "fa-span-partido")


def norm(w):
    s = unicodedata.normalize("NFD", w.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta")
    ap.add_argument("--limiar", type=float, default=0.35)
    ap.add_argument("--janela", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cam = os.path.join(a.pasta, "analise.json")
    d = json.load(open(cam, encoding="utf-8"))
    pal = [w for w in d["palavras"] if w.get("s") is not None]
    pal.sort(key=lambda w: w["s"])

    candidatas = [w for w in pal if w.get("instrumento") in TAGS_GAP]
    descartar = []
    for w in candidatas:
        n = norm(w["w"])
        dup = any(
            v is not w and norm(v["w"]) == n
            and abs(v["s"] - w["s"]) <= a.janela
            for v in pal
        )
        if dup or (w.get("fa") or 0) < a.limiar:
            motivo = "duplicata/fragmento de vizinha" if dup else f"fa<{a.limiar}"
            descartar.append((w, motivo))

    if descartar:
        print(f"  descartando {len(descartar)} de {len(candidatas)} palavras de "
              f"preenchimento de gap:")
        for w, motivo in descartar:
            print(f"    [{w['s']:.2f}-{w['e']:.2f}] {w['w'].strip()!r} "
                  f"(fa={w.get('fa')}) — {motivo}")
    else:
        print(f"  nenhuma das {len(candidatas)} palavras de preenchimento de gap "
              "foi descartada")

    if a.dry_run:
        print("  (--dry-run: nada gravado)")
        return 0

    ids_fora = {id(w) for w, _ in descartar}
    novas = [w for w in pal if id(w) not in ids_fora]
    d["palavras"] = novas
    d.setdefault("etapas", []).append("limpar_gaps")
    if not os.path.exists(cam + ".pre_limpeza"):
        os.replace(cam, cam + ".pre_limpeza")
    tmp = cam + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, cam)
    print(f"  {cam}: {len(novas)} palavras (de {len(pal)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
