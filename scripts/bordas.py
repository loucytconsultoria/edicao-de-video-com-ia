# -*- coding: utf-8 -*-
"""Onde a fala ACABA de verdade, e se a borda escolhida e segura.

Por que este arquivo existe: a revisao humana perguntou por que o corte nao
esperava as frases terminarem.

A resposta: a borda estava ancorada em DOIS numeros do transcritor — o fim
declarado da ultima palavra e o inicio declarado da palavra seguinte. O Whisper
fecha a palavra cedo (mediana de 180 ms numa gravacao medida, e ate 490 ms em
palavras terminadas em vogal longa), e o corte caia dentro desse atraso. Onze
de 38 bordas de um lote cortavam a palavra.

Aqui nada disso e suposto: mede-se a ENERGIA do audio.

  fim_acustico  — anda para a frente a partir do fim declarado ate a energia
                  cair e FICAR baixa (60 ms). E ali que a palavra acaba.
  respiro       — o tamanho desse silencio, com teto.
  borda segura  — so existe se houver silencio antes da palavra seguinte. Se a
                  pessoa emenda a frase, NAO ha corte limpo ali, e a borda e
                  reprovada em vez de ser cortada em cima da palavra. Quem
                  escolhe outra borda e o editor, nao o script.

Uso como modulo (montadores) ou pela linha de comando:
  python bordas.py VIDEO --fim 12.48 [--prox 12.71] [--ultimo]
  python bordas.py VIDEO --inicio 15.02 [--ant 14.40]
"""
import argparse
import json
import subprocess
import sys

import numpy as np

SR, HOP = 16000, 160          # 100 quadros por segundo
QUIETO = 8                    # 80 ms abaixo do limiar = fim da palavra
                              # (60 ms deixava passar oclusiva de /p/ /t/ /k/)
LIMIAR = 0.10                 # fracao do nivel de fala (p95 da janela)
RESPIRO_MAX = 0.32
RESPIRO_FIM = 0.60            # ultimo bloco da peca: nao ha proximo plano
                              # para mascarar o corte, entao ela respira mais
RESPIRO_MIN = 0.14
GUARDA = 0.12                 # espaco livre que fica ENTRE o corte e o ataque
                              # da palavra seguinte. Era 0,04 e nao bastava: 40 ms
                              # antes do ataque o ouvido ja escuta o comeco do que
                              # vem depois, e o corte soa como frase atropelada.
CAUDA_MAX = 0.45              # cauda plausivel de UMA palavra. Silencio achado
                              # depois disso ja e do outro lado da frase seguinte:
                              # medido em 'formulacao.' (1,02 s), 'aqui.' (1,13 s)
                              # e 'preto.' (0,60 s), onde a busca tinha atravessado
                              # a fala do vizinho e voltava um falso 'ok'.


def envoltoria(origem, t0, t1):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t0):.3f}",
                        "-t", f"{max(0.05, t1 - t0):.3f}", "-i", str(origem), "-vn",
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    x = np.frombuffer(p.stdout, dtype=np.float32)
    n = len(x) // HOP
    if n < 4:
        return None
    return np.sqrt((x[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1))


def _quieto_em(env, i, lim):
    return i + QUIETO <= len(env) and bool((env[i:i + QUIETO] < lim).all())


def melhor_vao(env, i0, lim, teto_i):
    """O MAIOR vao de silencio na janela, e onde ele comeca.

    Regra unica, e a que sobreviveu a todos os casos desta gravacao: o corte
    cai no maior vao disponivel dentro de 0,45 s depois do fim declarado. Se
    esse vao tem 80 ms ou mais, e pausa de verdade e a borda e folgada; se e
    menor, a palavra ainda esta inteira (o corte fica DEPOIS dela) mas ele
    emenda, e a borda sai marcada como apertada.

    As tentativas anteriores erravam por motivos opostos: parar no primeiro vao
    cortava dentro da palavra (o vao entre "pro" e "-pria" tem 60 ms), e andar
    ate a energia cair "de vez" atravessava a fala seguinte. Olhar TODOS os
    vaos da janela e escolher o maior resolve os dois."""
    melhor_i, melhor_n = None, -1
    i = i0
    while i < teto_i:
        if env[i] < lim:
            j = i
            while j < len(env) and env[j] < lim:
                j += 1
            if (j - i) > melhor_n:
                melhor_i, melhor_n = i, j - i
            i = j
        else:
            i += 1
    if melhor_i is None:
        return teto_i, 0
    return melhor_i, melhor_n


def fim_e_respiro(origem, fim_declarado, prox_declarado, teto=None, ultimo=False):
    """Devolve (fim_da_fala, respiro, folgado, motivo).

    Duas garantias, nesta ordem:

    1. **Nunca cortar dentro da palavra.** O corte cai no maior vao de silencio
       dentro de 0,45 s depois do fim DECLARADO, e nunca antes dele. Era este o
       defeito reprovado duas vezes na revisao ("cortou a ultima palavra antes
       da hora"): o Whisper fecha a palavra cedo — mediana de 180 ms, ate 490 ms
       — e o corte saia desse numero.
    2. **Respiro de verdade quando existe.** O respiro e o tamanho do vao,
       menos a guarda, com teto de 0,32 s (0,60 s no ultimo bloco da peca).

    `folgado` diz se o vao tem 80 ms ou mais. Quando nao tem, a palavra ainda
    esta inteira, mas ele emenda a frase seguinte: a borda sai marcada, para o
    editor decidir se troca de borda."""
    TETO_JANELA = 0.40
    t0 = fim_declarado - TETO_JANELA
    env = envoltoria(origem, t0, fim_declarado + CAUDA_MAX + 0.60)
    if env is None:
        return fim_declarado, (teto or RESPIRO_MAX), True, "fim do bruto"
    ref = float(np.percentile(env, 95))
    if ref <= 1e-6:
        return fim_declarado, (teto or RESPIRO_MAX), True, "silencio total"
    lim = LIMIAR * ref
    i0 = int(TETO_JANELA * 100)
    teto_i = min(len(env) - 1, int((TETO_JANELA + CAUDA_MAX) * 100))

    i_fim, n_vao = melhor_vao(env, i0, lim, teto_i)
    fim_ac = t0 + i_fim / 100.0
    atraso = fim_ac - fim_declarado
    silencio = n_vao / 100.0

    # O respiro sai de DENTRO da pausa, nunca de fora dela.
    #
    # Estava errado de duas maneiras, e as duas a revisao ouviu:
    #
    # 1. `max(RESPIRO_MIN, ...)` punha o piso POR CIMA da guarda. Numa pausa de
    #    0,08 s a conta devolvia respiro de 0,14 s — isto e, 60 ms DENTRO da
    #    palavra seguinte. O piso existia para garantir folego e acabava
    #    fabricando folego em cima do vizinho. Foi isso em "cultura" e "mental".
    # 2. A guarda de 40 ms deixava o corte encostado no ataque seguinte.
    #
    # Agora o piso e um DESEJO: quando a pausa nao o comporta, entrega-se o que
    # cabe e a borda sai MARCADA. Quem escolhe outra borda e o editor.
    # A guarda existe para proteger a palavra SEGUINTE. No ultimo bloco da peca
    # nao ha palavra seguinte — a peca acaba ali — entao a pausa inteira e
    # nossa, e o fecho pode respirar de verdade.
    guarda = 0.03 if ultimo else GUARDA
    livre = max(0.0, silencio - guarda)
    respiro = min(teto or RESPIRO_MAX, livre)
    if n_vao >= QUIETO and respiro + 1e-6 >= RESPIRO_MIN:
        return (fim_ac, round(respiro, 3), True,
                f"corte {atraso*1000:.0f}ms depois do declarado, "
                f"pausa de {silencio:.2f}s, respiro {respiro:.2f}s")
    if n_vao >= QUIETO:
        return (fim_ac, round(respiro, 3), False,
                f"corte {atraso*1000:.0f}ms depois do declarado; pausa de {silencio:.2f}s "
                f"so da {respiro:.2f}s de respiro (minimo {RESPIRO_MIN:.2f}s)")
    return (fim_ac, round(respiro, 3), False,
            f"corte {atraso*1000:.0f}ms depois do declarado; so {silencio:.2f}s de vao: ele emenda")


CABECA_MAX = 0.28             # ar que se pode tomar ANTES da primeira palavra


def cabeca_livre(origem, ini_ac, ant_declarado=None):
    """Quanto silencio REAL existe imediatamente antes de `ini_ac`.

    Serve para o outro lado da emenda. Quando a gravacao nao da respiro no fim
    de um bloco — ele emenda a frase seguinte e nao ha o que esperar —, o ar
    pode vir da CABECA do bloco que entra, que muitas vezes tem silencio de
    sobra e estava sendo jogado fora: o montador tomava 0,10 s fixos.

    Isso nao inventa nada e nao toca em palavra nenhuma: e silencio que ja
    estava gravado ali. O limite e a cauda da palavra anterior."""
    env = envoltoria(origem, ini_ac - (CABECA_MAX + 0.30), ini_ac + 0.05)
    if env is None:
        return 0.10
    ref = float(np.percentile(env, 95))
    if ref <= 1e-6:
        return CABECA_MAX
    lim = LIMIAR * ref
    i = int((CABECA_MAX + 0.30) * 100)
    i = min(i, len(env) - 1)
    n = 0
    while i - 1 >= 0 and env[i - 1] < lim and n < int(CABECA_MAX * 100) + 4:
        i -= 1
        n += 1
    livre = n / 100.0
    if ant_declarado is not None:
        livre = min(livre, max(0.0, ini_ac - ant_declarado - 0.04))
    return max(0.0, min(CABECA_MAX, livre))


def inicio_acustico(origem, ini_declarado, ant_declarado=None):
    """Onde a fala COMECA de verdade, andando para tras a partir do declarado."""
    t0 = ini_declarado - 0.60
    env = envoltoria(origem, t0, ini_declarado + 0.30)
    if env is None:
        return ini_declarado
    ref = float(np.percentile(env, 95))
    if ref <= 1e-6:
        return ini_declarado
    lim = LIMIAR * ref
    i = int(0.60 * 100)
    piso = 0 if ant_declarado is None else max(0, int((ant_declarado - t0) * 100))
    while i - 1 >= piso and env[i - 1] >= lim:
        i -= 1
    return max(0.0, t0 + i / 100.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--fim", type=float, help="fim declarado da ultima palavra do bloco")
    ap.add_argument("--prox", type=float, default=None, help="inicio declarado da palavra seguinte")
    ap.add_argument("--ultimo", action="store_true", help="ultimo bloco da peca (respira mais)")
    ap.add_argument("--inicio", type=float, help="inicio declarado da primeira palavra do bloco")
    ap.add_argument("--ant", type=float, default=None, help="fim declarado da palavra anterior")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = {}
    if a.fim is not None:
        fim, resp, ok, mot = fim_e_respiro(a.video, a.fim, a.prox, ultimo=a.ultimo)
        out["fim"] = {"fim_acustico": round(fim, 3), "respiro": resp, "folgado": ok, "motivo": mot}
    if a.inicio is not None:
        ini = inicio_acustico(a.video, a.inicio, a.ant)
        out["inicio"] = {"inicio_acustico": round(ini, 3),
                         "cabeca_livre": round(cabeca_livre(a.video, ini, a.ant), 3)}
    if not out:
        ap.error("informe --fim e/ou --inicio")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out.get("fim", {}).get("folgado", True) else 2


if __name__ == "__main__":
    sys.exit(main())
