# -*- coding: utf-8 -*-
"""Mede o VIES do alinhador fonetico contra o envelope de energia do audio.

O CTC tem emissao 'peaky': tende a marcar o inicio um pouco tarde e o fim um
pouco cedo. Os pads do montador foram calibrados contra a regua do Whisper
(errada) e nao podem ser reaproveitados. Aqui, para N palavras isoladas por
silencio (as unicas em que 'onde a fala comeca/termina' e mensuravel sem
ambiguidade), comparo o span FA com o onset/offset de energia.

Saida: distribuicao (mediana, p90) de   onset_energia - s_FA   e   e_FA - offset_energia
-> quanto o pad_antes e o pad_depois precisam ter para NAO decepar.

Uso: python calibrar_fa.py PASTA_ANALISE   (analise.json ja realinhado)
Rodar uma vez por setup de gravacao (microfone, sala, falante)."""
import json, os, statistics, subprocess, sys, wave
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def envelope(video, ini, dur, sr=16000, jan=0.010):
    import tempfile
    wav = os.path.join(tempfile.gettempdir(), f"videoia_cal_{os.getpid()}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(ini), "-i", video,
                    "-t", str(dur), "-vn", "-ar", str(sr), "-ac", "1", "-y", wav],
                   capture_output=True)
    with wave.open(wav, "rb") as f:
        pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32)
    n = int(sr * jan)
    rms = np.array([np.sqrt(np.mean(pcm[i:i + n] ** 2)) for i in range(0, len(pcm) - n, n)])
    return rms, jan


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    pasta = sys.argv[1]
    d = json.load(open(os.path.join(pasta, "analise.json"), encoding="utf-8"))
    pal = [w for w in d["palavras"] if "fa" in w and w["w"].strip()]
    video = d["video"]
    antes, depois, usadas = [], [], 0
    for i in range(1, len(pal) - 1):
        w = pal[i]
        # so palavras isoladas por >=0,35s de cada lado: onset/offset sem ambiguidade
        if w["s"] - pal[i - 1]["e"] < 0.20 or pal[i + 1]["s"] - w["e"] < 0.20:
            continue
        if w["e"] - w["s"] < 0.15 or w.get("fa", 0) < 0.5:
            continue
        ini = w["s"] - 0.18
        rms, jan = envelope(video, ini, (w["e"] - w["s"]) + 0.36)
        if len(rms) < 10:
            continue
        piso = np.percentile(rms, 15)
        pico = rms.max()
        if pico < 4 * piso + 1:
            continue
        lim = piso + 0.12 * (pico - piso)
        acima = np.where(rms > lim)[0]
        if len(acima) == 0:
            continue
        onset = ini + acima[0] * jan
        offset = ini + (acima[-1] + 1) * jan
        antes.append(w["s"] - onset)      # >0: FA comeca DEPOIS da energia (atrasado)
        depois.append(offset - w["e"])    # >0: FA termina ANTES da energia (cedo)
        usadas += 1
        if usadas >= 80:
            break

    def q(v, p):
        return sorted(v)[int(p * (len(v) - 1))]
    print(f"palavras isoladas medidas: {usadas}")
    if usadas < 5:
        print("poucas palavras isoladas por silencio para calibrar")
        return 1
    print(f"INICIO  (energia comeca antes do FA em):  mediana {statistics.median(antes):+.3f}s  "
          f"p75 {q(antes,.75):+.3f}  p90 {q(antes,.90):+.3f}  max {max(antes):+.3f}")
    print(f"FIM     (energia termina depois do FA em): mediana {statistics.median(depois):+.3f}s  "
          f"p75 {q(depois,.75):+.3f}  p90 {q(depois,.90):+.3f}  max {max(depois):+.3f}")
    print()
    print("=> pad_antes  >= p90 do INICIO,  pad_depois >= p90 do FIM  (mais folga de sala no fim da peca)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
