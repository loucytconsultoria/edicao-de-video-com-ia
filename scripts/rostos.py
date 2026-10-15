# -*- coding: utf-8 -*-
"""Detecção de rosto para ENQUADRAR: onde está o rosto principal e onde ficam
os olhos, medido em quadros amostrados do vídeo inteiro.

Detector: YuNet (OpenCV, ONNX). O modelo NÃO vem no repositório; ver
models/README.md (variável VIDEO_IA_YUNET ou arquivo em models/). Sem o modelo,
cai no Haar cascade que acompanha o opencv-python (menos preciso).

Três lições embutidas (ver docs/licoes.md, "Enquadramento"):
  1. A âncora é a LINHA DOS OLHOS, não a caixa: a caixa do detector vai da
     testa ao queixo e o que sobra acima dela muda com a pessoa e o ângulo; os
     olhos são marcos e não dependem do fundo.
  2. Falsos positivos se filtram por PERSISTÊNCIA e TAMANHO: pôster na parede
     volta como rosto pequeno em todo quadro; mão e tronco voltam grandes e
     esporádicos. Fica a trilha mais persistente; detecção com menos de 40% da
     altura dela cai.
  3. Mede-se a janela inteira, não três quadros: a pessoa se move.

Detecção de rosto aqui só MEDE posição para corte e layout: nada é gerado nem
alterado na imagem.

Uso:
  python rostos.py VIDEO [--quadros 30] [--json saida.json]
Saída: caixa mediana, linha dos olhos (fração da altura), centro x e o x do
crop 9:16 sugerido.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import modelo_yunet

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _detector(W: int, H: int):
    import cv2
    m = modelo_yunet()
    if m is not None and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(m), "", (W, H), 0.6, 0.3, 5000)
        return "yunet", det
    haar = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if haar and hasattr(cv2, "CascadeClassifier"):       # o OpenCV 5 nao traz mais o Haar
        return "haar", cv2.CascadeClassifier(haar + "haarcascade_frontalface_default.xml")
    return None, None


def detectar(video: str, quadros: int = 30) -> dict:
    """Detecções por quadro amostrado: [(x, y, w, h, olhos_y | None, score)]."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(video))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    N = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    tipo, det = _detector(W, H)
    if det is None:
        cap.release()
        return {"detector": None, "largura": W, "altura": H, "amostras": []}
    amostras = []
    for i in np.linspace(N * 0.06, N * 0.94, quadros).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, fr = cap.read()
        if not ok:
            continue
        achados = []
        if tipo == "yunet":
            _, fs = det.detect(fr)
            for f in (fs if fs is not None else []):
                x, y, w, h = (float(v) for v in f[:4])
                olhos = (float(f[5]) + float(f[7])) / 2.0      # marcos 4..7: os dois olhos
                achados.append((x, y, w, h, olhos, float(f[-1])))
        else:
            cinza = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            for (x, y, w, h) in det.detectMultiScale(cinza, 1.1, 5):
                achados.append((float(x), float(y), float(w), float(h), None, 1.0))
        amostras.append(achados)
    cap.release()
    return {"detector": tipo, "largura": W, "altura": H, "amostras": amostras}


def rosto_principal(d: dict) -> dict | None:
    """Trilha mais persistente (agrupada por centro x), filtrada por tamanho."""
    W, H = d["largura"], d["altura"]
    todos = [a for quadro in d["amostras"] for a in quadro]
    if not todos:
        return None
    # agrupa por centro x em faixas de 10% da largura
    trilhas: dict[int, list] = {}
    for a in todos:
        cx = (a[0] + a[2] / 2) / max(1, W)
        trilhas.setdefault(int(cx * 10), []).append(a)
    principal = max(trilhas.values(), key=len)
    alt_ref = statistics.median(a[3] for a in principal)
    principal = [a for a in principal if a[3] >= 0.40 * alt_ref]
    if not principal:
        return None
    med = lambda k: statistics.median(a[k] for a in principal)
    olhos = [a[4] for a in principal if a[4] is not None]
    cx = med(0) + med(2) / 2
    return {"x": round(med(0)), "y": round(med(1)), "w": round(med(2)), "h": round(med(3)),
            "centro_x": round(cx), "olhos_y_frac": round(statistics.median(olhos) / H, 4) if olhos else None,
            "deteccoes": len(principal), "quadros": len(d["amostras"]),
            "descartadas": len(todos) - len(principal)}


def crop_x_916(video: str, largura: int, altura: int, quadros: int = 12) -> int | None:
    """x do crop 9:16 (altura cheia) centrado no rosto principal."""
    try:
        r = rosto_principal(detectar(video, quadros))
    except Exception as e:                       # opencv ausente ou vídeo ilegível
        print(f"AVISO: detecção de rosto falhou ({type(e).__name__}): {e}", file=sys.stderr)
        return None
    if not r:
        return None
    crop_w = int(round(altura * 9 / 16 / 2)) * 2
    return int(max(0, min(r["centro_x"] - crop_w / 2, largura - crop_w)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--quadros", type=int, default=30)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    d = detectar(a.video, a.quadros)
    r = rosto_principal(d)
    out = {"video": a.video, "detector": d["detector"], "largura": d["largura"],
           "altura": d["altura"], "rosto": r}
    if r:
        crop_w = int(round(d["altura"] * 9 / 16 / 2)) * 2
        out["crop_916_x"] = int(max(0, min(r["centro_x"] - crop_w / 2, d["largura"] - crop_w)))
    txt = json.dumps(out, ensure_ascii=False, indent=1)
    print(txt)
    if a.json:
        Path(a.json).write_text(txt, encoding="utf-8")
    return 0 if r else 2


if __name__ == "__main__":
    sys.exit(main())
