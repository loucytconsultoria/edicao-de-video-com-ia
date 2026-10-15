# -*- coding: utf-8 -*-
"""PORTAO DE TRANSCRICAO — a terceira conferencia: o que nao pode sumir.

Os dois outros portoes respondem perguntas diferentes desta:

  - `checar_corte.py` mede se a palavra ESPERADA saiu integra no audio. Ele
    parte do cutmap: so procura o que o montador decidiu incluir. Se o
    montador decidiu NAO incluir um numero, o portao nao sente falta.
  - `auditar_video.py` olha o quadro. Nao sabe o que estava sendo dito.

Este aqui parte do ROTEIRO e pergunta o contrario: o que era essencial e nao
chegou na peca? Foi assim que passaram, sem nenhum alarme, numa versao sem
este portao:

  - a chamada final entregue sem o numero de contato (o bruto dizia o numero
    em dois tokens; o roteiro o tinha num so);
  - "isso ta muito longe de dizer" entregue sem o "longe";
  - "desde 1919" — erro de fato na gravacao — entregue como conteudo.

O que mede:

  A. OBRIGATORIAS. Palavras que nao podem sumir, do JSON de criticas mais as
     detectadas no roteiro: numeros e nomes proprios no meio de frase.
  B. PROIBIDAS. Trechos ditos errado que quem aprova mandou cortar.
  C. FECHO. A peca termina onde uma frase do roteiro termina?
  D. ABERTURA. A peca comeca no comeco de uma frase do roteiro?

Config: --criticas JSON (default $VIDEO_IA_CRITICAS ou
config/palavras_criticas_exemplo.json). Chaves por nome do roteiro sem
extensao; "todos" vale para todas as pecas.

Uso: python checar_palavras.py PASTA_RENDER --roteiro R.txt [--analise PASTA]
Exit: 0 aprovada | 2 reprovada | 1 crash.
"""
import argparse, json, os, re, sys, unicodedata

CRITICAS_PADRAO = os.environ.get("VIDEO_IA_CRITICAS") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "palavras_criticas_exemplo.json")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PENDURADO = re.compile(r"^(e|mas|entao|ai|isso|essa|esse|assim|tambem|depois|"
                       r"agora|pois|dai|que|se|de|do|da|em|no|na|com|por|pra|para)$")


def sa(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", sa(s))


def criticas_do_roteiro(rot_o):
    """Detecta sozinha o que nao pode sumir: NUMERO (fato) e NOME PROPRIO no
    meio de frase (identidade). Inicio de frase nao conta — maiuscula ali e
    ortografia, nao nome."""
    fora = set()
    inicio = True
    for w in rot_o:
        limpo = w.strip("\"'()[],.;:!?")
        if not limpo:
            continue
        if re.fullmatch(r"[\d.]+", limpo) and len(norm(limpo)) >= 2:
            fora.add(norm(limpo))
        elif not inicio and limpo[:1].isupper() and len(limpo) >= 3:
            fora.add(norm(limpo))
        inicio = bool(re.search(r"[.!?…]\"?\)?$", w))
    return fora


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta_render")
    ap.add_argument("--roteiro", required=True)
    ap.add_argument("--analise", default=None, help="pasta com analise.json da saida")
    ap.add_argument("--criticas", default=CRITICAS_PADRAO)
    a = ap.parse_args()

    nome = os.path.basename(os.path.abspath(a.pasta_render))
    an = os.path.join(a.analise or os.path.join(a.pasta_render, "analise_corte"), "analise.json")
    if not os.path.exists(an):
        print(f"sem analise_corte em {a.pasta_render}", file=sys.stderr)
        return 1
    d = json.load(open(an, encoding="utf-8"))
    fala_o = [w["w"].strip() for w in d["palavras"] if norm(w["w"])]
    fala = [norm(w) for w in fala_o]
    dito = set(fala)
    # numero partido: "15" + "20" na peca cobre "1520" do roteiro
    for i in range(len(fala) - 1):
        if fala[i].isdigit() and fala[i + 1].isdigit():
            dito.add(fala[i] + fala[i + 1])

    rot_txt = open(a.roteiro, encoding="utf-8").read()
    rot_o = [x for x in rot_txt.split() if norm(x)]
    rot = [norm(x) for x in rot_o]

    base = os.path.splitext(os.path.basename(a.roteiro))[0].replace("_CURTO", "")
    try:
        cfg = json.load(open(a.criticas, encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {"obrigatorias": {}, "proibidas": {}}

    print(f"\n### PALAVRAS — {nome}")
    problemas = []

    # ---- A. obrigatorias
    obrig = set()
    for k in ("todos", base):
        for w in (cfg.get("obrigatorias", {}).get(k) or []):
            obrig.add(norm(w))
    auto = criticas_do_roteiro(rot_o)
    # so cobra o que o roteiro DESTA versao realmente pede
    no_roteiro = set(rot)
    for i in range(len(rot) - 1):
        if rot[i].isdigit() and rot[i + 1].isdigit():
            no_roteiro.add(rot[i] + rot[i + 1])
    alvo = {w for w in (obrig | auto) if w in no_roteiro}

    faltando = sorted(w for w in alvo if w not in dito)
    print(f"  A. palavras que nao podem sumir: {len(alvo) - len(faltando)}/{len(alvo)} presentes")
    for w in faltando:
        orig = next((o for o in rot_o if norm(o) == w), w)
        problemas.append(f"PALAVRA CRITICA AUSENTE: \"{orig}\" esta no roteiro e nao esta na peca")

    # ---- B. proibidas
    proib = [x for x in (cfg.get("proibidas", {}).get(base) or []) if isinstance(x, dict)]
    for x in proib:
        seq = [norm(t) for t in x["trecho"].split() if norm(t)]
        n = len(seq)
        achou = any(fala[i:i + n] == seq for i in range(len(fala) - n + 1))
        if achou:
            problemas.append(f"TRECHO PROIBIDO NA PECA: \"{x['trecho']}\" ({x.get('motivo','')})")
    if proib:
        print(f"  B. trechos proibidos: {sum(1 for p in problemas if 'PROIBIDO' in p)} encontrado(s) "
              f"de {len(proib)} vigiado(s)")

    # ---- C/D. pontas
    fins = {i for i, w in enumerate(rot_o) if re.search(r"[.!?…]\"?\)?$", w)}
    if fala:
        ult = fala[-1]
        ok_fim = any(norm(rot_o[i]) == ult for i in fins)
        print(f"  C. fecho: \"{' '.join(fala_o[-5:])}\"" + ("" if ok_fim else "   <== nao e fim de frase do roteiro"))
        if not ok_fim and PENDURADO.match(ult):
            problemas.append(f"FECHO PENDURADO: a peca termina em \"{' '.join(fala_o[-4:])}\"")
        pri = fala[0]
        if PENDURADO.match(pri):
            problemas.append(f"ABERTURA PENDURADA: a peca comeca em \"{' '.join(fala_o[:4])}\"")
        print(f"  D. abertura: \"{' '.join(fala_o[:5])}\"")

    if problemas:
        print(f"  REPROVADA — {len(problemas)} problema(s):")
        for p in problemas:
            print(f"    - {p}")
        return 2
    print("  OK: nada essencial sumiu, nada proibido entrou, pontas em frase inteira")
    return 0


if __name__ == "__main__":
    sys.exit(main())
