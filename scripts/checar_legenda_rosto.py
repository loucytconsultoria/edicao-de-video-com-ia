# -*- coding: utf-8 -*-
"""PORTAO: antes de entregar qualquer video legendado, verificar se a LEGENDA
cai em cima do ROSTO — e, se cair, mover a legenda (para baixo ou para cima do
rosto). Caso de origem: video em que o rosto ficou baixo no enquadramento e a
tarja cobriu a boca e o queixo.

Como funciona:
  1. Detecta o rosto (YuNet, OpenCV) em frames amostrados do video inteiro
     (2 fps) e tira a CAIXA MEDIANA (video de apresentador e estavel).
  2. Faixa da legenda: --faixa Y0,Y1 em 1080x1920 (default 1300,1460: uma
     linha de legenda com Alignment 2 e MarginV ~480).
  3. Se a caixa do rosto invadir a faixa em mais de --limiar (default 25%)
     dos frames amostrados, recomenda um MarginV novo:
       - ABAIXO do rosto, se sobrar faixa livre entre o queixo e y=1635
         (limite vivo do corte 4:5 da grade e do rotulo de anuncio);
       - senao ACIMA do rosto, respeitando y>=320 (UI de topo do Instagram).
  4. --fix LEGENDA.ass reescreve o MarginV das linhas Style do arquivo
     para o valor recomendado.

Requer o modelo YuNet (models/README.md ou $VIDEO_IA_YUNET).

Uso:
  python checar_legenda_rosto.py VIDEO.mp4 [--faixa 1300,1460] [--ass legenda.ass] [--fix]
Saida: JSON em stdout com veredicto e marginv_recomendado (0 = manter).
"""
import argparse, json, os, re, subprocess, sys, tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from comum import modelo_yunet

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
W, H = 1080, 1920
FAIXA_PADRAO = (1300, 1460)     # tarja da legenda padrao (MarginV 480, 1 linha)
ALT_LINHA = 160                 # altura util de 1 linha de legenda com tarja
Y_MAX_VIVO = 1635               # limite inferior visivel (corte 4:5 do feed, UI de baixo)
Y_MIN_VIVO = 320                # UI de topo (avatar/@/audio) + folga
MARGEM_ROSTO = 40               # folga entre rosto e tarja


def rostos_no_video(video, fps_amostra=2.0):
    """Deteccao com YuNet (o OpenCV 5 nao traz mais os haarcascades). Deteccao
    de rosto so MEDE posicao: nada e criado nem alterado na imagem."""
    modelo = modelo_yunet()
    if modelo is None:
        sys.exit("modelo YuNet nao encontrado: ver models/README.md ou defina VIDEO_IA_YUNET")
    det = cv2.FaceDetectorYN_create(str(modelo), "", (320, 320), score_threshold=0.7)
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    passo = max(1, int(round(fps / fps_amostra)))
    caixas, total, i = [], 0, 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % passo == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            total += 1
            fy, fx = frame.shape[:2]
            # reduz para acelerar; caixas voltam normalizadas para 1080x1920
            alvo_w = 320
            alvo_h = max(1, round(fy * alvo_w / fx))
            det.setInputSize((alvo_w, alvo_h))
            peq = cv2.resize(frame, (alvo_w, alvo_h))
            _, dets = det.detect(peq)
            if dets is not None and len(dets):
                x, y, w, h = max(dets, key=lambda d: d[2] * d[3])[:4]
                caixas.append((x / alvo_w * W, y / alvo_h * H,
                               w / alvo_w * W, h / alvo_h * H))
        i += 1
    cap.release()
    return caixas, total


def veredicto(video, faixa=FAIXA_PADRAO, limiar=0.25):
    caixas, total = rostos_no_video(video)
    if not caixas:
        return {"rosto_detectado": False, "frames": total, "sobrepoe": False,
                "marginv_recomendado": 0}
    y0f, y1f = faixa
    sobre = 0
    for x, y, w, h in caixas:
        top, bot = y, y + h
        inter = max(0.0, min(bot, y1f) - max(top, y0f))
        if inter > 0.2 * h:
            sobre += 1
    frac = sobre / len(caixas)
    xs = np.array(caixas)
    med = {"x": float(np.median(xs[:, 0])), "y": float(np.median(xs[:, 1])),
           "w": float(np.median(xs[:, 2])), "h": float(np.median(xs[:, 3]))}
    out = {"rosto_detectado": True, "frames_com_rosto": len(caixas), "frames": total,
           "fracao_sobreposta": round(frac, 3), "rosto_mediano": {k: round(v) for k, v in med.items()},
           "sobrepoe": frac > limiar, "marginv_recomendado": 0}
    if frac > limiar:
        queixo = med["y"] + med["h"]
        testa = med["y"]
        # 1) abaixo do rosto, se couber na faixa viva
        if queixo + MARGEM_ROSTO + ALT_LINHA <= Y_MAX_VIVO:
            base_tarja = min(Y_MAX_VIVO, queixo + MARGEM_ROSTO + ALT_LINHA)
            out["marginv_recomendado"] = int(H - base_tarja)
            out["posicao"] = "abaixo do rosto"
        # 2) acima do rosto
        elif testa - MARGEM_ROSTO - ALT_LINHA >= Y_MIN_VIVO:
            base_tarja = testa - MARGEM_ROSTO
            out["marginv_recomendado"] = int(H - base_tarja)
            out["posicao"] = "acima do rosto"
        else:
            out["posicao"] = "SEM FAIXA LIVRE — decidir manualmente"
    return out


def aplica_fix(ass_path, marginv):
    txt = open(ass_path, encoding="utf-8-sig").read()
    def troca(m):
        campos = m.group(0).split(",")
        campos[-2] = str(marginv)          # MarginV e o penultimo campo do Style
        return ",".join(campos)
    novo, n = re.subn(r"^Style:.*$", troca, txt, flags=re.M)
    open(ass_path, "w", encoding="utf-8-sig").write(novo)
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--ass", default=None, help="legenda .ass (para --fix)")
    ap.add_argument("--fix", action="store_true",
                    help="reescreve o MarginV das Styles do .ass com o recomendado")
    ap.add_argument("--limiar", type=float, default=0.25)
    ap.add_argument("--faixa", default=None, help="Y0,Y1 da legenda em 1080x1920")
    a = ap.parse_args()
    faixa = tuple(int(x) for x in a.faixa.split(",")) if a.faixa else FAIXA_PADRAO
    v = veredicto(a.video, faixa=faixa, limiar=a.limiar)
    if a.fix and v.get("marginv_recomendado"):
        if not a.ass:
            sys.exit("--fix exige --ass")
        n = aplica_fix(a.ass, v["marginv_recomendado"])
        v["styles_ajustadas"] = n
    print(json.dumps(v, ensure_ascii=False, indent=1))
    sys.exit(2 if v.get("sobrepoe") and not v.get("styles_ajustadas") else 0)
