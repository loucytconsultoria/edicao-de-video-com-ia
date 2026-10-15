# -*- coding: utf-8 -*-
"""Montagem BEST-OF entre takes, com a FRASE como unidade atomica.

Historico das falhas que este arquivo corrige (todas encontradas em entrega):

1. Recorte por frase do tipo "do inicio da primeira palavra ao fim da ultima"
   engloba a tentativa abortada quando o ASR casa metade da frase na ruim e
   metade na boa -> repeticao no video. Corrigido: o recorte e por palavra.

2. Alinhamento global (LCS do roteiro inteiro contra o take inteiro) maximiza
   palavras casadas SEM SE IMPORTAR de qual tentativa vem cada palavra. O
   resultado e uma frase montada com pedacos de tres execucoes diferentes —
   costura que nao existe na fala e que perde o sentido. Corrigido aqui: cada
   frase e procurada por janela deslizante e sai de UMA execucao contigua; o
   DP escolhe qual.

3. Gaguejo sobrevivia por causa de uma tolerancia cega ("ate 3 palavras a
   mais"). Corrigido: sobra de fala e classificada — erro de transcricao
   (mantem, senao picota a frase), insercao pura (corta) ou regravacao (corta).

Restricao dura mantida: so mistura takes do MESMO grupo de enquadramento.

Uso:
  python melhor_de.py ROTEIRO.txt PASTA_TAKE1 [PASTA_TAKE2 ...] --saida casamento.json

Cada PASTA_TAKE tem o analise.json do bruto JA realinhado por
alinhar_fonetico.py (sem isso o montador aborta com exit 3). A saida e o
casamento: clipes com [in,out] e o cutmap interno medido, pronto para virar EDL
(pipeline_peca.py) e para o portao (checar_corte.py --casamento).

Trechos proibidos (erro de fato que nao pode ir ao ar): config JSON em
$VIDEO_IA_CRITICAS (default config/palavras_criticas_exemplo.json), chave
"proibidas" -> nome do roteiro sem extensao.
"""
import argparse, difflib, json, os, re, sys, unicodedata

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COB_MIN = 0.55
CUSTO_TROCA = 14.0
# Larguras ate 1,35x nao cobriam frase com improviso no meio: o apresentador
# acrescentou 8 palavras no meio da frase e a janela curta venceu, jogando fora
# a oracao que trazia os dois numeros do roteiro. Perder conteudo do roteiro
# custa mais caro que incluir fala a mais que foi dita.
LARGURAS = (0.72, 0.88, 1.0, 1.15, 1.35, 1.6, 1.9)
PESO_COBERTURA = 160.0
PESO_RUIDO = 1.8
RESPIRO_FALANTE = 0.60


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", s)


def frases_do_roteiro(txt):
    """[(i1,i2)] em indices de palavra normalizada.

    Fim de paragrafo e fronteira DURA. Sem isso, uma frase curta de CTA como
    "Inscreva-se no curso." (3 palavras) nao fechava a unidade minima de 4 e era
    grudada na frase anterior; como as duas ficam em pontos distantes do bruto,
    a janela so achava a primeira parte e o CTA sumia da peca entregue."""
    brutas, fim_para = [], set()
    for para in [p for p in txt.split("\n") if p.strip()]:
        for w in para.split():
            if norm(w):
                brutas.append(w)
        fim_para.add(len(brutas))

    corte = [k + 1 for k, w in enumerate(brutas) if re.search(r"[.!?…]\"?\)?$", w)]
    corte = sorted(set(corte) | fim_para | {len(brutas)})

    fr, ini = [], 0
    for f in corte:
        if f - ini >= 4 or f in fim_para:
            if f - ini >= 2:
                fr.append((ini, f)); ini = f
    if ini < len(brutas) and fr:
        fr[-1] = (fr[-1][0], len(brutas))
    elif ini < len(brutas):
        fr.append((ini, len(brutas)))
    return fr, brutas


FILLERS = {"eh", "ah", "ahn", "an", "hum", "hmm", "aa", "ee", "uhm",
           "aham", "ahm"}   # so nao-palavras: "e", "ai", "assim", "ta",
# "entao" e "ne" sao palavras de verdade e marcar como muleta reprovava
# fala legitima ("E tem um segundo ponto...")


def candidatos(alvo, pal, max_cand=5, video=None, nivel_ref=None):
    """Todas as execucoes plausiveis da frase no take, por janela deslizante.
    E aqui que se garante que a frase sai de UM trecho continuo — nao de um
    remendo de tentativas diferentes."""
    m, n = len(alvo), len(pal)
    tk = [p["n"] for p in pal]
    brutos = []
    for f in LARGURAS:
        W = max(3, int(round(m * f)))
        if W > n:
            continue
        for i in range(0, n - W + 1):
            jan = tk[i:i + W]
            sm = difflib.SequenceMatcher(None, alvo, jan, autojunk=False)
            blocos = sm.get_matching_blocks()
            casadas = sum(b.size for b in blocos)
            cov = casadas / m
            if cov < COB_MIN:
                continue
            # Fala fora do roteiro tem DOIS tipos e pesos opostos. Tratar tudo
            # como ruido fazia a janela fugir do improviso e jogar fora conteudo
            # legitimo (uma oracao com numero foi perdida assim). Repeticao e
            # falsa partida encarecem; improviso fluente e quase de graca —
            # quem fala mudou a frase e pode ter ficado melhor.
            ok = {b.b + k for b in blocos for k in range(b.size)}
            disf = improv = 0
            k2 = 0
            while k2 < W:
                if k2 in ok:
                    k2 += 1; continue
                fim2 = k2
                while fim2 < W and fim2 not in ok:
                    fim2 += 1
                run = jan[k2:fim2]
                seg = [p["n"] for p in pal[i + fim2:i + fim2 + len(run)]]
                ant = [p["n"] for p in pal[max(0, i + k2 - len(run)):i + k2]]
                if run == seg or run == ant or all(w in FILLERS for w in run):
                    disf += len(run)
                else:
                    improv += len(run)
                k2 = fim2
            conf = sum(p["p"] for p in pal[i:i + W]) / W
            # PENALIDADE POR TRUNCAR A FRASE: a janela pode casar bem o comeco
            # e parar no meio ("...antes de|" — faltou "salvar o arquivo no
            # servidor"). Cobertura sozinha nao pega, porque as palavras que
            # faltam sao poucas; mas o ouvinte escuta a frase morrer. Se as
            # ULTIMAS palavras do alvo ficaram de fora, encarece forte.
            # Tolerancia FIXA de 2 palavras, nao percentual: numa frase de 53
            # palavras, 15% deixava 8 de fora e a janela que corta em "antes
            # de|" passava. O que o ouvinte percebe e a frase morrer, e isso
            # independe do tamanho.
            fim_alvo = max((b.a + b.size for b in blocos if b.size), default=0)
            faltam = m - fim_alvo
            trunca = 0.0 if faltam <= 2 else min(90.0, 18.0 * (faltam - 2))
            # NIVEL DE VOZ: trecho gravado com o microfone longe soa errado
            # ao lado dos vizinhos, e a loudnorm da peca nao corrige (ela
            # normaliza o conjunto, entao a diferenca entre trechos continua).
            pen_nivel = 0.0
            if video and nivel_ref is not None:
                nv = nivel_de_fala(video, pal[i]["s"], pal[i + W - 1]["e"] - pal[i]["s"])
                if nv is not None:
                    queda = nivel_ref - nv
                    if queda > 6.0:
                        pen_nivel = min(150.0, 14.0 * (queda - 6.0))
            custo = ((1 - cov) * PESO_COBERTURA + disf * 4.0
                     + improv * 0.6 + (1 - conf) * 25 + trunca + pen_nivel)
            brutos.append((custo, i, i + W, cov, disf + improv, conf))
    if not brutos:
        return []
    brutos.sort()
    esc = []
    for c in brutos:
        _, i, j = c[0], c[1], c[2]
        if any(min(j, b) - max(i, a) > 0.5 * (j - i) for _, a, b, *_ in esc):
            continue
        esc.append(c)
        if len(esc) >= max_cand:
            break
    return esc






_ENVELOPE = {}


def _envelope_do_take(video, jan=0.05):
    """Envelope RMS (dBFS) da GRAVACAO INTEIRA, calculado uma unica vez.

    A primeira versao chamava ffmpeg por janela candidata — milhares de
    processos, e a montagem de uma peca nao terminava em 10 minutos. O
    envelope inteiro cabe em memoria (uma amostra a cada 50 ms: 20 por
    segundo, ~6 mil numeros numa gravacao de 5 minutos) e responde a qualquer
    intervalo por fatiamento."""
    import numpy as np, wave, subprocess, os, tempfile
    if video in _ENVELOPE:
        return _ENVELOPE[video]
    wav = os.path.join(tempfile.gettempdir(), f"videoia_env_{os.getpid()}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", video, "-vn",
                    "-ar", "16000", "-ac", "1", "-y", wav], capture_output=True)
    if not os.path.exists(wav):
        _ENVELOPE[video] = (None, jan)
        return _ENVELOPE[video]
    try:
        with wave.open(wav, "rb") as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype="int16").astype("float32")
    except Exception:
        _ENVELOPE[video] = (None, jan)
        return _ENVELOPE[video]
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    n = int(16000 * jan)
    if len(pcm) < 2 * n:
        _ENVELOPE[video] = (None, jan)
        return _ENVELOPE[video]
    r = np.sqrt(np.mean(np.square(pcm[:len(pcm) // n * n].reshape(-1, n)), axis=1))
    _ENVELOPE[video] = (20 * np.log10(np.maximum(r, 1e-6) / 32768.0), jan)
    return _ENVELOPE[video]


def nivel_de_fala(video, ini, dur):
    """p75 do envelope no intervalo — o nivel do que E falado, sem as pausas."""
    import numpy as np
    db, jan = _envelope_do_take(video)
    if db is None or dur < 0.30:
        return None
    a0 = max(0, int(ini / jan))
    b0 = min(len(db), int((ini + dur) / jan))
    if b0 - a0 < 4:
        return None
    return float(np.percentile(db[a0:b0], 75))


def eh_disfluencia(pal, pj, cj):
    """A fala sobrando entre duas palavras casadas e defeito ou fala natural?

    Nao basta "diferente do roteiro": quem fala solto acrescenta artigo e
    pronome o tempo todo ("revisa AS metas", "EU passei os ultimos anos").
    Cortar isso mutila a frase e ainda arrisca decepar o ataque da
    palavra seguinte num corte de 0,1s. So e defeito quando o trecho REPETE o
    que vem antes ou depois (falsa partida/gaguejo) ou e so muleta sonora."""
    run = [pal[k]["n"] for k in range(pj + 1, cj)]
    if not run:
        return False
    # FRAGMENTO NAO E REPETICAO. O CTC reparte uma vogal e devolve um segundo
    # token de ~50 ms com o mesmo texto; tratar isso como gaguejo faz o corte
    # ficar com o FRAGMENTO e jogar fora a palavra de verdade. Medido:
    #   'longe' 159.439-159.639 (200 ms, a palavra)
    #   'longe' 159.639-159.690 ( 51 ms, o fragmento)
    # O corte ficou com o de 51 ms e a palavra sumiu no meio da frase.
    if all((pal[k]["e"] - pal[k]["s"]) < 0.08 for k in range(pj + 1, cj)):
        return False
    if all(w in FILLERS for w in run):
        return True
    # REPETIR UM FRAGMENTO NAO E REPETIR. O difflib pode casar a palavra do
    # roteiro com o FRAGMENTO de 51 ms em vez da palavra de 200 ms; a palavra
    # boa vira "sobra" e e cortada (o mesmo caso: 'longe' 200 ms seguido de
    # 'longe' 51 ms, e a peca perdeu a palavra). Se o lado com que o trecho
    # "repete" e so fragmento, quem sobra e a fala de verdade: nao corta.
    def _so_fragmento(ini_, fim_):
        idx = range(max(0, ini_), min(len(pal), fim_))
        return bool(list(idx)) and all((pal[k]["e"] - pal[k]["s"]) < 0.08 for k in idx)

    seguinte = [p["n"] for p in pal[cj:cj + len(run)]]
    if run == seguinte and not _so_fragmento(cj, cj + len(run)):
        return True                          # comecou, parou e recomecou
    anterior = [p["n"] for p in pal[max(0, pj + 1 - len(run)):pj + 1]]
    if run == anterior and not _so_fragmento(pj + 1 - len(run), pj + 1):
        return True                          # repetiu o que acabou de dizer
    # Falsa partida com PALAVRA TROCADA: "Parece dificil abrir o chamado
    # disso." / "Parece simples abrir o chamado de tudo isso...".
    # Nao e repeticao literal, e a comparacao POSICIONAL falha por causa de um
    # token extra no meio ('isso.' entre as duas tentativas desalinha tudo).
    # Criterio certo: similaridade de SEQUENCIA (difflib) com a janela que vem
    # logo depois, tolerando insercao/troca.
    if len(run) >= 4:
        # (a) a run RECOMECA dentro de si mesma: a palavra que a abre reaparece
        # no fim. "Parece dificil abrir o chamado disso isso Parece"
        # — o segundo "Parece" e o inicio da tentativa boa. Sinal mais forte e
        # direto que comparar com a janela seguinte (que ja pulou esse token).
        if run[0] == run[-1] and len(run) >= 5:
            return True
        # (b) a run e quase igual ao que vem depois (recomeco com troca de
        # palavra), tolerando insercao — comparacao de SEQUENCIA, nao posicional
        jan = [p["n"] for p in pal[cj:cj + len(run) + 3]]
        if jan and difflib.SequenceMatcher(None, run, jan, autojunk=False).ratio() >= 0.55:
            return True
    return False


def _rms(video, ini, dur, jan=0.01):
    import subprocess, wave, os, tempfile
    import numpy as np
    wav = os.path.join(tempfile.gettempdir(), f"videoia_sil_{os.getpid()}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(max(0.0, ini)),
                    "-i", video, "-t", str(max(0.05, dur)), "-vn", "-ar", "16000",
                    "-ac", "1", "-y", wav], capture_output=True)
    if not os.path.exists(wav):
        return None
    with wave.open(wav, "rb") as f:
        pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32)
    os.remove(wav)
    n = int(16000 * jan)
    if len(pcm) < 2 * n:
        return None
    return np.array([np.sqrt(np.mean(pcm[i:i + n] ** 2)) for i in range(0, len(pcm) - n, n)])


def _silencio_apos(video, t_pal, t_corte):
    """segundos de SILENCIO REAL entre o fim da fala e o corte"""
    if t_corte - t_pal <= 0.02:
        return 0.0
    # janela: 0,25s ANTES do fim da palavra (para ter fala no quadro e calibrar
    # o limiar) ate 0,30s depois do corte. O indice de t_pal e 25 janelas de
    # 10ms; contar dali para frente. A versao anterior usava o mesmo indice
    # para as duas funcoes e media 0,00 onde havia 0,22s.
    pre = 0.25
    r = _rms(video, t_pal - pre, (t_corte - t_pal) + pre + 0.30)
    if r is None or len(r) < 5:
        return max(0.0, t_corte - t_pal)
    import numpy as np
    p15 = np.percentile(r, 15)
    lim = p15 + 0.12 * (r.max() - p15)
    k = int(pre / 0.01)
    n = 0
    while k + n < len(r) and r[k + n] < lim and n * 0.01 < (t_corte - t_pal) + 0.05:
        n += 1
    return min(n * 0.01, max(0.0, t_corte - t_pal))


def _silencio_antes(video, t_corte, t_pal):
    """segundos de SILENCIO REAL entre o corte e o inicio da fala"""
    if t_pal - t_corte <= 0.02:
        return 0.0
    r = _rms(video, t_corte - 0.05, (t_pal - t_corte) + 0.30)
    if r is None or len(r) < 5:
        return max(0.0, t_pal - t_corte)
    import numpy as np
    lim = np.percentile(r, 15) + 0.12 * (r.max() - np.percentile(r, 15))
    k = int((t_pal - t_corte) / 0.01) + 5
    n = 0
    while k - n > 0 and r[k - n] < lim and n * 0.01 < (t_pal - t_corte) + 0.05:
        n += 1
    return min(n * 0.01, t_pal - t_corte)


def fim_da_fala(video, t, limite=0.8, ruido="-38dB", minimo=0.10):
    """Onde a fala REALMENTE acaba depois de t, medido no audio.

    O timestamp de fim do ASR nao serve de borda: quando o Whisper alucina algo
    logo apos a ultima palavra, ele encurta essa palavra para caber a alucinacao.
    Foi assim que uma palavra de 8 letras saiu com 0,22s (impossivel de pronunciar) e o final
    ficou decepado — o invariante protegia um fim que ja vinha errado.
    Aqui o corte so termina onde o silencio comeca de fato."""
    import subprocess
    ini = max(0.0, t - 0.25)
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-nostdin", "-ss", str(ini), "-i", video,
         "-t", str(limite + 0.55), "-af",
         f"silencedetect=noise={ruido}:d={minimo}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    marcas = re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr or "")
    for m in marcas:
        abs_t = ini + float(m)
        if abs_t > t + 0.02:                 # primeiro silencio APOS a palavra
            return min(abs_t, t + limite)
    return None


def pares_da_janela(alvo, i1, pal, j0, j1):
    """mapeia palavra do roteiro -> palavra do take DENTRO da janela escolhida"""
    sm = difflib.SequenceMatcher(None, alvo, [p["n"] for p in pal[j0:j1]],
                                 autojunk=False)
    pares = []
    for b in sm.get_matching_blocks():
        if b.size < 1:
            continue
        for k in range(b.size):
            pares.append((i1 + b.a + k, j0 + b.b + k))
    return pares


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roteiro")
    ap.add_argument("takes", nargs="+")
    ap.add_argument("--saida", required=True)
    # Pads para a regua FONETICA (nao reaproveitar os do Whisper): medido em
    # palavras isoladas, o FA ja cobre o envelope de energia (fim marcado ate
    # 0,12s DEPOIS de a energia acabar). Aqui o pad e respiro, nao correcao.
    ap.add_argument("--pad-antes", type=float, default=0.10)
    ap.add_argument("--pad-depois", type=float, default=0.22)
    ap.add_argument("--pad-fim-peca", type=float, default=0.45)
    ap.add_argument("--pausa-min", type=float, default=None,
                    help="pausa (medida) a partir da qual se ENCURTA silencio dentro do clipe; "
                         "default = p90 das pausas do falante entre frases (nunca abaixo de 1,2s)")
    ap.add_argument("--respiro", type=float, default=None,
                    help="quanto de pausa fica quando uma pausa longa e encurtada; "
                         "default = mediana das pausas do falante entre frases")
    ap.add_argument("--custo-troca", type=float, default=CUSTO_TROCA)
    a = ap.parse_args()

    txt = open(a.roteiro, encoding="utf-8").read()
    fr, rot = frases_do_roteiro(txt)
    rot_n = [norm(w) for w in rot]

    takes = []
    for pasta in a.takes:
        d = json.load(open(os.path.join(pasta, "analise.json"), encoding="utf-8"))
        pal = [{"w": w["w"].strip(), "n": norm(w["w"]), "s": w["s"], "e": w["e"],
                "p": w.get("p", 1.0), "instrumento": w.get("instrumento", "")}
               for w in d["palavras"] if norm(w["w"])]
        # NUMERO PARTIDO EM DOIS TOKENS. Um codigo de 4 digitos dito em pares
        # ("quinze, vinte") sai do ASR como ' 15' + '-20.' — dois tokens. O
        # roteiro tem "1520", um so. Nao casam, a janela da ultima frase fecha
        # antes do numero, e a peca termina com o numero cortado (achado na
        # revisao humana). Aqui os pedacos numericos vizinhos sao unidos quando
        # a concatenacao existe no roteiro.
        _numeros_rot = {x for x in rot_n if x.isdigit() and len(x) >= 3}
        k = 0
        while k < len(pal) - 1:
            a_, b_ = pal[k]["n"], pal[k + 1]["n"]
            if (a_.isdigit() and b_.isdigit()
                    and (a_ + b_) in _numeros_rot
                    and pal[k + 1]["s"] - pal[k]["e"] < 0.60):
                pal[k] = {"w": pal[k]["w"] + pal[k + 1]["w"], "n": a_ + b_,
                          "s": pal[k]["s"], "e": pal[k + 1]["e"],
                          "p": min(pal[k]["p"], pal[k + 1]["p"]),
                          "instrumento": "numero-unido"}
                del pal[k + 1]
                continue
            k += 1
        takes.append({"pasta": pasta, "pal": pal, "dur": d["meta"]["duracao"],
                      "video": d.get("video", ""),
                      "nome": os.path.basename(pasta),
                      "fa": bool(d.get("alinhamento_fonetico"))})

    # ritmo do falante (pausa apos ponto final): usado nas bordas e no corte interno
    import statistics as _st0
    _pf = []
    for tk_ in takes:
        pw = tk_["pal"]
        for k in range(1, len(pw)):
            g = pw[k]["s"] - pw[k - 1]["e"]
            if 0 < g < 3.0 and re.search(r"[.!?…]$", pw[k - 1]["w"]):
                _pf.append(g)
    global RESPIRO_FALANTE
    RESPIRO_FALANTE = _st0.median(_pf) if len(_pf) >= 5 else 0.60

    # NIVEL DE REFERENCIA DE CADA TAKE: mediana do nivel de fala em amostras ao
    # longo da gravacao. E contra ela que cada candidato e comparado — o que
    # importa nao e o valor absoluto, e a diferenca audivel entre um trecho e o
    # resto. Sem isto, uma frase gravada com o microfone longe (-37 dB contra
    # -21 dB dos vizinhos, medido) vencia por ter uma palavra a mais casada
    # com o roteiro.
    import statistics as _st1
    for t_ in takes:
        pl_ = t_["pal"]
        amostras = []
        if pl_:
            passo = max(1, len(pl_) // 12)
            for k_ in range(0, len(pl_) - 1, passo):
                fim_ = min(len(pl_) - 1, k_ + 12)
                nv_ = nivel_de_fala(t_["video"], pl_[k_]["s"], pl_[fim_]["e"] - pl_[k_]["s"])
                if nv_ is not None:
                    amostras.append(nv_)
        t_["nivel_ref"] = _st1.median(amostras) if amostras else None
        if t_["nivel_ref"] is not None:
            print(f"  nivel de fala de {t_['nome'][-6:]}: {t_['nivel_ref']:.1f} dB")

    # candidatos por (frase, take)
    C = []
    for (i1, i2) in fr:
        alvo = rot_n[i1:i2]
        C.append([candidatos(alvo, t["pal"], video=t["video"],
                             nivel_ref=t.get("nivel_ref")) for t in takes])

    # estados: (take, indice do candidato)
    estados = []
    for s in range(len(fr)):
        est = [(t, k) for t in range(len(takes)) for k in range(len(C[s][t]))]
        estados.append(est)
        if not est:
            print(f"frase {s+1} sem execucao aproveitavel", file=sys.stderr)

    # Frase sem candidato NAO pode matar a peca. Antes, uma unica frase que o
    # apresentador reformulou demais (o roteiro dizia uma coisa, a fala trouxe
    # outra enumeracao, com outras palavras) zerava os
    # estados daquele passo e o DP falhava em cascata: "nenhum grupo cobriu o
    # roteiro" e a peca inteira nao saia. Agora existe o estado PULAR, com
    # penalidade — perde-se uma frase, nao a peca.
    PULAR = ("pular", -1)
    CUSTO_PULO = 120.0
    for s in range(len(fr)):
        estados[s] = list(estados[s]) + [PULAR]

    INF = float("inf")
    dp = [{} for _ in fr]
    pai = [{} for _ in fr]
    for e in estados[0]:
        dp[0][e] = CUSTO_PULO if e == PULAR else C[0][e[0]][e[1]][0]
    for s in range(1, len(fr)):
        for e in estados[s]:
            t, k = e
            cand = None if e == PULAR else C[s][t][k]
            melhor, arg = INF, None
            for e0 in estados[s - 1]:
                if e0 not in dp[s - 1] or dp[s - 1][e0] == INF:
                    continue
                t0, k0 = e0
                if cand is None or e0 == PULAR:
                    c = dp[s - 1][e0] + (0 if e0 == PULAR or e == PULAR else a.custo_troca)
                    if c < melhor:
                        melhor, arg = c, e0
                    continue
                if t0 == t:
                    prev = C[s - 1][t0][k0]
                    # Avancar sim, mas SOBREPOR pode: com janelas de ate 1,9x a
                    # janela de uma frase engole a seguinte, e exigir "comecar
                    # depois do fim da anterior" tornava o DP insoluvel. A
                    # fusao de janelas mais adiante resolve a sobreposicao.
                    if cand[1] < prev[1] + 1 or cand[2] < prev[2]:
                        continue
                c = dp[s - 1][e0] + (0 if t0 == t else a.custo_troca)
                if c < melhor:
                    melhor, arg = c, e0
            if arg is not None:
                dp[s][e] = melhor + (CUSTO_PULO if cand is None else cand[0])
                pai[s][e] = arg
    if not dp[-1]:
        print("nao foi possivel cobrir o roteiro", file=sys.stderr)
        return 2
    fim = min(dp[-1], key=lambda e: dp[-1][e])
    escolha, cur = [None] * len(fr), fim
    for s in range(len(fr) - 1, -1, -1):
        escolha[s] = cur
        cur = pai[s].get(cur) if s > 0 else cur

    # A borda do clipe vem da JANELA da frase (trecho continuo), nao das
    # palavras que casaram. O Whisper ouviu uma sigla soletrada como letras
    # soltas ("W .A."): nao casa com o roteiro, ficava fora da borda e a
    # palavra saia decepada no video.
    janelas, casadas, puladas = [], {}, []
    for s, (i1, i2) in enumerate(fr):
        if escolha[s] == PULAR:
            puladas.append(s + 1)
            continue
        t, k = escolha[s]
        j0, j1 = C[s][t][k][1], C[s][t][k][2]
        pares = pares_da_janela(rot_n[i1:i2], i1, takes[t]["pal"], j0, j1)
        rot_ok = {ri for (ri, _) in pares}
        for (_, ji) in pares:
            casadas.setdefault(t, set()).add(ji)
        # A janela e escolhida por pontuacao e sobra nas pontas — foi assim que
        # um "Obrigado." que nao existe no roteiro entrou na peca. Entao apara
        # ate a palavra que casou. A excecao e a ponta cuja palavra do ROTEIRO
        # nao casou: ela foi dita, o ASR e que escreveu outra coisa (sigla
        # virando letras soltas), e aparar ali deceparia a palavra no video.
        pal_t = takes[t]["pal"]
        alvo = [ji for (_, ji) in pares]
        if alvo:
            j0, j1 = min(alvo), max(alvo) + 1
        if i1 not in rot_ok:
            lim = pal_t[j0]["s"] - 1.0
            n = 0
            while j0 > 0 and n < 2 and pal_t[j0 - 1]["s"] >= lim:
                j0 -= 1; n += 1
        if (i2 - 1) not in rot_ok:
            lim = pal_t[j1 - 1]["e"] + 1.0
            n = 0
            while j1 < len(pal_t) and n < 2 and pal_t[j1]["e"] <= lim:
                j1 += 1; n += 1
        # A janela pode parar no meio da fala porque o apresentador IMPROVISOU
        # o final da frase (o roteiro fechava de um jeito, a fala fechou de
        # outro). As palavras improvisadas nao casam, a janela para nelas e a
        # frase morre no ar. Estende ate a proxima pausa real — no maximo 8
        # palavras / 3,5s. SO estende quando a frase ficou truncada: se a
        # ultima palavra do roteiro casou, nao ha improviso a recuperar e
        # avancar invade a frase seguinte — foi assim que um fecho de CTA
        # ganhou um "e se" pendurado na peca entregue.
        if (i2 - 1) not in rot_ok and j1 < len(pal_t):
            base_fim = pal_t[j1 - 1]["e"]
            n = 0
            while (j1 < len(pal_t) and n < 8
                   and pal_t[j1]["e"] - base_fim < 3.5
                   and (pal_t[j1]["s"] - pal_t[j1 - 1]["e"]) < max(0.35, RESPIRO_FALANTE * 0.6)):
                j1 += 1; n += 1

        if janelas and janelas[-1]["take"] == t and j0 <= janelas[-1]["j1"]:
            janelas[-1]["j1"] = max(janelas[-1]["j1"], j1)
            janelas[-1]["frases"].append(s + 1)
        else:
            janelas.append({"take": t, "j0": j0, "j1": j1, "frases": [s + 1]})

    # dentro de cada janela, corta o que for gaguejo/falsa partida
    trechos = []
    for jan in janelas:
        t, pal = jan["take"], takes[jan["take"]]["pal"]
        ok = casadas.get(t, set())
        atual, k = jan["j0"], jan["j0"]
        while k < jan["j1"]:
            if k in ok:
                k += 1; continue
            fim = k
            while fim < jan["j1"] and fim not in ok:
                fim += 1
            # Usa a MESMA funcao de classificacao das emendas. Havia uma copia
            # do teste aqui, e ela nao recebeu nenhuma melhoria: a falsa partida
            # "Parece dificil... Parece simples" era detectada nas emendas e
            # ignorada dentro da janela, entao continuava na peca. Regra unica.
            if eh_disfluencia(pal, k - 1, fim):
                if k > atual:
                    trechos.append((t, atual, k, jan["frases"]))
                atual = fim
            k = fim
        if atual < jan["j1"]:
            trechos.append((t, atual, jan["j1"], jan["frases"]))

    # UMA FRASE, UM TRECHO — mas so quando os dois trechos DIZEM A MESMA
    # COISA. Agrupar por conjunto-de-frases errou nos dois sentidos: uniu
    # metades complementares de uma janela partida por gaguejo (e a metade com
    # o conteudo foi jogada fora) e nao uniu duas tentativas da mesma fala que
    # cairam em frases diferentes (e a peca repetiu).
    #
    # O criterio e a COBERTURA DO ROTEIRO: dois trechos que cobrem as mesmas
    # palavras do roteiro sao duas tentativas -> fica a mais longa; trechos que
    # cobrem palavras disjuntas sao partes complementares -> ficam as duas.
    def _cobertura_roteiro(tr):
        t_, j0_, j1_, frases_ = tr[0], tr[1], tr[2], tr[3]
        idx = set()
        for s_ in frases_:
            if not (1 <= s_ <= len(fr)):
                continue
            a_, b_ = fr[s_ - 1]
            for (ri, _) in pares_da_janela(rot_n[a_:b_], a_, takes[t_]["pal"], j0_, j1_):
                idx.add(ri)
        return idx

    cob = {id(tr): _cobertura_roteiro(tr) for tr in trechos}

    def _dur_tr(x):
        pal_g = takes[x[0]]["pal"]
        a_ = pal_g[min(x[1], len(pal_g) - 1)]["s"]
        b_ = pal_g[min(x[2] - 1, len(pal_g) - 1)]["e"]
        return b_ - a_

    descartar = set()
    for ia in range(len(trechos)):
        xa = trechos[ia]
        if id(xa) in descartar:
            continue
        for ib in range(ia + 1, len(trechos)):
            xb = trechos[ib]
            if id(xb) in descartar or xa[0] != xb[0]:
                continue
            ca, cb = cob[id(xa)], cob[id(xb)]
            menor = min(len(ca), len(cb))
            if menor < 3:
                continue
            # >=60% das palavras do trecho menor tambem estao no maior: e a
            # mesma passagem dita duas vezes.
            if len(ca & cb) / menor < 0.60:
                continue
            # Fica a execucao MAIS COMPLETA — quem cobre mais palavras do
            # roteiro. A duracao so desempata: ela infla com silencio e ja
            # elegeu trecho errado antes.
            if (len(ca), _dur_tr(xa)) >= (len(cb), _dur_tr(xb)):
                fica, perde = xa, xb
            else:
                fica, perde = xb, xa
            descartar.add(id(perde))
            print(f"    mesma passagem do roteiro em 2 execucoes "
                  f"({len(ca & cb)}/{menor} palavras em comum): "
                  f"mantida a de {_dur_tr(fica):.1f}s, descartada a de {_dur_tr(perde):.1f}s")
            if perde is xa:
                break
    if descartar:
        trechos = [x for x in trechos if id(x) not in descartar]

    clips = []
    for ti, (t, j0, j1, frases) in enumerate(trechos):
        eh_ultimo = (ti == len(trechos) - 1)
        tk = takes[t]
        j1 = min(j1, len(tk["pal"])) - 1
        if j1 < j0:
            continue
        # A regua aqui e o alinhamento FONETICO (analise realinhada). Com ela,
        # a borda e medicao, nao heuristica: sem clamp por token fantasma, sem
        # silencedetect, sem "invariante" remendando um fim que vinha errado.
        # Se a analise NAO foi realinhada, isto e erro de processo — aborta.
        if not tk.get("fa"):
            print(f"ERRO: {tk['nome']} sem alinhamento fonetico — rode "
                  f"alinhar_fonetico.py antes de montar", file=sys.stderr)
            sys.exit(3)
        ataque, termino = tk["pal"][j0]["s"], tk["pal"][j1]["e"]
        pal_ = tk["pal"]

        # (a) FIM DE FALA REAL: a ultima palavra do trecho tem que ser fim de
        # frase NO AUDIO, nao so no roteiro. Se quem fala emendou ("no
        # servidor, e se der erro, chame o suporte"), cortar em "servidor" corta
        # no meio da fala e o tom denuncia. Regra: se a pausa depois da ultima palavra
        # e menor que o respiro entre frases do falante, o trecho segue ate a
        # proxima palavra que E fim de fala (pausa >= respiro), no maximo 12
        # palavras / 6s adiante. Vale para todo trecho, e em especial o ultimo.
        j1_orig = j1
        k = j1
        # so vale para o ULTIMO trecho da peca (nos outros, o que vem depois e
        # regravacao/falsa partida cortada de proposito — estender atravessa o
        # corte). E o ponto de parada e FIM DE FRASE no texto do bruto
        # (pontuacao) ou pausa >= respiro — "e se" nao e fim de fala.
        if eh_ultimo:
            def _fantasma(w):
                # token que o ASR inventou: dura ~nada. Nao serve de parada nem
                # de referencia de gap — foi um "e..." de 0,03s que travou a
                # extensao no meio da ultima frase.
                return (w["e"] - w["s"]) < 0.05
            # A peca ja termina onde o ROTEIRO termina? Entao nao ha fim de
            # fala a procurar. Sem esta guarda, uma peca fechava em "inscreva-se
            # no curso, SE": o roteiro fecha em "Inscreva-se no curso.", mas no
            # bruto a fala emenda "curso, se depois de ver o primeiro modulo...",
            # e a virgula da transcricao nao conta como fim de frase — a extensao
            # saiu atras de um ponto final que so existia na frase seguinte.
            # A folga de sala vem do pad_fim_peca, nao de palavra a mais.
            _uf = max(frases) if frases else 0
            _fim_rot = rot_n[fr[_uf - 1][1] - 1] if 1 <= _uf <= len(fr) else None
            ja_fechou = (_fim_rot is not None and pal_[j1]["n"] == _fim_rot)
            while (not ja_fechou and k + 1 < len(pal_)
                   and (k - j1_orig) < 16 and (pal_[k]["e"] - termino) < 8.0):
                prox = next((x for x in range(k + 1, len(pal_))
                             if not _fantasma(pal_[x])), None)
                if prox is None:
                    break
                fim_txt = (re.search(r"[.!?…]\"?$", pal_[k]["w"]) is not None
                           and not _fantasma(pal_[k]))
                gap = pal_[prox]["s"] - pal_[k]["e"]
                if fim_txt and gap >= 0.25:
                    break
                if gap >= max(0.9, RESPIRO_FALANTE * 1.5):
                    break
                k = prox
            # se parou por limite sem achar fim de frase, nao estende (melhor
            # o corte original que um corte pior mais adiante)
            if k != j1:
                prox = next((x for x in range(k + 1, len(pal_))
                             if (pal_[x]["e"] - pal_[x]["s"]) >= 0.05), None)
                ok_fim = (re.search(r"[.!?…]\"?$", pal_[k]["w"]) is not None
                          or prox is None
                          or pal_[prox]["s"] - pal_[k]["e"] >= 0.9)
                if not ok_fim:
                    k = j1
        if k != j1:
            j1 = k
            termino = pal_[j1]["e"]
            frases = sorted(set(frases) | {max(frases)})
            c_extra = " ".join(pal_[x]["w"] for x in range(j1_orig + 1, j1 + 1))
            print(f"    fim de fala estendido apos \"{pal_[j1_orig]['w']}\": +\"{c_extra}\"")

        # (b) SPAN IMPLAUSIVEL na borda: o CTC as vezes da 0,10s a "melhor"
        # (impossivel) porque repartiu com a vizinha. Se a palavra da borda
        # mede menos que 0,06s por letra, nao confie: estenda ate onde a
        # energia cai (silencedetect local) antes de aplicar o pad.
        def _implausivel(w):
            # marcado pelo alinhador (span fisicamente impossivel) OU curto demais
            # para o tamanho, OU vizinho inflado (a duracao "roubada" foi para ele)
            if w.get("instrumento") == "fa-invalida":
                return True
            n = len(re.sub(r"[^a-z0-9]", "", w["n"])) or 1
            return n >= 3 and (w["e"] - w["s"]) < 0.06 * n
        if _implausivel(pal_[j1]):
            real = fim_da_fala(tk["video"], pal_[j1]["s"], limite=1.0)
            if real and real > termino:
                termino = real
        if _implausivel(pal_[j0]):
            # ataque implausivel: recua ate o comeco do silencio anterior
            ini_real = fim_da_fala(tk["video"], max(0.0, ataque - 1.0), limite=1.0)
            if ini_real and ini_real < ataque:
                ataque = max(ini_real, pal_[j0 - 1]["e"] if j0 > 0 else 0.0)

        ini = max(0.0, ataque - a.pad_antes)
        f = min(tk["dur"], termino + a.pad_depois)
        # nao reintroduz o que foi cortado: respeita a palavra vizinha, mas
        # NUNCA entra na palavra desta borda (o pad e folga, nao correcao)
        if j0 > 0:
            ini = max(ini, min(ataque, pal_[j0 - 1]["e"] + 0.02))
        if j1 + 1 < len(pal_):
            f = min(f, max(termino, pal_[j1 + 1]["s"] - 0.02))
        # ultima palavra da PECA: cauda de sala precisa de folga maior — mas a
        # folga e SILENCIO, nunca a palavra seguinte. Este pad era aplicado
        # depois do limite da palavra vizinha e o sobrescrevia: a peca fechava
        # na ultima palavra do roteiro e os 0,45s de cauda traziam junto o
        # "se depois" emendado no bruto — o fecho virava um "se" pendurado, e o
        # ultimo cartao de legenda em tela era a palavra "SE" sozinha. Se nao
        # ha silencio, nao ha cauda para pegar.
        if eh_ultimo:
            lim_fim = tk["dur"]
            if j1 + 1 < len(pal_):
                lim_fim = min(lim_fim, pal_[j1 + 1]["s"] - 0.03)
            f = min(lim_fim, max(f, termino + a.pad_fim_peca))
        if f - ini < 0.45:
            continue
        # SO funde se os trechos forem CONTIGUOS NA LISTA (nenhum corte entre
        # eles). Fundir por proximidade temporal desfazia o corte da gaguejada:
        # o trecho anterior terminava em 162,86 e o seguinte comecava em
        # 162,87 — 1 centesimo, dentro da tolerancia de 0,01 — e a palavra
        # gaguejada voltava inteira. Diferenca de arredondamento nao pode reverter
        # uma decisao de corte.
        contiguo = (clips and clips[-1].get("_j1") == j0)
        if clips and clips[-1]["take"] == t and contiguo and ini <= clips[-1]["out"] + 0.01:
            clips[-1]["out"] = max(clips[-1]["out"], f)
            clips[-1]["frases"] = sorted(set(clips[-1]["frases"]) | set(frases))
            clips[-1]["_j1"] = j1
        else:
            clips.append({"take": t, "src": tk["video"], "analise": tk["pasta"],
                          "in": round(ini, 2), "out": round(f, 2),
                          "frases": sorted(set(frases)), "_j0": j0, "_j1": j1})

    # Corte de silencio DENTRO do clipe, decidido aqui com a regua FA. Antes o
    # render_edl refazia isso com o Whisper e SOBRESCREVIA a borda medida.
    #
    # LICAO (entrega reprovada na revisao humana): pausa entre frases E
    # RESPIRO, nao silencio a cortar. Com pausa_min fixo de 0,70s o montador
    # cortou as pausas de 0,9-1,1s que o apresentador faz depois de ponto
    # final, e a peca virou "uma frase so". Regra agora: pausa apos ponto final NUNCA e removida —
    # so ENCURTADA para o respiro mediano do falante quando passa do p90 dele.
    # Pausa no meio de frase e tratada igual (encurtar, nao zerar).
    import statistics as _st
    pf = []
    for tk_ in takes:
        pw = tk_["pal"]
        for k in range(1, len(pw)):
            g = pw[k]["s"] - pw[k - 1]["e"]
            if 0 < g < 3.0 and re.search(r"[.!?…]$", pw[k - 1]["w"]):
                pf.append(g)
    pf.sort()
    # respiro = p75 das pausas do falante entre frases (nao a mediana): quando
    # uma pausa longa e encurtada ela cai para "uma pausa dele um pouco generosa",
    # nao para a mais curta. E so pausas acima do p90 (min 1,2s) sao tocadas.
    # ALVO do respiro na emenda = MEDIANA das pausas do falante (ritmo tipico). O p75
    # como alvo pedia 0,64s onde ja havia 0,86s de silencio natural e mandava
    # inserir mais — o portao entao reprovava por excesso. O p75 fica so como
    # TETO tolerado (no portao), nao como meta a atingir.
    import statistics as _stM
    respiro = a.respiro if a.respiro is not None else (_stM.median(pf) if len(pf) >= 5 else 0.50)
    pausa_min = a.pausa_min if a.pausa_min is not None else max(1.2, (pf[int(0.9 * (len(pf) - 1))] if len(pf) >= 5 else 1.2))
    # RESPIRO NAS EMENDAS ENTRE CLIPES: quando o corte de regravacao remove
    # junto o silencio natural, a frase seguinte cola na anterior (0,08s entre
    # um ponto final e a frase seguinte). Aqui cada emenda entre frases recebe o respiro
    # do falante: o clipe seguinte comeca `respiro` antes da primeira palavra
    # (pegando silencio real do bruto), e o anterior termina pad_depois apos a
    # ultima. Emenda no meio de frase (gaguejo cortado) nao recebe respiro.
    for k in range(1, len(clips)):
        ant, cur = clips[k - 1], clips[k]
        pal_a = takes[ant["take"]]["pal"]; pal_c = takes[cur["take"]]["pal"]
        ult = max((w for w in pal_a if w["e"] <= ant["out"] + 0.02), key=lambda w: w["e"], default=None)
        pri = min((w for w in pal_c if w["s"] >= cur["in"] - 0.02), key=lambda w: w["s"], default=None)
        if not ult or not pri:
            continue
        entre_frases = re.search(r"[.!?…]$", ult["w"]) is not None
        if not entre_frases:
            continue
        # silencio disponivel antes da primeira palavra do clipe seguinte, no bruto
        idx = pal_c.index(pri)
        lim_ant = pal_c[idx - 1]["e"] + 0.02 if idx > 0 else 0.0
        querido = pri["s"] - respiro
        novo_in = max(lim_ant, min(cur["in"], querido))
        # e depois da ultima do anterior
        idx_a = pal_a.index(ult)
        lim_prox = pal_a[idx_a + 1]["s"] - 0.02 if idx_a + 1 < len(pal_a) else takes[ant["take"]]["dur"]
        # NUNCA recua o out ja decidido (ele pode ter vindo de medicao por
        # energia quando a palavra da borda era fa-invalida); so avanca se o
        # respiro pedir mais cauda e houver silencio para isso
        novo_out = min(lim_prox, max(ant["out"], ult["e"] + a.pad_depois))
        novo_out = max(novo_out, ant["out"])
        cur["in"], ant["out"] = round(novo_in, 3), round(novo_out, 3)
        # Se mesmo assim a emenda ficou colada, o bruto NAO tinha silencio para
        # emprestar (a pausa daquele ponto foi para a regravacao cortada).
        # Marca para o render inserir silencio: sem isto, duas frases coladas
        # soam como uma so — reprovado na revisao humana ("parece uma frase so").
        # Silencio que a emenda JA tera: cauda do clipe anterior apos a ultima
        # palavra + cabeca do seguinte antes da primeira. So completa o que
        # falta para chegar ao respiro do falante — somar o respiro inteiro
        # por cima gerava 0,92s onde o falante pausa 0,64s (excesso e engasgo igual).
        # A folga tem que ser medida no AUDIO, nao em timestamp: o span da
        # palavra termina antes da cauda acustica, e somar respiro sobre uma
        # folga subestimada gera excesso (0,92s onde o falante pausa 0,64s).
        # A folga da emenda e a soma de dois pedacos de silencio que vao ficar
        # COLADOS na peca: o que sobra depois da ultima palavra do clipe
        # anterior, e o que vem antes da primeira palavra do seguinte. Cada um
        # e limitado pelo proprio corte — medir a "pausa do bruto" inteira
        # superestima, porque boa parte dela foi cortada fora.
        cauda = max(0.0, ant["out"] - ult["e"])
        cabeca = max(0.0, pri["s"] - cur["in"])
        disponivel = cauda + cabeca
        falta = respiro - disponivel
        if falta > 0.15:
            cur["silencio_antes"] = round(min(falta, respiro), 3)
        elif cur.get("silencio_antes"):
            del cur["silencio_antes"]
    for c in clips:
        tk = takes[c["take"]]; pal = tk["pal"]
        segs, s_ini = [], c["in"]
        for k in range(c["_j0"], c["_j1"]):
            # TOKEN FANTASMA NAO PARTE O VAO. O alinhador colapsa uma alucinacao
            # do ASR num span de ~0,02s; ela fica no meio do silencio e divide
            # um vao real em dois menores, cada um abaixo do limiar. Foi assim
            # que 3,75s de fala nenhuma (a mao do apresentador mexendo no celular)
            # sobreviveram ao corte: um ' O' de 0,02s com p=0,06 no meio
            # transformou o vao em 1,04s + 2,69s. O fim do vao e a proxima
            # palavra DE VERDADE.
            prox = next((x for x in range(k + 1, c["_j1"] + 1)
                         if x < len(pal) and (pal[x]["e"] - pal[x]["s"]) >= 0.05), None)
            if prox is None:
                continue
            gap = pal[prox]["s"] - pal[k]["e"]
            if (pal[k]["e"] - pal[k]["s"]) < 0.05:
                continue                       # fantasma nao abre segmento
            if gap > pausa_min:
                # encurta: fecha o segmento pad_depois apos a palavra e reabre
                # de modo que a pausa resultante seja o respiro do falante
                fim_seg = pal[k]["e"] + a.pad_depois
                ini_seg = max(fim_seg, pal[prox]["s"] - max(a.pad_antes, respiro - a.pad_depois))
                segs.append([round(s_ini, 3), round(fim_seg, 3)])
                s_ini = ini_seg
        segs.append([round(s_ini, 3), round(c["out"], 3)])
        # DEDUP DE SEGMENTOS: dois segmentos do mesmo clipe que dizem a mesma
        # coisa sao duas tentativas da mesma frase (quem fala refez). Compara
        # o texto normalizado de cada segmento; se um e quase-prefixo do outro
        # (>=70% das palavras do menor em comum), mantem so o maior.
        if len(segs) > 1:
            def _txt(a_, b_):
                return [w["n"] for w in pal if w["s"] >= a_ - 0.01 and w["e"] <= b_ + 0.01]
            manter = []
            for sg in segs:
                tx = _txt(*sg)
                dup_de = None
                for idx, (sg2, tx2) in enumerate(manter):
                    if not tx or not tx2:
                        continue
                    menor, maior = (tx, tx2) if len(tx) <= len(tx2) else (tx2, tx)
                    comuns = sum(1 for w in menor if w in maior)
                    if len(menor) >= 3 and comuns / len(menor) >= 0.70:
                        dup_de = idx
                        break
                if dup_de is None:
                    manter.append((sg, tx))
                else:
                    sg2, tx2 = manter[dup_de]
                    if (sg[1] - sg[0]) > (sg2[1] - sg2[0]):
                        manter[dup_de] = (sg, tx)     # fica a execucao maior
                    print(f"    segmento duplicado descartado em {min(sg[0], sg2[0]):.1f}s "
                          f"(mesma frase dita 2x)")
            segs = [sg for sg, _ in manter]
        c["cutmap"] = segs
        c["dur_util"] = round(sum(b - a_ for a_, b in segs), 2)
        c["pausas_encurtadas"] = len(segs) - 1
        del c["_j0"], c["_j1"]
    # ---------------------------------------------------------------- JUNCOES
    # Percorre a peca inteira: cada segmento do cutmap de cada clipe, na ordem
    # em que o espectador ouve. A repeticao mora na juncao, e a juncao pode
    # estar dentro de um clipe OU entre dois clipes — a versao anterior so
    # olhava dentro, e uma chamada final duplicada passou por fora.
    plano = []
    for ci, c in enumerate(clips):
        for si, sg in enumerate(c["cutmap"]):
            plano.append((ci, si, sg))

    def _pals_de(ci, sg):
        pl = takes[clips[ci]["take"]]["pal"]
        return [w for w in pl if w["s"] >= sg[0] - 0.01 and w["e"] <= sg[1] + 0.01]

    _PARES_ROT = {(rot_n[i], rot_n[i + 1]) for i in range(len(rot_n) - 1)}

    def _parece_roteiro(seq):
        """Aderencia ao roteiro em SALDO de bigramas: quantos existem no roteiro
        menos meio ponto por cada um que nao existe.

        A versao anterior media FRACAO, e fracao nao enxerga conteudo perdido.
        Num caso real as duas opcoes davam 0,75 x 0,73 — quase empate — mas uma
        delas jogava fora uma oracao inteira do roteiro e deixava no lugar a
        chamada abandonada no meio. Saldo premia quem entrega mais roteiro
        correto; fracao premiava quem entrega menos texto."""
        if len(seq) < 2:
            return 0.0
        h = sum(1 for i in range(len(seq) - 1) if (seq[i], seq[i + 1]) in _PARES_ROT)
        return h - 0.5 * ((len(seq) - 1) - h)

    # FALSA PARTIDA DENTRO DO SEGMENTO. A limpeza de disfluencia so examina
    # trechos que NAO casaram com o roteiro — entao uma falsa partida cujas
    # palavras casam passa livre. Medido: o segmento continha
    #   "Mas... | Mas levaria | (embolo fora do roteiro) | mas levaria 633
    #    horas. | horas para revisar"
    # e as duas primeiras execucoes casaram com o "Mas levaria" do roteiro,
    # entao ficaram. Aqui a busca e por REPETICAO, nao por casamento: um trecho
    # de >=2 palavras que reaparece logo adiante e falsa partida, e o que fica
    # e a ULTIMA execucao — a que ele completou.
    for ci_, c_ in enumerate(clips):
        pl_ = takes[c_["take"]]["pal"]
        novos_segs = []
        for (sa_, sb_) in c_["cutmap"]:
            ws = [w for w in pl_ if w["s"] >= sa_ - 0.01 and w["e"] <= sb_ + 0.01
                  and (w["e"] - w["s"]) >= 0.05]
            nn = [w["n"] for w in ws]
            corte = None
            for k_ in range(min(6, len(nn) // 2), 1, -1):
                for i0 in range(0, len(nn) - 2 * k_ + 1):
                    alvo_ = nn[i0:i0 + k_]
                    n_rot = sum(1 for z in range(len(rot_n) - k_ + 1)
                                if rot_n[z:z + k_] == alvo_)
                    if n_rot >= 2:
                        continue              # anafora do roteiro
                    # a repeticao tem que ser PROXIMA: recomeco e imediato
                    for j0 in range(i0 + k_, min(len(nn) - k_ + 1, i0 + k_ + 14)):
                        if nn[j0:j0 + k_] != alvo_:
                            continue
                        if ws[j0]["s"] - ws[i0]["e"] > 4.0:
                            continue
                        # FALSA PARTIDA x PARALELISMO. Repetir nao basta: quem
                        # fala improvisa paralelismo o tempo todo, e cortar
                        # isso mutila a frase. O roteiro dizia "os relatorios
                        # de todas as areas E FILIAIS"; a fala foi "de todas
                        # as areas DE TODAS AS filiais" — melhor que o
                        # roteiro, e a regra queria cortar, deixando "os
                        # relatorios de todas as filiais" (sem areas).
                        # O QUE DECIDE E O QUE ESTA NO MEIO, nao a pausa.
                        # Testar a pausa primeiro reprovou anafora legitima
                        # ("um processo QUE REDUZ custo, QUE REDUZ retrabalho"
                        # tem 0,42s de respiro na virgula e seria cortada,
                        # destruindo a passagem). Ja o embolo real tinha texto
                        # fora do roteiro entre as duas execucoes.
                        # Regra: e falsa partida quando o miolo NAO e roteiro.
                        meio = nn[i0 + k_:j0]
                        _rot_set = set(rot_n)
                        no_rot = [z for z in meio if z in _rot_set]
                        # figura de tres ou mais execucoes nunca e falsa partida
                        n_ocorr = sum(1 for z in range(len(nn) - k_ + 1)
                                      if nn[z:z + k_] == alvo_)
                        if n_ocorr >= 3:
                            continue
                        if meio and len(no_rot) >= 0.5 * len(meio):
                            continue          # miolo e conteudo do roteiro
                        corte = (i0, j0, k_)
                        break
                    if corte:
                        break
                if corte:
                    break
            if not corte:
                novos_segs.append([sa_, sb_])
                continue
            i0, j0, k_ = corte
            print(f"    falsa partida no segmento em {ws[i0]['s']:.1f}s: "
                  f"\"{' '.join(nn[i0:i0 + k_])}\" dito 2x — fica a 2a execucao")
            esq_fim = round(max(sa_, ws[i0]["s"] - a.pad_antes), 3)
            dir_ini = round(max(esq_fim, ws[j0]["s"] - a.pad_antes), 3)
            # O QUE SOBRA A ESQUERDA DE UMA FALSA PARTIDA COSTUMA SER LIXO.
            # Num caso medido sobrou um segmento de 1,67s contendo so o "Mas..."
            # abandonado antes do recomeco. Menos de 3 palavras ali e resto de
            # tentativa, nao conteudo: cai fora junto.
            n_esq = sum(1 for w in ws[:i0] if w["e"] <= esq_fim + 0.01)
            if esq_fim - sa_ > 0.20 and n_esq >= 3:
                novos_segs.append([sa_, esq_fim])
            elif esq_fim - sa_ > 0.20:
                print(f"      (resto de {n_esq} palavra(s) antes da falsa "
                      f"partida descartado)")
            if sb_ - dir_ini > 0.20:
                novos_segs.append([dir_ini, sb_])
        if novos_segs:
            c_["cutmap"] = novos_segs
    plano = []
    for ci, c in enumerate(clips):
        for si, sg in enumerate(c["cutmap"]):
            plano.append((ci, si, sg))

    i_ = 0
    while i_ < len(plano) - 1:
        (cia, sia, sga), (cib, sib, sgb) = plano[i_], plano[i_ + 1]
        esq, dir_ = _pals_de(cia, sga), _pals_de(cib, sgb)
        ne = [w["n"] for w in esq]
        nd = [w["n"] for w in dir_]
        achou = None
        for k_ in range(min(8, len(nd)), 2, -1):
            alvo_ = nd[:k_]
            n_rot = sum(1 for z in range(len(rot_n) - k_ + 1)
                        if rot_n[z:z + k_] == alvo_)
            if n_rot >= 2:
                continue                     # anafora do roteiro, nao gaguejo
            ini_busca = max(0, len(ne) - 14)
            for q_ in range(len(ne) - k_, ini_busca - 1, -1):
                if ne[q_:q_ + k_] == alvo_:
                    achou = (q_, k_)
                    break
            if achou:
                break
        if not achou or achou[0] <= 0:
            i_ += 1
            continue
        q_, k_ = achou
        # Ponto de emenda da OPCAO B: nao basta pular as k_ palavras iguais.
        # Num caso real a segunda tentativa era "Esse ano, vendo DE QUE... o
        # resultado do trimestre": pular so "esse ano vendo" partia a palavra
        # seguinte no meio e deixava o tropeco "de que..." na peca. Alinha a
        # cauda da esquerda com a cabeca da direita e retoma DEPOIS da ultima
        # palavra que as duas tem em comum — ali as duas execucoes se
        # encontram, e e onde a emenda nao se ouve.
        cauda, cabeca = ne[q_:], nd[:20]
        blocos_ = [b for b in difflib.SequenceMatcher(None, cauda, cabeca,
                                                      autojunk=False).get_matching_blocks()
                   if b.size]
        k_b = max((b.b + b.size for b in blocos_), default=k_)
        k_b = max(k_, min(k_b, len(nd) - 1))
        # OPCAO A: apara a cauda da esquerda (fica o recomeco inteiro)
        seq_a = ne[:q_] + nd
        # OPCAO B: apara a cabeca da direita (fica a primeira tentativa)
        seq_b = ne + nd[k_b:]
        pa, pb = _parece_roteiro(seq_a), _parece_roteiro(seq_b)
        if pb > pa:
            novo_ini = round(dir_[k_b]["s"] - a.pad_antes, 3)
            if novo_ini < sgb[1] - 0.20:
                print(f"    juncao repetida em {dir_[0]['s']:.1f}s: "
                      f"\"{' '.join(nd[:k_])}\" — cabeca da direita aparada "
                      f"(aderencia ao roteiro {pb:.2f} x {pa:.2f})")
                clips[cib]["cutmap"][sib][0] = novo_ini
                plano[i_ + 1] = (cib, sib, clips[cib]["cutmap"][sib])
                continue
        novo_fim = round(esq[q_ - 1]["e"] + a.pad_depois, 3)
        if novo_fim > sga[0] + 0.20:
            print(f"    juncao repetida em {esq[q_]['s']:.1f}s: "
                  f"\"{' '.join(ne[q_:q_ + k_])}\" — cauda da esquerda aparada "
                  f"(aderencia ao roteiro {pa:.2f} x {pb:.2f})")
            clips[cia]["cutmap"][sia][1] = novo_fim
            plano[i_] = (cia, sia, clips[cia]["cutmap"][sia])
            continue
        i_ += 1

    # ------------------- TRECHOS PROIBIDOS -----------------------------------
    # Erro de FATO dito na gravacao. Improviso nao e defeito, mas numero
    # errado nao e improviso: o apresentador disse "desde 1919" querendo dizer
    # 2019. Com uma tomada so, nao ha alternativa correta a escolher: o trecho
    # e excisado e a frase fecha antes dele. A lista vive num JSON fora do
    # codigo, porque e decisao de conteudo (de quem aprova) e nao regra de
    # edicao.
    _cam_cfg = os.environ.get("VIDEO_IA_CRITICAS") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "palavras_criticas_exemplo.json")
    try:
        _cfg = json.load(open(_cam_cfg, encoding="utf-8-sig"))
    except (OSError, ValueError):
        _cfg = {}
    _base = os.path.splitext(os.path.basename(a.roteiro))[0].replace("_CURTO", "")
    _proib = [x for x in (_cfg.get("proibidas", {}).get(_base) or []) if isinstance(x, dict)]
    if _proib:
        for c_ in clips:
            pl_ = takes[c_["take"]]["pal"]
            saida_segs = []
            for (sa_, sb_) in c_["cutmap"]:
                pedacos = [[sa_, sb_]]
                for x in _proib:
                    seq = [norm(z) for z in x["trecho"].split() if norm(z)]
                    n_ = len(seq)
                    novos = []
                    for (pa_, pb_) in pedacos:
                        ws = [w for w in pl_ if w["s"] >= pa_ - 0.02 and w["e"] <= pb_ + 0.02]
                        nn = [w["n"] for w in ws]
                        corte = next((i for i in range(len(nn) - n_ + 1)
                                      if nn[i:i + n_] == seq), None)
                        if corte is None:
                            novos.append([pa_, pb_])
                            continue
                        ini_p = max(pa_, ws[corte]["s"] - 0.05)
                        fim_p = min(pb_, ws[corte + n_ - 1]["e"] + 0.05)
                        print(f"    trecho PROIBIDO excisado em {ini_p:.1f}s: "
                              f"\"{x['trecho']}\" — {x.get('motivo', '')}")
                        if ini_p - pa_ > 0.20:
                            novos.append([pa_, round(ini_p, 3)])
                        if pb_ - fim_p > 0.20:
                            novos.append([round(fim_p, 3), pb_])
                    pedacos = novos
                saida_segs.extend(pedacos)
            c_["cutmap"] = saida_segs

    # ------------- NORMALIZACAO FINAL DOS SEGMENTOS (so tira, nunca poe) -----
    # Achado pela auditoria VISUAL: um segmento de 1,10s carregava a palavra
    # "se" sozinha, com 0,80s de silencio na cabeca — e e durante esse
    # silencio que o apresentador aparece debrucado, fora do enquadramento.
    # Salto visual de 89,1, o pior do lote. O portao de audio aprovou: as
    # palavras estavam integras e a emenda, limpa. Nao havia o que ouvir.
    #
    # Duas guardas, ambas seguras porque so removem silencio ou fragmento:
    #   (a) segmento sem fala util nao existe;
    #   (b) silencio na cabeca/cauda de um segmento e limitado ao pad — o vao
    #       entre frases quem faz e o respiro do falante, nao sobra de bruto.
    ult_ci = len(clips) - 1
    for ci_, c_ in enumerate(clips):
        pl_ = takes[c_["take"]]["pal"]
        limpos = []
        for si_, (sa_, sb_) in enumerate(c_["cutmap"]):
            dentro = [w for w in pl_
                      if w["s"] >= sa_ - 0.02 and w["e"] <= sb_ + 0.02
                      and (w["e"] - w["s"]) >= 0.05]
            fala = sum(w["e"] - w["s"] for w in dentro)
            if not dentro or (fala < 0.25 and len(dentro) <= 2):
                print(f"    segmento sem fala util descartado: {sa_:.2f}-{sb_:.2f} "
                      f"({sb_ - sa_:.2f}s, "
                      + (f"so \"{' '.join(w['w'].strip() for w in dentro)}\"" if dentro
                         else "nenhuma palavra") + ")")
                continue
            eh_fim_peca = (ci_ == ult_ci and si_ == len(c_["cutmap"]) - 1)
            novo_a = max(sa_, round(dentro[0]["s"] - a.pad_antes, 3))
            folga = a.pad_fim_peca if eh_fim_peca else a.pad_depois
            novo_b = min(sb_, round(dentro[-1]["e"] + folga, 3))
            if novo_a - sa_ > 0.15 or sb_ - novo_b > 0.15:
                print(f"    silencio aparado no segmento {sa_:.2f}-{sb_:.2f} -> "
                      f"{novo_a:.2f}-{novo_b:.2f} "
                      f"(cabeca -{novo_a - sa_:.2f}s, cauda -{sb_ - novo_b:.2f}s)")
            if novo_b - novo_a > 0.15:
                limpos.append([novo_a, novo_b])
        c_["cutmap"] = limpos

    for c in clips:
        c["cutmap"] = [sg for sg in c["cutmap"] if sg[1] - sg[0] > 0.05]
        c["in"] = min([sg[0] for sg in c["cutmap"]], default=c["in"])
        c["out"] = max([sg[1] for sg in c["cutmap"]], default=c["out"])
        c["dur_util"] = round(sum(b - a_ for a_, b in c["cutmap"]), 2)
        c["pausas_encurtadas"] = len(c["cutmap"]) - 1
    clips[:] = [c for c in clips if c["cutmap"]]

    print(f"  ritmo do falante: respiro entre frases {respiro:.2f}s, encurta pausas > {pausa_min:.2f}s")
    for k, c in enumerate(clips):
        c["dur"] = round(c["out"] - c["in"], 2)
        c["nome_take"] = takes[c["take"]]["nome"]
        c["salto_antes"] = (round(c["in"] - clips[k - 1]["out"], 2)
                            if k and clips[k - 1]["take"] == c["take"] else None)

    util = sum(c["dur"] for c in clips)
    trocas = sum(1 for x, y in zip(clips, clips[1:]) if x["take"] != y["take"])
    cob = sum(C[s][escolha[s][0]][escolha[s][1]][3] for s in range(len(fr))
              if escolha[s] != PULAR) / max(1, len(fr) - len(puladas))
    usados = sorted({c["nome_take"] for c in clips})

    json.dump({"roteiro": os.path.basename(a.roteiro),
               "takes": [t["nome"] for t in takes], "takes_usados": usados,
               "frases": len(fr), "frases_faltando": puladas,
               "cobertura_media": round(cob, 3), "dur_util": round(util, 2),
               "trocas_de_take": trocas, "clips": clips},
              open(a.saida, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"{os.path.basename(a.roteiro)}  best-of de {len(takes)} take(s)")
    print(f"  {len(fr)} frases | cobertura media {100*cob:.1f}% | util {util:.1f}s")
    print(f"  {len(clips)} clip(s), {trocas} troca(s); usados: {', '.join(usados)}")
    for c in clips:
        print(f"    {c['nome_take']:<26} {c['in']:7.2f}-{c['out']:7.2f} "
              f"({c['dur']:5.1f}s) frases {c['frases'][0]}-{c['frases'][-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
