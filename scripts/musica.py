# -*- coding: utf-8 -*-
"""Cama musical sob a voz, bem baixa, em NIVEL CONSTANTE e MEDIDO.

Uso:
    python musica.py --peca final/peca.mp4 --faixa trilhas/calma.mp3
    python musica.py --peca p.mp4 --faixa t.mp3 --relacao 0.035  # padrao
    python musica.py --peca p.mp4 --faixa t.mp3 --db -32         # mais baixa
    python musica.py --peca p.mp4 --faixa t.mp3 --ducking        # opcao explicita
    python musica.py --todas --mapa mapa.json --pasta final/     # lote

REGRA DE NIVEL (medida, nao chutada): o script MEDE a loudness integrada da voz
(ebur128, via loudnorm pass 1), mede a da faixa e calcula o alvo da cama como
`voz_medida + db`. Peca com voz a -16 LUFS e relacao 3,5% recebe a cama a
cerca de -45 LUFS.

  --relacao 0.035 ->  db = 20*log10(0.035) = -29,12 dB  (padrao)
  --db -29        ->  o mesmo, dito em dB (vence --relacao se os dois vierem)

O valor padrao foi escolhido OUVINDO em revisao humana: 25% e 5% ficaram altos,
2,5% quase inaudivel. Se a medicao falhar (peca sem audio, silencio absoluto),
cai para a presuncao de -16 LUFS e avisa.

NIVEL CONSTANTE E O PADRAO. A cama entra no alvo calculado e FICA LA a peca
inteira: nada de sidechain, nada de compressor disparado pela voz. Musica que
sobe e desce a cada pausa chama atencao para si e soa a truque de edicao; e,
com ducking, o nivel medido no trecho falado deixa de ser o nivel pedido.

  (padrao)     cama constante no alvo `voz + db`
  --ducking    sidechain (compressor disparado pela voz), so quando a peca pedir

Fade de 1,5s na entrada e 2,5s na saida (a cama vai a zero no fim) — corte
seco de musica no fim de peca soa a erro de edicao.

Licenca das faixas: use so musica com licenca clara para uso comercial
(CC0/dominio publico, ou CC BY com a atribuicao escrita na publicacao).

NAO altera a peca aprovada: escreve em outra pasta (--saida). O portao ja
carimbou a peca sem musica; a cama e camada de acabamento, e a original
continua disponivel se a musica for descartada.
"""
import argparse, json, math, os, re, subprocess, sys

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")

SAIDA = "final_musica"

VOZ_LUFS_PRESUMIDA = -16.0   # so entra se a medicao falhar
RELACAO_PADRAO = 0.035       # musica a 3,5% da voz (~29 dB abaixo), escolhido de ouvido
FADE_IN, FADE_OUT = 1.5, 2.5


def dur(caminho):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", caminho],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def medir_lufs(caminho):
    """Loudness integrada (LUFS) do primeiro stream de audio, por ebur128.

    Usa o pass 1 do loudnorm, que imprime input_i em JSON no stderr.
    Devolve None se nao houver audio medivel (silencio absoluto da -inf).
    """
    cmd = ["ffmpeg", "-v", "info", "-nostdin", "-i", caminho, "-map", "0:a:0",
           "-af", "loudnorm=print_format=json", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    m = re.findall(r'"input_i"\s*:\s*"(-?[\d.]+|-inf)"', r.stderr or "")
    if not m:
        return None
    try:
        v = float(m[-1])
    except ValueError:
        return None
    return None if not math.isfinite(v) else v


def db_de(relacao, db):
    """dB da cama em relacao a voz. --db vence --relacao."""
    if db is not None:
        return float(db)
    rel = RELACAO_PADRAO if relacao is None else float(relacao)
    if rel <= 0:
        raise SystemExit("--relacao tem que ser > 0")
    return 20.0 * math.log10(rel)


def mixar(peca, faixa, saida, db=None, relacao=None, verboso=True, ducking=False):
    """Mixa a cama sob a voz. Devolve (ok, medidas).

    ducking=False (padrao): nivel constante, a cama fica no alvo a peca
    inteira. ducking=True: sidechain disparado pela voz.
    """
    delta = db_de(relacao, db)
    d = dur(peca)
    ini_fade = max(0.0, d - FADE_OUT)

    voz = medir_lufs(peca)
    medido = voz is not None
    if not medido:
        voz = VOZ_LUFS_PRESUMIDA
    faixa_lufs = medir_lufs(faixa)

    alvo = max(-70.0, min(-5.0, voz + delta))   # faixa aceita pelo loudnorm
    medidas = {"voz_lufs": round(voz, 2), "voz_medida": medido,
               "faixa_lufs": None if faixa_lufs is None else round(faixa_lufs, 2),
               "db": round(delta, 2), "relacao": round(10 ** (delta / 20.0), 4),
               "cama_alvo_lufs": round(alvo, 2),
               "modo": "ducking" if ducking else "constante"}
    if verboso:
        if not medido:
            print("  ! voz nao medivel; presumindo -16 LUFS")
        print(f"  voz {medidas['voz_lufs']} LUFS · faixa {medidas['faixa_lufs']} LUFS "
              f"· cama alvo {medidas['cama_alvo_lufs']} LUFS ({medidas['db']} dB, "
              f"{medidas['relacao']:.1%} da voz) · modo {medidas['modo']}")

    # a cama repete (aloop) ate cobrir a peca e entra no alvo calculado.
    # PADRAO: nivel CONSTANTE, sem sidechain.
    cama = (
        f"[1:a]aloop=loop=-1:size=2e9,atrim=0:{d:.3f},"
        f"loudnorm=I={alvo:.2f}:TP=-2.0:LRA=7,"
        f"afade=t=in:st=0:d={FADE_IN},afade=t=out:st={ini_fade:.3f}:d={FADE_OUT}[cama];"
    )
    if ducking:
        # opcao explicita: o sidechain abaixa a cama enquanto ha voz
        fc = (cama +
              "[0:a]asplit=2[voz][chave];"
              "[cama][chave]sidechaincompress=threshold=0.02:ratio=6:"
              "attack=15:release=350[camad];"
              "[voz][camad]amix=inputs=2:duration=first:normalize=0[mix]")
    else:
        fc = (cama +
              "[0:a][cama]amix=inputs=2:duration=first:normalize=0[mix]")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", peca, "-i", faixa,
           "-filter_complex", fc, "-map", "0:v", "-map", "[mix]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-y", saida]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print((r.stderr or "")[-800:], file=sys.stderr)
        return False, medidas
    return True, medidas


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--peca")
    ap.add_argument("--faixa")
    ap.add_argument("--todas", action="store_true")
    ap.add_argument("--mapa", help="json {nome_da_peca: caminho_da_faixa}")
    ap.add_argument("--pasta", default="final", help="pasta das pecas do --todas")
    ap.add_argument("--relacao", type=float, default=None,
                    help="musica como fracao da voz (padrao 0.035 = 29 dB abaixo)")
    ap.add_argument("--db", type=float, default=None,
                    help="musica em dB abaixo da voz medida (padrao -29); vence --relacao")
    ap.add_argument("--saida", default=SAIDA, help=f"pasta de saida (padrao {SAIDA})")
    ap.add_argument("--ducking", action="store_true",
                    help="OPCAO EXPLICITA: sidechain que abaixa a cama enquanto "
                         "ha voz. O padrao e nivel CONSTANTE: musica que sobe e desce "
                         "chama atencao para si e o nivel medido deixa de ser o pedido.")
    ap.add_argument("--relatorio", help="grava as medidas em json")
    a = ap.parse_args()
    os.makedirs(a.saida, exist_ok=True)

    pares = []
    if a.todas:
        mapa = json.load(open(a.mapa, encoding="utf-8"))
        for nome, faixa in mapa.items():
            p = os.path.join(a.pasta, nome if nome.endswith(".mp4") else nome + ".mp4")
            if os.path.exists(p):
                pares.append((p, faixa))
            else:
                print(f"  ! peca nao encontrada: {nome}")
    else:
        pares.append((a.peca, a.faixa))

    rel = {}
    for peca, faixa in pares:
        out = os.path.join(a.saida, os.path.basename(peca))
        ok, medidas = mixar(peca, faixa, out, db=a.db, relacao=a.relacao,
                            ducking=a.ducking)
        medidas["ok"] = ok
        medidas["faixa"] = faixa
        rel[os.path.basename(peca)] = medidas
        print(f"  {'ok ' if ok else 'ERRO'} {os.path.basename(peca)}")

    if a.relatorio:
        with open(a.relatorio, "w", encoding="utf-8") as f:
            json.dump(rel, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
