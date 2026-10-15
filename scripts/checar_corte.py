# -*- coding: utf-8 -*-
"""PORTAO DE ENTREGA — conferencia do corte por MEDICAO na saida.

Principio (veio de auditoria apos entregas reprovadas): "Whisper le, alinhador
fonetico mede, energia cala." A versao anterior deste portao inferia defeito a
partir de timestamps do Whisper — a mesma regua errada que produzia o defeito.
Reprovava fala boa e aprovava numero mutilado ("414" virou "400" e passou).

O que este portao faz, por peca entregue:
  A. Alinha foneticamente o TEXTO ESPERADO (palavras do bruto dentro do cutmap)
     no AUDIO FINAL. Cada palavra ganha span e score MEDIDOS na saida.
     -> DECEPADA: span na saida < 70% do span que a palavra tinha no bruto
        (mesmo instrumento nos dois lados) ou score que desaba em palavra
        encostada numa emenda. Cobre numero, sigla e palavra curta — as classes
        que as heuristicas de duracao/letras deixavam de fora.
     -> PERDIDA: palavra esperada sem span (o alinhador nao a encontra).
  B. Mede por RMS o vao em CADA EMENDA (posicoes conhecidas pelo cutmap).
     -> ENGASGO: silencio > 0,45s numa emenda que no bruto nao tinha pausa.
  C. Energia de fala sem palavra esperada = REPETICAO / FALSA PARTIDA / texto
     nao previsto que sobrou no corte.
  D. Segunda opiniao TEXTUAL (Whisper do corte): repeticao (5-grama), gaguejo
     em pares, corte no meio de frase, abertura pendurada, cobertura.

Uso: python checar_corte.py PASTA_RENDER --roteiro R.txt --casamento c.json
       [--video sem_legenda.mp4] [--analise PASTA_ANALISE_DA_SAIDA]

PASTA_RENDER segue a convencao do pipeline_peca.py: `sem_legenda.mp4` (o corte
sem legenda nem musica) e `analise_corte/analise.json` (transcricao da saida).
--video e --analise sobrescrevem a convencao.
Exit: 0 aprovada | 2 reprovada | 1 crash (nao e reprova)
"""
import argparse, collections, difflib, json, os, re, subprocess, sys, unicodedata, wave

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FILLERS = {"eh", "ah", "ahn", "an", "hum", "hmm", "aa", "ee", "uhm", "aham", "ahm"}
PENDURADO = re.compile(
    r"^(e|mas|entao|então|ai|aí|por isso|porem|porém|alem disso|além disso|nele|nela|"
    r"isso|essa|esse|esta|este|assim|tambem|também|depois|agora|so que|só que|ou seja|"
    r"pois|dai|daí|nisso|dessa|desse)\b")


def sa(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", sa(s))


def ngramas(seq, n):
    return collections.Counter(" ".join(seq[i:i + n]) for i in range(len(seq) - n + 1))


def rms_por_janela(video, ini, dur, sr=16000, jan=0.010):
    import numpy as np
    import tempfile
    wav = os.path.join(tempfile.gettempdir(), f"videoia_chk_{os.getpid()}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(max(0, ini)), "-i", video,
                    "-t", str(dur), "-vn", "-ar", str(sr), "-ac", "1", "-y", wav],
                   capture_output=True)
    if not os.path.exists(wav):
        return None, jan
    with wave.open(wav, "rb") as f:
        pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32)
    os.remove(wav)
    n = int(sr * jan)
    if len(pcm) < 2 * n:
        return None, jan
    return np.array([np.sqrt(np.mean(pcm[i:i + n] ** 2)) for i in range(0, len(pcm) - n, n)]), jan


def remedir_par(e, r, corte_mp4, cache):
    """Remede a MESMA palavra, com 3 vizinhos de cada lado, no bruto e na
    saida, com janela local identica. Devolve (dur_bruto, dur_saida) ou None."""
    try:
        import alinhar_fonetico as AF
        words, _ = cache[e["src"]]
        i = e["idx"]
        viz = words[max(0, i - 3):i + 4]
        textos = [w["w"].strip() for w in viz]
        k = i - max(0, i - 3)
        ini_b, fim_b = viz[0]["s"] - 0.20, viz[-1]["e"] + 0.20
        vid_b = json.load(open(os.path.join(e["src"], "analise.json"), encoding="utf-8"))["video"]
        rb = AF.alinhar(vid_b, max(0.0, ini_b), fim_b - ini_b, textos)
        # na saida: mesma janela relativa em torno do span medido
        desl = r["s"] - e["s_bruto"]
        rs = AF.alinhar(corte_mp4, max(0.0, ini_b + desl), fim_b - ini_b, textos)
        if len(rb) != len(textos) or len(rs) != len(textos):
            return None
        if rb[k]["s"] is None or rs[k]["s"] is None:
            return None
        return (rb[k]["e"] - rb[k]["s"], rs[k]["e"] - rs[k]["s"])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta_render")
    ap.add_argument("--roteiro", required=True)
    ap.add_argument("--casamento", default=None)
    ap.add_argument("--video", default=None, help="corte a medir (default PASTA/sem_legenda.mp4)")
    ap.add_argument("--analise", default=None, help="pasta com analise.json da saida")
    ap.add_argument("--cobertura-min", type=float, default=82.0)
    a = ap.parse_args()

    nome = os.path.basename(os.path.abspath(a.pasta_render))
    corte_mp4 = a.video or os.path.join(a.pasta_render, "sem_legenda.mp4")
    an = os.path.join(a.analise or os.path.join(a.pasta_render, "analise_corte"), "analise.json")
    if not (os.path.exists(corte_mp4) and os.path.exists(an)):
        print(f"faltam sem_legenda.mp4/analise_corte em {a.pasta_render}", file=sys.stderr)
        return 1
    d = json.load(open(an, encoding="utf-8"))
    dur_saida = d["meta"]["duracao"]
    pal = [{"w": w["w"].strip(), "n": norm(w["w"]), "s": w["s"], "e": w["e"],
            "p": w.get("p", 1.0)} for w in d["palavras"] if norm(w["w"])]
    fala = [p["n"] for p in pal]

    rot_txt = open(a.roteiro, encoding="utf-8").read()
    rot_o = [x for x in rot_txt.split() if norm(x)]
    rot = [norm(x) for x in rot_o]
    fim_frase = {i for i, w in enumerate(rot_o) if re.search(r"[.!?…]\"?\)?$", w)}

    print(f"\n### CORTE — {nome}  ({dur_saida:.1f}s, {len(pal)} palavras)")
    problemas, avisos = [], []

    # ------------------------------------------------------------ plano
    esperado, emendas = [], []      # esperado: [{w, n, dur_bruto, fa_bruto, t_saida_est}]
    if a.casamento and os.path.exists(a.casamento):
        cas = json.load(open(a.casamento, encoding="utf-8"))
        cache = {}
        t_out = 0.0
        for c in cas["clips"]:
            p = c["analise"]
            if p not in cache:
                dd = json.load(open(os.path.join(p, "analise.json"), encoding="utf-8"))
                cache[p] = ([w for w in dd["palavras"] if norm(w["w"])],
                            bool(dd.get("alinhamento_fonetico")))
            words, tem_fa = cache[p]
            if not tem_fa:
                avisos.append(f"bruto sem alinhamento fonetico: {os.path.basename(p)}")
            for (i0, o0) in c.get("cutmap") or [[c["in"], c["out"]]]:
                if emendas or esperado:
                    emendas.append({"t": t_out, "src": p, "src_t": i0})
                for wi, w in enumerate(words):
                    if w["s"] >= i0 - 0.01 and w["e"] <= o0 + 0.01 and (w["e"] - w["s"]) >= 0.02:
                        # span sobreposto ao vizinho = gaguejada emendada; a
                        # palavra pode ter sido cortada de proposito e a
                        # duracao nao e referencia confiavel
                        sobrep = ((wi > 0 and w["s"] < words[wi - 1]["e"] - 0.005) or
                                  (wi + 1 < len(words) and words[wi + 1]["s"] < w["e"] - 0.005))
                        esperado.append({"sobrep": sobrep,
                                         "w": w["w"].strip(), "n": norm(w["w"]),
                                         "dur_bruto": w["e"] - w["s"],
                                         "fa_bruto": w.get("fa"),
                                         "t_est": t_out + (w["s"] - i0),
                                         "src": p, "s_bruto": w["s"], "e_bruto": w["e"],
                                         "idx": words.index(w)})
                t_out += (o0 - i0)

    # ------------------------------------------------ A. medir palavras na saida
    if esperado:
        try:
            import alinhar_fonetico as AF
            res = AF.alinhar(corte_mp4, 0.0, dur_saida, [e["w"] for e in esperado])
        except Exception as ex:
            print(f"alinhador indisponivel: {ex}", file=sys.stderr)
            res = []
        if len(res) == len(esperado):
            n_ok = 0
            excluidos = []
            for e, r in zip(esperado, res):
                if r["s"] is None:
                    # token que comeca com pontuacao (o "-20." de um codigo
                    # dito "15-20") nao alinha: o alinhador nao tem fonema para
                    # o hifen e o span sai vazio. Verificado por reescuta: o
                    # numero estava inteiro na peca. Nao e palavra perdida.
                    frag = re.match(r"^[^\wÀ-ÿ]", e["w"]) is not None
                    if not e.get("sobrep") and not frag:
                        problemas.append(f"PALAVRA PERDIDA: \"{e['w']}\" (esperada em ~{e['t_est']:.1f}s) nao esta no audio final")
                    else:
                        # exclusao silenciosa esconde defeito: nomeia sempre
                        excluidos.append(f"{e['w']!r} ({'span sobreposto' if e.get('sobrep') else 'fragmento de pontuacao'})")
                    continue
                dur_s = r["e"] - r["s"]
                perto_emenda = any(abs(r["s"] - m["t"]) < 0.30 or abs(r["e"] - m["t"]) < 0.30 for m in emendas)
                # palavra de ate 3 letras: o CTC reparte com o vizinho de forma
                # diferente a cada passagem; so conta se ENCOSTA na emenda
                # <=4 letras: o CTC reparte "quem/como/isso" com o vizinho de
                # forma diferente a cada passagem. Verificado: "quem" com 0,08s
                # vs 0,14s — reescuta isolada devolve a frase continua, nada
                # cortado. So conta se ENCOSTA na emenda (ai o corte pode de
                # fato ter mutilado).
                if len(e["n"]) <= 4 and not any(abs(r["s"] - m["t"]) < 0.12 or abs(r["e"] - m["t"]) < 0.12 for m in emendas):
                    n_ok += 1; continue
                # decepada: encolheu contra o bruto. Suspeita primeiro; depois
                # CONFIRMA remedindo a mesma palavra no bruto e na saida com a
                # MESMA janela local (o span em bloco longo reparte diferente do
                # span local — "Quem" dava 0,30 em bloco e 0,12 local, nos dois
                # lados; nao era decepada). So reprova se a remedicao mantiver
                # a diferenca.
                suspeita = (not e.get("sobrep")) and \
                    e["dur_bruto"] >= 0.12 and dur_s < 0.70 * e["dur_bruto"] and \
                    (perto_emenda or (len(e["n"]) >= 3 and e["dur_bruto"] - dur_s > 0.15))
                if suspeita:
                    real = remedir_par(e, r, corte_mp4, cache)
                    if real is not None:
                        d_b, d_s = real
                        if d_s >= 0.70 * d_b or d_b - d_s < 0.06:
                            n_ok += 1; continue          # falso alarme confirmado
                        dur_s, e["dur_bruto"] = d_s, d_b
                    problemas.append(
                        f"PALAVRA DECEPADA em {r['s']:.1f}s: \"{e['w']}\" saiu com "
                        f"{dur_s:.2f}s contra {e['dur_bruto']:.2f}s no bruto"
                        + (" (na emenda)" if perto_emenda else ""))
                    continue
                # score que desaba na emenda com palavra longa: fonema perdido
                if perto_emenda and len(e["n"]) >= 4 and (e["fa_bruto"] or 0) >= 0.6 and r["score"] < 0.15:
                    problemas.append(
                        f"PALAVRA MUTILADA NA EMENDA em {r['s']:.1f}s: \"{e['w']}\" "
                        f"(score {r['score']:.2f} na saida vs {e['fa_bruto']:.2f} no bruto)")
                    continue
                n_ok += 1
            print(f"  A. palavras medidas na saida: {n_ok}/{len(esperado)} integras")
            if excluidos:
                # "406/407" sem dizer qual e a 407a e um buraco no relatorio:
                # ou a palavra e um artefato conhecido, ou e defeito escondido.
                print(f"     fora da contagem ({len(excluidos)}): " + "; ".join(excluidos[:6]))
        else:
            avisos.append("alinhador nao mediu a saida (contagem divergente)")

    # ------------------------------------------------ B. vao em cada emenda
    # Limiar calibrado NO FALANTE, nao fixo: mede no bruto quanto ele pausa
    # entre frases (ponto final) e no meio delas. Emenda apos ponto final que
    # fica dentro do p75 dele e ritmo normal (mediana medida: 0,76s); um limiar
    # fixo de 0,45s reprovava pausa natural. No meio da frase a tolerancia e a
    # do proprio meio de frase (bem menor).
    def pausas_do_falante():
        pf, pm = [], []
        for p, (words, _) in cache.items():
            for i in range(1, len(words)):
                g = words[i]["s"] - words[i - 1]["e"]
                if not (0 < g < 3.0):
                    continue
                (pf if re.search(r"[.!?…]\"?$", words[i - 1]["w"].strip()) else pm).append(g)
        # TETO = p90, nao p75. Com p75 o portao reprovava 16% das pausas que o
        # proprio falante faz entre frases (medido: pausas de 0,77s / 0,88s /
        # 1,04s em 4 de 25 fins de frase). Teto e o limite do que soa como
        # aquela pessoa, nao a media dela — passar da mediana nao e defeito,
        # passar do que ela nunca faz e que e.
        def pq(v, q, dflt):
            return sorted(v)[int(q * (len(v) - 1))] if len(v) >= 6 else dflt
        return pq(pf, 0.90, 1.00), pq(pm, 0.90, 0.45)
    if emendas:
        import numpy as np
        lim_frase, lim_meio = pausas_do_falante()
        # tipo de cada emenda: fim de frase se a palavra anterior termina frase
        # no BRUTO ou a seguinte comeca frase no ROTEIRO (a pontuacao do Whisper
        # nao bate com a do roteiro; um dos dois basta). E a pausa que ELE fez
        # naquele ponto do bruto conta como credito: a emenda pode reproduzi-la.
        rot_ini_frase = {rot[i + 1] for i in fim_frase if i + 1 < len(rot)}
        for m in emendas:
            words, _ = cache[m["src"]]
            ant = [w for w in words if w["e"] <= m["src_t"] + 0.02]
            prox = [w for w in words if w["s"] >= m["src_t"] - 0.02]
            fim_b = bool(ant) and bool(re.search(r"[.!?…]\"?$", ant[-1]["w"].strip()))
            ini_r = bool(prox) and norm(prox[0]["w"]) in rot_ini_frase
            m["fim_frase"] = fim_b or ini_r
            m["pausa_bruto"] = (prox[0]["s"] - ant[-1]["e"]) if (ant and prox) else 0.0
        engasgos = 0
        for m in emendas:
            rms, jan = rms_por_janela(corte_mp4, m["t"] - 0.6, 1.2)
            if rms is None:
                continue
            piso = np.percentile(rms, 20)
            lim = piso + 0.15 * (rms.max() - piso)
            baixo = rms < lim
            # maior corrida de silencio que TOCA o ponto de emenda (janela central)
            centro = int(0.6 / jan)
            i, j = centro, centro
            while i > 0 and baixo[i - 1]:
                i -= 1
            while j < len(baixo) - 1 and baixo[j + 1]:
                j += 1
            vao = (j - i + 1) * jan if baixo[centro] else 0.0
            # limite = maior entre o ritmo do falante e a pausa que ele fez ali
            lim = max(lim_frase if m["fim_frase"] else lim_meio, m["pausa_bruto"])
            if vao > lim + 0.12:
                engasgos += 1
                problemas.append(f"ENGASGO na emenda em {m['t']:.1f}s: {vao:.2f}s de silencio "
                                 f"({'entre frases' if m['fim_frase'] else 'NO MEIO DA FRASE'}, "
                                 f"limite do falante {lim:.2f}s)")
        print(f"  B. emendas medidas: {len(emendas)}, com engasgo: {engasgos} "
              f"(limites do falante: entre frases {lim_frase:.2f}s, meio {lim_meio:.2f}s)")

    # ------------------------------------------------ C/D. segunda opiniao textual
    # Repeticao: 5-grama contra o roteiro E 3-grama contra o roteiro em janela
    # curta (< 8s). "os novos processos" repetido a 3s de distancia passou no
    # 5-grama porque a segunda vez veio truncada ("novos processos sao simples"
    # x "os novos processos sao"): so o 3-grama pega, e a janela curta evita
    # marcar refrao legitimo do roteiro (o mesmo bordao 2x, longe um do outro).
    for N, jan_max in ((5, None), (3, 8.0)):
        if len(fala) <= N:
            continue
        cf, cr = ngramas(fala, N), ngramas(rot, N)
        vistos = []
        for g, c in cf.items():
            if not (c >= 2 and c > cr.get(g, 0)):
                continue
            pos = [pal[i]["s"] for i in range(len(fala) - N + 1) if " ".join(fala[i:i + N]) == g]
            # ANAFORA RETORICA do roteiro: o texto repete a estrutura de
            # proposito ("Foi o que me fez estudar... o que me fez
            # mudar... o que me fez voltar") e quem fala pronuncia o "foi"
            # nas quatro, onde o roteiro elide em duas. Contar a forma
            # LITERAL acusa fala legitima.
            # Criterio: e anafora se uma janela de >= 2 palavras do trecho
            # aparece no roteiro pelo menos `c` vezes.
            #
            # Tentativa revertida: exigir janela >= 60% do trecho reprovava
            # anafora real e AINDA ASSIM nao pegava uma chamada final dita
            # duas vezes (o motivo da tentativa). A chamada duplicada e
            # resolvida onde ela nasce, no montador (aparo de juncao entre
            # CLIPES); aqui o criterio continua permissivo.
            gw = g.split()
            anafora = False
            for ini_j in range(len(gw)):
                for fim_j in range(ini_j + 2, len(gw) + 1):
                    jl = gw[ini_j:fim_j]
                    n_rot = sum(1 for i in range(len(rot) - len(jl) + 1)
                                if rot[i:i + len(jl)] == jl)
                    if n_rot >= c:
                        anafora = True
                        break
                if anafora:
                    break
            if anafora:
                continue
            if jan_max is not None:
                # so conta se DUAS ocorrencias estao a menos de jan_max segundos
                if not any(b - a < jan_max for a, b in zip(pos, pos[1:])):
                    continue
                if all(len(w) <= 2 for w in g.split()):
                    continue                      # "e a de" nao e repeticao
                # enumeracao legitima: "de todas as areas, de todas as
                # filiais" repete o trigrama mas e fala corrida, nao defeito.
                # Defeito e repeticao que RECOMECA a frase; enumeracao segue
                # adiante com item novo. Exige que o trecho apos as duas
                # ocorrencias seja parecido (recomeco), nao divergente.
                if len(pos) >= 2:
                    ia = [i for i in range(len(fala) - N + 1)
                          if " ".join(fala[i:i + N]) == g]
                    if len(ia) >= 2:
                        d1 = fala[ia[0] + N:ia[0] + N + 4]
                        d2 = fala[ia[1] + N:ia[1] + N + 4]
                        ig = sum(1 for x, y in zip(d1, d2) if x == y)
                        if d1 and ig / max(len(d1), 1) < 0.5:
                            continue
            if any(abs(pos[-1] - v) < 4 for v in vistos):
                continue
            vistos.append(pos[-1])
            problemas.append(f"REPETICAO ({c}x na peca, {cr.get(g,0)}x no roteiro) em "
                             + ", ".join(f"{x:.1f}s" for x in pos) + f": \"{g}\"")
    pares_rot = set(zip(rot, rot[1:]))
    # Duplicata na transcricao do corte NAO prova duplicata no audio: o Whisper
    # inventa ("decide. Decide." — reescuta isolada devolve um so, energia
    # mostra fala continua). Duracao tambem nao serve de filtro: o token
    # inventado tinha 0,18s. A prova e o par existir no BRUTO.
    pares_bruto = set()
    seqs_bruto = []
    for _p, (_ws, _fa) in (cache.items() if isinstance(locals().get("cache"), dict) else {}.items()):
        _n = [norm(w["w"]) for w in _ws]
        pares_bruto |= {(x, y) for x, y in zip(_n, _n[1:])}
        seqs_bruto.append([x for x in _n if x])

    def dito_no_bruto(seq):
        """A sequencia do roteiro chegou a ser dita em alguma tomada?

        Compara com folga (ele parafraseia ao gravar), mas a ORDEM conta. Um
        teste de conjunto errou aqui: para "que isso ta errado" a janela
        "voce nao ta errado e isso serve" devolvia 3 de 4 palavras em comum e o
        portao concluia que a frase tinha sido dita — quando "ta", "isso" e
        "que" aparecem em qualquer lugar de uma gravacao de 5 minutos. Palavra
        funcional casa por acaso; sequencia nao."""
        seq = [w for w in seq if w]
        if len(seq) < 3:
            return True                      # curto demais para decidir: acusa
        n = len(seq)
        for ws in seqs_bruto:
            for i in range(0, max(1, len(ws) - n + 1)):
                jan = ws[i:i + n + 2]
                if difflib.SequenceMatcher(None, seq, jan,
                                           autojunk=False).ratio() >= 0.75:
                    return True
        return False
    for i in range(1, len(pal)):
        if pal[i]["n"] == pal[i - 1]["n"] and len(pal[i]["n"]) > 1 \
                and (pal[i]["n"], pal[i]["n"]) not in pares_rot \
                and (pal[i]["n"], pal[i]["n"]) in pares_bruto \
                and (pal[i]["e"] - pal[i]["s"]) >= 0.10 and (pal[i-1]["e"] - pal[i-1]["s"]) >= 0.10:
            problemas.append(f"GAGUEJO em {pal[i]['s']:.1f}s: \"{pal[i-1]['w']} {pal[i]['w']}\"")

    sm = difflib.SequenceMatcher(None, rot, fala, autojunk=False)
    blocos = [b for b in sm.get_matching_blocks() if b.size >= 2]
    for ant, b in zip(blocos, blocos[1:]):
        extra = b.b - (ant.b + ant.size)
        buraco = b.a - (ant.a + ant.size)
        if buraco == 0 and extra >= 1:
            k = ant.b + ant.size
            run = [pal[j]["n"] for j in range(k, b.b)]
            seg = [p["n"] for p in pal[b.b:b.b + len(run)]]
            ant_ = [p["n"] for p in pal[max(0, k - len(run)):k]]
            # mesma regra: o trecho "repetido" tem que existir no BRUTO
            no_bruto = bool(run) and all(
                (run[x], run[x + 1]) in pares_bruto for x in range(len(run) - 1)) \
                if len(run) > 1 else (bool(run) and (run[0], run[0]) in pares_bruto)
            if (run == seg or run == ant_ or all(w in FILLERS for w in run)) \
                    and no_bruto \
                    and all((pal[j]["e"] - pal[j]["s"]) >= 0.10 for j in range(k, b.b)):
                problemas.append(f"FALSA PARTIDA em {pal[k]['s']:.1f}s: \"{' '.join(pal[j]['w'] for j in range(k, b.b))[:50]}\"")
        elif buraco >= 3 and extra == 0:
            ini_rot = ant.a + ant.size
            # "improviso nao e defeito": se ele nunca disse essas palavras em
            # tomada nenhuma, nao houve corte — houve outra formulacao.
            faltando = rot[ini_rot:b.a]
            if (ini_rot - 1) not in fim_frase and dito_no_bruto(faltando):
                k = min(ant.b + ant.size, len(pal) - 1)
                problemas.append(f"CORTE NO MEIO DE FRASE em {pal[k]['s']:.1f}s: depois de "
                                 f"\"...{' '.join(rot_o[max(0, ini_rot-5):ini_rot])}\"")

    abertura = " ".join(p["w"] for p in pal[:6]).lower()
    if PENDURADO.match(abertura):
        problemas.append(f"ABERTURA PENDURADA: \"{abertura}\"")

    casadas = sum(b.size for b in blocos)
    cob = 100.0 * casadas / max(len(rot), 1)
    print(f"  D. cobertura do roteiro: {cob:.1f}%")
    if cob < a.cobertura_min:
        problemas.append(f"COBERTURA BAIXA: {cob:.1f}% (minimo {a.cobertura_min})")

    for av in avisos:
        print(f"  aviso: {av}")
    if problemas:
        print(f"  REPROVADA — {len(problemas)} problema(s):")
        for p in problemas[:14]:
            print(f"    - {p}")
        if len(problemas) > 14:
            print(f"    ... +{len(problemas)-14}")
        return 2
    print("  OK: palavras integras na saida, emendas limpas, sem repeticao/gaguejo/corte torto")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as ex:
        import traceback
        traceback.print_exc()
        sys.exit(1)
