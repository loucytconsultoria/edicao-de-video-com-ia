# -*- coding: utf-8 -*-
"""Alinhamento FONETICO forcado: mede no audio onde cada palavra realmente
comeca e termina, dado o texto que foi falado.

Por que existe: o Whisper e bom em RECONHECER e ruim em MARCAR TEMPO. Seus
timestamps por palavra sao subproduto da decodificacao, nao medicao — e erram
sistematicamente em numeros ("414" marcado com 0,40s), palavras curtas e fim de
fala (encurta a ultima palavra quando alucina algo depois). Como todo o corte
usava esses tempos como regua, palavra decepada e engasgo eram inevitaveis, e
"reouvir com o mesmo Whisper" nao resolvia porque a regua era a mesma.

O alinhador (MMS_FA, wav2vec2 CTC, 1.100+ idiomas) faz o inverso: recebe o
texto e localiza cada letra no audio. E o instrumento certo para borda de corte.

Uso:
  python alinhar_fonetico.py AUDIO.wav --texto "o que foi falado" [--ini T] [--json saida]
  python alinhar_fonetico.py PASTA_ANALISE           # realinha analise.json inteiro

Variaveis de ambiente:
  VIDEO_IA_PRONUNCIAS      JSON {sigla: [formas faladas]} (default config/pronuncias_exemplo.json)
  VIDEO_IA_WHISPER_MODELO  modelo do faster-whisper usado na retranscricao local
                           de audio nao explicado (default large-v3)
"""
import argparse, json, os, re, subprocess, sys, tempfile, unicodedata

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_BUNDLE = None
_MODEL = None
_TOK = None
_ALN = None
_DEV = None


def _carregar():
    global _BUNDLE, _MODEL, _TOK, _ALN, _DEV
    if _MODEL is not None:
        return
    import torch, torchaudio
    from torchaudio.pipelines import MMS_FA
    _DEV = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _BUNDLE = MMS_FA
    # with_star=True: o token <star> absorve audio que o texto nao explica
    # (alucinacao "Boa noite", repeticao colapsada). Sem ele o alinhador e
    # OBRIGADO a espalhar esse audio pelos vizinhos e distorce a borda deles.
    _MODEL = MMS_FA.get_model(with_star=True).to(_DEV)
    _TOK = MMS_FA.get_tokenizer()
    _ALN = MMS_FA.get_aligner()


# Forma FALADA de siglas e simbolos. O alinhador casa letra-som; uma sigla
# soletrada ("RH" -> "erre aga") nao existe no audio como "r h", "%" some na
# normalizacao e "por cento" era absorvido pela palavra seguinte. Sem isto o
# instrumento novo reproduz o erro do velho nas mesmas palavras.
# O dicionario do dominio (siglas da empresa, nomes de produto) vem de um JSON
# externo; aqui ficam so os simbolos universais.
FALADO = {
    "%": ["por cento"], "r$": ["reais"], "ok": ["oquei", "o ca"],
    "ia": ["ia", "i a"],
}


def _carregar_pronuncias():
    from pathlib import Path
    cam = os.environ.get("VIDEO_IA_PRONUNCIAS") or str(
        Path(__file__).resolve().parent.parent / "config" / "pronuncias_exemplo.json")
    try:
        extra = json.load(open(cam, encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    for k, v in extra.items():
        if not k.startswith("_") and isinstance(v, list):
            FALADO[k.lower()] = v


_carregar_pronuncias()


def _limpa(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z' ]+", " ", s).strip()


def formas_de_numero(n_str):
    """Um numero pode ser dito de varios jeitos: '1520' pode ser 'mil
    quinhentos e vinte' (leitura canonica) ou 'quinze vinte' (codigo lido em
    pares); '2001' pode ser 'dois mil e um' ou 'dois mil e uma'. Devolve as
    hipoteses; o alinhador fica com a de melhor score."""
    from num2words import num2words
    n = re.sub(r"[.,]", "", n_str)
    hip = []
    try:
        hip.append(num2words(int(n), lang="pt_BR"))
    except Exception:
        pass
    if len(n) == 4 and n[0] != "0":                 # codigo de 4 digitos lido em pares
        try:
            hip.append(num2words(int(n[:2]), lang="pt_BR") + " " +
                       ("zero " if n[2] == "0" else "") +
                       num2words(int(n[2:]) if n[2] != "0" else int(n[3]), lang="pt_BR"))
        except Exception:
            pass
    if len(n) <= 4:                                  # digito a digito
        try:
            hip.append(" ".join(num2words(int(c), lang="pt_BR") for c in n))
        except Exception:
            pass
    return [_limpa(h) for h in hip if h] or [n]


def hipoteses_da_palavra(w):
    """lista de formas faladas candidatas (cada uma = lista de tokens)"""
    w0 = w.strip()
    chave = w0.lower().strip(".,;:!?\"'()")
    if chave in FALADO:
        return [f.split() for f in FALADO[chave]]
    m = re.fullmatch(r"(\d[\d.,]*)(%?)", chave)
    if m:
        hs = formas_de_numero(m.group(1))
        if m.group(2):
            hs = [h + " por cento" for h in hs]
        return [h.split() for h in hs]
    l = _limpa(w0)
    return [l.split()] if l else [[]]


def normalizar_para_alinhar(txt):
    """forma canonica (primeira hipotese) — usada quando nao ha escolha"""
    return [t for w in txt.split() for t in hipoteses_da_palavra(w)[0]]


def _wav_16k(video, ini, dur):
    wav = os.path.join(tempfile.gettempdir(), f"videoia_fa_{os.getpid()}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(max(0.0, ini)),
                    "-i", video, "-t", str(dur), "-vn", "-ar", "16000", "-ac", "1",
                    "-y", wav], capture_output=True)
    return wav if os.path.exists(wav) else None


def alinhar(video, ini, dur, palavras_originais):
    """devolve [{w, s, e, score}] com tempos ABSOLUTOS no video"""
    _carregar()
    import torch, torchaudio
    wav = _wav_16k(video, ini, dur)
    if not wav:
        return []
    # le o WAV direto: o torchaudio no Windows nao traz backend de audio, e o
    # ffmpeg ja entregou 16 kHz mono PCM 16-bit
    import wave, numpy as np
    with wave.open(wav, "rb") as wf:
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    os.remove(wav)
    forma = torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0)
    if sr != 16000:
        forma = torchaudio.functional.resample(forma, sr, 16000)
    with torch.inference_mode():
        emissao, _ = _MODEL(forma.to(_DEV))
    n_frames = emissao.size(1)
    razao = forma.size(1) / n_frames / 16000.0      # segundos por frame

    def alinhar_plano(grupos):
        """grupos: [(palavra_original, [tokens])] -> [(s,e,score)] por palavra"""
        plano = [t for _, ts in grupos for t in ts]
        if not plano:
            return None
        try:
            spans = _ALN(emissao[0], _TOK(plano))
        except Exception as e:
            print(f"  alinhador falhou: {e}", file=sys.stderr)
            return None
        res, k = [], 0
        for w, toks in grupos:
            if not toks:
                res.append(None); continue
            sp = spans[k:k + len(toks)]
            s0, e0 = sp[0][0].start, sp[-1][-1].end
            sc = sum(t.score for span in sp for t in span) / max(1, sum(len(span) for span in sp))
            res.append((ini + s0 * razao, ini + e0 * razao, sc))
            k += len(toks)
        return res

    # 1) plano canonico
    hip = [hipoteses_da_palavra(w) for w in palavras_originais]
    grupos = [(w, h[0]) for w, h in zip(palavras_originais, hip)]
    base = alinhar_plano(grupos)
    if base is None:
        return []
    # 2) para palavras com hipoteses alternativas (numero, sigla) e score
    #    fraco, testa cada alternativa isoladamente e fica com a melhor
    for i, (w, hs) in enumerate(zip(palavras_originais, hip)):
        if len(hs) < 2 or base[i] is None or base[i][2] >= 0.6:
            continue
        melhor = base[i]; melhor_toks = grupos[i][1]
        for alt in hs[1:]:
            g2 = list(grupos); g2[i] = (w, alt)
            r2 = alinhar_plano(g2)
            if r2 and r2[i] and r2[i][2] > melhor[2]:
                melhor, melhor_toks = r2[i], alt
        if melhor is not base[i]:
            grupos[i] = (w, melhor_toks)
            base = alinhar_plano(grupos) or base    # realinha tudo com a escolha
    saida = []
    for w, r in zip(palavras_originais, base):
        if r is None:
            saida.append({"w": w, "s": None, "e": None, "score": 0.0}); continue
        saida.append({"w": w, "s": round(r[0], 3), "e": round(r[1], 3),
                      "score": round(r[2], 3)})
    return saida




# O Whisper preenche silencio com estas frases. Medido em 23 tomadas reais:
# todas voltaram com score do alinhador abaixo de 0,15, enquanto fala de
# verdade no mesmo material ficou entre 0,33 e 0,89.
HALUCINACOES = {"obrigado", "obrigada", "tchau", "musica", "super", "hum", "ah",
                "amem", "legendas", "inscrevase", "valeu", "ate", "logo", "oi",
                "bom", "boa", "noite", "dia", "fim"}


def _e_alucinacao(reais):
    """O pedaco retranscrito e alucinacao do ASR sobre silencio?"""
    if not reais:
        return True
    import re as _re
    def _n(x):
        return _re.sub(r"[^a-z0-9]", "", _limpa(x["w"]))
    melhor = max((x.get("fa") or 0.0) for x in reais)
    if melhor < 0.25:
        # medido: fala de verdade neste material fica entre 0,33 e 0,89;
        # alucinacao sobre silencio, entre 0,02 e 0,20
        return True
    if melhor < 0.30 and all(_n(x) in HALUCINACOES for x in reais):
        return True
    return False


def _envelope(video, ini, dur, jan=0.025):
    """RMS em dBFS por janela de 25 ms. Devolve (array_db, jan) ou (None, jan)."""
    import numpy as np, wave
    wav = _wav_16k(video, max(0.0, ini), dur)
    if not wav:
        return None, jan
    try:
        with wave.open(wav, "rb") as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()),
                                dtype="int16").astype("float32")
    except Exception:
        return None, jan
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    n = int(16000 * jan)
    if len(pcm) < 2 * n:
        return None, jan
    r = np.array([np.sqrt(np.mean(pcm[i:i + n] ** 2))
                  for i in range(0, len(pcm) - n, n)])
    return 20 * np.log10(np.maximum(r, 1e-6) / 32768.0), jan


def limiar_de_silencio(video, dur):
    """Limiar RELATIVO ao falante. Absoluto nao serve: -38 dB nao achou nada em
    duas regioes mudas de uma gravacao real, porque ruido de sala e barulho de
    boca ficam acima disso. Piso + 30% da distancia ate o nivel de voz separa
    limpo."""
    import numpy as np
    db, _ = _envelope(video, 0.0, min(dur, 240.0), jan=0.05)
    if db is None or not len(db):
        return -55.0
    piso = float(np.percentile(db, 5))
    voz = float(np.percentile(db, 90))
    if voz - piso < 12:
        return piso + 6.0
    return piso + 0.30 * (voz - piso)


def silencio_dentro_do_span(video, s, e, limiar, minimo=0.40):
    """Corridas continuas de silencio dentro de [s, e]. Devolve [(a, b), ...]."""
    import numpy as np
    dur = e - s
    if dur < minimo + 0.10:
        return []
    db, jan = _envelope(video, s, dur)
    if db is None:
        return []
    quieto = db < limiar
    saida, k = [], 0
    while k < len(quieto):
        if not quieto[k]:
            k += 1
            continue
        j = k
        while j < len(quieto) and quieto[j]:
            j += 1
        if (j - k) * jan >= minimo:
            saida.append((round(s + k * jan, 3), round(s + j * jan, 3)))
        k = j
    return saida


def _silencios(video, dur, ruido="-35dB", minimo=0.25):
    """[(ini,fim)] dos silencios do arquivo inteiro, medidos por energia"""
    r = subprocess.run(["ffmpeg", "-v", "info", "-nostdin", "-i", video, "-vn", "-af",
                        f"silencedetect=noise={ruido}:d={minimo}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ini = re.findall(r"silence_start:\s*(-?[\d.]+)", r.stderr or "")
    fim = re.findall(r"silence_end:\s*(-?[\d.]+)", r.stderr or "")
    out = []
    for a, b in zip(ini, fim):
        out.append((float(a), float(b)))
    if len(ini) > len(fim):
        out.append((float(ini[-1]), dur))
    return out


_WHISPER = {}


def _whisper_local():
    """faster-whisper para retranscrever janelas curtas (audio nao explicado).
    Modelo por $VIDEO_IA_WHISPER_MODELO; CUDA quando houver, senao CPU."""
    if "m" in _WHISPER:
        return _WHISPER["m"]
    nome = os.environ.get("VIDEO_IA_WHISPER_MODELO", "large-v3")
    m = None
    try:
        from faster_whisper import WhisperModel
        try:
            m = WhisperModel(nome, device="cuda", compute_type="float16")
        except Exception:
            m = WhisperModel(nome, device="cpu", compute_type="int8")
    except Exception as e:
        print(f"  retranscricao local indisponivel ({type(e).__name__}): {e}", file=sys.stderr)
        m = None
    _WHISPER["m"] = m
    return m


def realinhar_analise(pasta, bloco_s=22.0):
    """Substitui os tempos de analise.json pela medicao fonetica, sem mudar o
    TEXTO. Regras (todas vieram de defeito em entrega):
      - blocos cortados em SILENCIO MEDIDO, nao em gap do Whisper (que cola
        pausa na palavra e erra o gap);
      - 100% das palavras saem com regua FA: span colapsado e interpolado
        entre vizinhos FA — nunca fica um tempo Whisper no meio (misturar
        reguas gerava sobreposicao e vao negativo em todo consumidor);
      - score e gravado por palavra como FLAG de arbitragem (nao portao):
        baixo = texto duvidoso ali (alucinacao, sigla, repeticao);
      - escrita atomica; ordem das etapas registrada."""
    cam = os.path.join(pasta, "analise.json")
    d = json.load(open(cam, encoding="utf-8"))
    video, pal, dur = d["video"], d["palavras"], d["meta"]["duracao"]
    idioma = (d.get("transcricao") or {}).get("idioma") or "pt"
    if not pal:
        return 0
    sil = _silencios(video, dur)

    def em_silencio(t):
        return any(a <= t <= b for a, b in sil)

    # 0) SANEAMENTO DO TEXTO ANTES DE MEDIR. Token que o Whisper alucinou
    #    ("Boa noite" no fim, quando ninguem disse) forca o alinhador a lhe dar
    #    tempo — roubado da palavra vizinha (uma palavra caiu de 0,42s para
    #    0,18s so por causa disso, mesmo com <star>). Regra: no FIM do arquivo,
    #    depois da ultima palavra do roteiro, tokens de baixa confianca e
    #    duracao ~zero sao alucinacao e saem do texto.
    removidos = []
    if len(pal) >= 4:
        # candidata a cauda alucinada: os ultimos 1-3 tokens, se juntos duram
        # menos do que e fisicamente possivel falar (~0,12s por token) E ha
        # pelo menos um com confianca baixa ou duracao ~zero. "Boa noite." com
        # 0,16s para 2 palavras (0,06 + 0,10) e o caso real.
        for n in (3, 2, 1):
            cauda = pal[-n:]
            dur_c = cauda[-1]["e"] - cauda[0]["s"]
            fraco = any((x["e"] - x["s"]) < 0.08 or x.get("p", 1.0) < 0.6 for x in cauda)
            if dur_c < 0.12 * n and fraco and (dur - cauda[-1]["e"]) < 3.0:
                removidos = [x["w"].strip() for x in cauda]
                pal = pal[:-n]
                break
    if removidos:
        print(f"  alucinacao removida do fim: {' '.join(removidos)!r}")

    # blocos: quebra onde ha silencio medido entre duas palavras (ou bloco longo)
    blocos, atual = [], [0]
    for i in range(1, len(pal)):
        meio = (pal[i - 1]["e"] + pal[i]["s"]) / 2
        gap_sil = any(a <= meio <= b for a, b in sil)
        longo = pal[i]["e"] - pal[atual[0]]["s"] > bloco_s
        if gap_sil or (longo and pal[i]["s"] - pal[i - 1]["e"] > 0.25):
            blocos.append(atual); atual = []
        atual.append(i)
    blocos.append(atual)

    novas = list(pal)
    medidas = {}
    for idx in blocos:
        idx = [i for i in idx if i < len(pal)]
        if not idx:
            continue
        # janela: do fim do silencio anterior ao inicio do proximo (nunca entra
        # na palavra vizinha), com teto de folga
        ini = max(0.0, pal[idx[0]]["s"] - 0.45)
        fim = min(dur, pal[idx[-1]]["e"] + 0.45)
        for a, b in sil:
            if b <= pal[idx[0]]["s"] and b > ini:
                ini = max(ini, b - 0.05)
            if a >= pal[idx[-1]]["e"] and a < fim:
                fim = min(fim, a + 0.05)
        textos = [pal[i]["w"].strip() for i in idx]
        res = alinhar(video, ini, fim - ini, textos)
        if len(res) != len(idx):
            continue
        for i, r in zip(idx, res):
            if r["s"] is not None and r["e"] - r["s"] >= 0.02:
                medidas[i] = (r["s"], r["e"], r["score"])

    # 100% FA: interpola os que nao mediram entre vizinhos medidos
    interp = 0
    for i in range(len(pal)):
        if i in medidas:
            continue
        a = next((k for k in range(i - 1, -1, -1) if k in medidas), None)
        b = next((k for k in range(i + 1, len(pal)) if k in medidas), None)
        if a is None or b is None:
            continue
        s0, e0 = medidas[a][1], medidas[b][0]
        n = b - a
        passo = (e0 - s0) / n
        k = i - a
        medidas[i] = (s0 + (k - 1) * passo, s0 + k * passo, 0.0)
        interp += 1
    for i, (s, e, sc) in medidas.items():
        novas[i] = {**pal[i], "s": round(s, 3), "e": round(e, 3),
                    "p_whisper": pal[i].get("p", 1.0), "fa": round(max(sc, 0.0), 3),
                    "instrumento": ("fa" if sc > 0 else ("fa-invalida" if sc < 0 else "fa-interp"))}
    # remove sobreposicao residual (arredondamento): fim nunca passa do proximo inicio
    for i in range(1, len(novas)):
        if novas[i]["s"] < novas[i - 1]["e"]:
            m = (novas[i]["s"] + novas[i - 1]["e"]) / 2
            novas[i - 1]["e"] = round(m - 0.005, 3); novas[i]["s"] = round(m + 0.005, 3)

    # MEDICAO INVALIDA (depois de resolver sobreposicao, que e quem produz
    # duracao negativa): duracao <= 0 ou fisicamente impossivel para o tamanho
    # ("escolher" 8 letras em 0,14s; "melhor" em 0,10s — caso real em que o
    # alinhador desalinhou porque o texto ali era falsa partida). Marca como
    # invalida e re-interpola entre vizinhas validas. Nunca entrega ao corte um
    # span em que a palavra "cabe" em 0,10s.
    def _plausivel(w, s, e):
        if (e - s) <= 0.005:
            return False
        n = len(re.sub(r"[^a-z0-9]", "", _limpa(w))) or 1
        return n < 4 or (e - s) >= 0.03 * n
    ok_idx = [i for i in range(len(novas)) if _plausivel(novas[i]["w"], novas[i]["s"], novas[i]["e"])]
    n_inv = 0
    for i in range(len(novas)):
        if _plausivel(novas[i]["w"], novas[i]["s"], novas[i]["e"]):
            continue
        a = next((k for k in reversed(ok_idx) if k < i), None)
        b = next((k for k in ok_idx if k > i), None)
        novas[i]["instrumento"] = "fa-invalida"
        novas[i]["fa"] = 0.0
        n_inv += 1
        if a is None or b is None:
            continue
        s0, e0 = novas[a]["e"], novas[b]["s"]
        n = b - a; passo = (e0 - s0) / n; k = i - a
        novas[i]["s"] = round(s0 + (k - 1) * passo, 3)
        novas[i]["e"] = round(s0 + k * passo, 3)
    if n_inv:
        print(f"  spans implausiveis re-interpolados: {n_inv}")

    # audio NAO explicado: energia de fala > 0,3s dentro de bloco sem span
    # nenhum = repeticao colapsada, falsa partida ou texto errado. Substitui
    # heuristicas de duracao de palavra, que erravam nos dois sentidos.
    nao_explicado = []
    fala = []
    t = 0.0
    for a, b in sil + [(dur, dur)]:
        if a - t > 0.3:
            fala.append((t, a))
        t = b
    spans = sorted((novas[i]["s"], novas[i]["e"]) for i in medidas)
    for fa, fb in fala:
        cur = fa
        for s, e in spans:
            if e < fa or s > fb:
                continue
            if s - cur > 0.30:
                nao_explicado.append((round(cur, 2), round(s, 2)))
            cur = max(cur, e)
        if fb - cur > 0.30:
            nao_explicado.append((round(cur, 2), round(fb, 2)))

    # AUDIO NAO EXPLICADO -> RETRANSCREVER E INSERIR. Regiao de fala >= 0,3s
    # sem palavra e o que o Whisper colapsou ("os novos processos sao [5s]
    # simples": os 5s ERAM "os novos processos sao" dito de novo). Se
    # ficar sem texto, o montador trata como pausa longa e corta no meio da
    # repeticao. Aqui cada regiao e retranscrita ISOLADA (em janela curta o
    # Whisper acerta), alinhada, e as palavras entram na lista — assim o
    # montador enxerga a repeticao e a corta inteira.
    inseridas = 0
    if nao_explicado:
        wm = _whisper_local()
        extras = []
        for (ra, rb) in nao_explicado:
            if wm is None or rb - ra < 0.30:
                continue
            wav = _wav_16k(video, max(0.0, ra - 0.15), (rb - ra) + 0.30)
            if not wav:
                continue
            segs, _ = wm.transcribe(wav, language=idioma, word_timestamps=True, vad_filter=False)
            ws = [x for s_ in segs for x in s_.words]
            os.remove(wav)
            if not ws:
                continue
            txt = [x.word.strip() for x in ws if x.word.strip()]
            res = alinhar(video, max(0.0, ra - 0.15), (rb - ra) + 0.30, txt)
            for r in res:
                if r["s"] is None or not (ra - 0.2 <= r["s"] <= rb + 0.2):
                    continue
                extras.append({"w": " " + r["w"], "s": round(r["s"], 3), "e": round(r["e"], 3),
                               "p": 0.5, "fa": round(r["score"], 3), "instrumento": "fa-retranscrito"})
        if extras:
            # dedup: palavra retranscrita que SOBREPOE (>50%) uma ja existente
            # com o mesmo texto e a mesma palavra vista duas vezes ("poucos" do
            # Whisper + "poucos." da retranscricao) — fica a original
            def _n(w): return re.sub(r"[^a-z0-9]", "", _limpa(w["w"]))
            keep = []
            for x in extras:
                dup = False
                for w in novas:
                    ov = min(x["e"], w["e"]) - max(x["s"], w["s"])
                    if ov > 0 and ov > 0.5 * min(x["e"] - x["s"], w["e"] - w["s"]) and _n(x) == _n(w):
                        dup = True; break
                if not dup:
                    keep.append(x)
            extras = keep
            # MESMA GUARDA DO SPAN PARTIDO. Este caminho e o mais antigo e o
            # que mais insere (48 das 72 palavras num lote medido), e estava
            # sem nenhuma: entregou 'Obrigado.' (score 0,022) no meio de uma
            # frase e uma palavra inventada (0,034) onde nao havia fala.
            if _e_alucinacao(extras):
                if extras:
                    print("  retranscricao descartada (alucinacao sobre silencio): "
                          + " ".join(x["w"].strip() for x in extras[:8])
                          + f"  [melhor score {max((x.get('fa') or 0) for x in extras):.3f}]")
                extras = []
            novas = sorted(novas + extras, key=lambda w: w["s"])
            inseridas = len(extras)
            print(f"  audio nao explicado retranscrito: +{inseridas} palavras "
                  f"({' / '.join(x['w'].strip() for x in extras[:12])})")

    # ---------------- AUDIO ENGOLIDO DENTRO DO SPAN DE UMA PALAVRA ----------
    # Vao entre palavras qualquer regra pega. O que passava era o silencio
    # DENTRO de um token: "633" com 3,05s escondendo um embolo e uma pausa,
    # " O" com 1,88s e p=0,06 cobrindo 2,8s de mao no celular. Aqui o span e
    # partido no silencio e cada pedaco com fala e retranscrito isolado.
    def _norm_ins(x):
        # NAO usa _limpa: ela descarta digitos (existe para alimentar o
        # alinhador, que fala em fonemas). Aqui compara-se IDENTIDADE de
        # palavra, e "414" some se os digitos sairem — foi assim que a guarda
        # de "a retranscricao devolveu a palavra?" nao rodava justamente nos
        # numeros, que sao os tokens que mais incham de span.
        s = unicodedata.normalize("NFD", x["w"].lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]", "", s)

    def _dedup_contra(novos, existentes):
        """Descarta inserida que SOBREPOE (>50%) uma existente com o mesmo texto:
        e a mesma palavra vista duas vezes, nao duas execucoes."""
        fica = []
        for x in novos:
            dup = False
            for w in existentes:
                # PROXIMIDADE, nao sobreposicao. Exigir 50% de sobreposicao
                # deixava passar "horas." (129.539-129.639) contra "horas"
                # (129.609-129.890): elas se tocam por apenas 0,03s e o teste
                # falhava — a peca dizia "633 horas. horas para revisar".
                # Duas ocorrencias da MESMA palavra a menos de 0,35s uma da
                # outra sao a mesma fala vista duas vezes, nao duas execucoes.
                perto = (abs(x["s"] - w["s"]) < 0.35 or abs(x["e"] - w["e"]) < 0.35
                         or min(x["e"], w["e"]) > max(x["s"], w["s"]))
                if perto and _norm_ins(x) == _norm_ins(w):
                    dup = True
                    break
            if not dup:
                fica.append(x)
        return fica

    engolidos = removidos_mudos = 0
    try:
        lim_sil = limiar_de_silencio(video, dur)
    except Exception:
        lim_sil = None
    if lim_sil is not None:
        suspeitos = [w for w in novas
                     if (w["e"] - w["s"]) >= 0.80 and w.get("instrumento") != "fa-invalida"]
        wm2 = None
        trocas = []
        for w in suspeitos:
            buracos = silencio_dentro_do_span(video, w["s"], w["e"], lim_sil)
            if not buracos:
                continue
            mudo = sum(b - a_ for a_, b in buracos) / max(1e-6, w["e"] - w["s"])
            pedacos, cur = [], w["s"]
            for (ba, bb) in buracos:
                if ba - cur > 0.12:
                    pedacos.append((cur, ba))
                cur = bb
            if w["e"] - cur > 0.12:
                pedacos.append((cur, w["e"]))
            if wm2 is None:
                wm2 = _whisper_local() or False
            if wm2 is False:
                continue
            aceitos = []
            for (pa, pb) in pedacos:
                wav = _wav_16k(video, max(0.0, pa - 0.05), (pb - pa) + 0.10)
                if not wav:
                    continue
                segs_, _ = wm2.transcribe(wav, language=idioma, word_timestamps=True,
                                          vad_filter=False)
                txt = [x.word.strip() for s_ in segs_ for x in s_.words if x.word.strip()]
                try:
                    os.remove(wav)
                except OSError:
                    pass
                if not txt:
                    continue
                res = alinhar(video, max(0.0, pa - 0.05), (pb - pa) + 0.10, txt)
                cand = [{"w": " " + r["w"], "s": round(r["s"], 3), "e": round(r["e"], 3),
                         "p": 0.5, "fa": round(r["score"], 3),
                         "instrumento": "fa-span-partido"}
                        for r in res
                        if r["s"] is not None and (pa - 0.10 <= r["s"] <= pb + 0.10)]
                # o PEDACO responde pelo seu melhor score, nao pelo pior:
                # '633' real saiu com 0,083 dentro de um pedaco de 0,883
                if _e_alucinacao(cand):
                    if cand:
                        print(f"  pedaco descartado (alucinacao sobre silencio): "
                              + " ".join(x["w"].strip() for x in cand)
                              + f"  [melhor score {max((x.get('fa') or 0) for x in cand):.3f}]")
                    continue
                aceitos.extend(cand)
            # dedup contra as palavras que JA existem fora do span
            vizinhas = [x for x in novas if x is not w
                        and x["e"] > w["s"] - 0.60 and x["s"] < w["e"] + 0.60]
            aceitos = _dedup_contra(aceitos, vizinhas)
            trocas.append((w, aceitos, mudo, pedacos))
        for w, aceitos, mudo, pedacos in trocas:
            # SO SUBSTITUI SE A PALAVRA ORIGINAL ESTIVER NO QUE ENTRA. Um span
            # pode ter um pedaco aceito e outro rejeitado; trocar assim apaga a
            # palavra. Medido: '414' (2,03s) tinha o pedaco do numero rejeitado
            # (score 0,084) e o outro aceito ("Sem o..."), e o numero sumia da
            # tomada. Se o que entra nao contem a palavra que sai, o original
            # fica e as insercoes sao descartadas.
            if (aceitos and _norm_ins(w)
                    and not any(_norm_ins(x) == _norm_ins(w) for x in aceitos)):
                print(f"  span com silencio dentro: {w['w'].strip()!r} mantido "
                      f"(a retranscricao nao devolveu a palavra)")
                aceitos = []
            if aceitos:
                if w in novas:
                    novas.remove(w)
                novas.extend(aceitos)
                engolidos += 1
                print(f"  span com silencio dentro: {w['w'].strip()!r} "
                      f"({w['e'] - w['s']:.2f}s) -> "
                      + " | ".join(x["w"].strip() for x in aceitos))
            elif sum(b_ - a_ for a_, b_ in pedacos) < 0.12:
                # nao sobrou fala nenhuma: o token cobria silencio (a mao no
                # celular). Sai, e o vao real fica exposto ao corte de silencio.
                if w in novas:
                    novas.remove(w)
                removidos_mudos += 1
                print(f"  span mudo removido: {w['w'].strip()!r} "
                      f"({w['e'] - w['s']:.2f}s, {mudo*100:.0f}% sem fala)")
            elif mudo >= 0.30:
                # ENCOLHER, NAO REMOVER. Um span 86% mudo ainda tem fala: o
                # fragmento final de um numero dito em duas partes media 2,67s
                # com 0,37s de voz. Remove-lo apagava o fim do numero da lista
                # de palavras, e o montador podia fechar a peca no meio dele. O
                # token fica com o span reduzido a parte falada: a palavra
                # protegida E o silencio virando vao visivel ao corte.
                s_novo, e_novo = pedacos[0][0], pedacos[-1][1]
                if e_novo - s_novo >= 0.05 and (e_novo - s_novo) < (w["e"] - w["s"]) - 0.05:
                    print(f"  span encolhido ate a fala: {w['w'].strip()!r} "
                          f"{w['e'] - w['s']:.2f}s -> {e_novo - s_novo:.2f}s "
                          f"({mudo*100:.0f}% era silencio)")
                    w["s"], w["e"] = round(s_novo, 3), round(e_novo, 3)
                    engolidos += 1
            else:
                # nao havia o que por no lugar: o original FICA. Remover aqui
                # apagava '414' da tomada porque a retranscricao veio ruim.
                print(f"  span com silencio dentro: {w['w'].strip()!r} mantido "
                      f"(retranscricao sem credibilidade)")
        if trocas:
            novas = sorted(novas, key=lambda x: x["s"])

    d["palavras"] = novas
    d["alinhamento_fonetico"] = {"medidas": len(medidas) - interp, "interpoladas": interp,
                                 "total": len(pal), "audio_nao_explicado": nao_explicado,
                                 "spans_com_silencio_partidos": engolidos,
                                 "limiar_silencio_db": (round(lim_sil, 1) if lim_sil else None),
                                 "alucinacao_removida_fim": removidos,
                                 "retranscritas_inseridas": inseridas}
    d.setdefault("etapas", []).append("alinhar_fonetico")
    if not os.path.exists(cam + ".pre_fa"):
        os.replace(cam, cam + ".pre_fa")
    tmp = cam + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, cam)
    if nao_explicado:
        print(f"  audio nao explicado pelo texto ({len(nao_explicado)}): "
              + ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in nao_explicado[:8]))
    return len(medidas)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("alvo", help="video/wav (com --texto) ou pasta de analise")
    ap.add_argument("--texto", default=None)
    ap.add_argument("--ini", type=float, default=0.0)
    ap.add_argument("--dur", type=float, default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    if os.path.isdir(a.alvo):
        n = realinhar_analise(a.alvo)
        print(f"{os.path.basename(a.alvo)}: {n} palavras realinhadas foneticamente")
        return 0

    if not a.texto:
        print("--texto obrigatorio para arquivo de audio/video", file=sys.stderr)
        return 2
    dur = a.dur
    if dur is None:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", a.alvo], capture_output=True, text=True)
        dur = float(r.stdout.strip()) - a.ini
    res = alinhar(a.alvo, a.ini, dur, a.texto.split())
    for r in res:
        print(f"  {r['s']:8.3f}-{r['e']:8.3f}  ({r['e']-r['s']:.2f}s) score {r['score']:.2f}  {r['w']}")
    if a.json:
        json.dump(res, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
