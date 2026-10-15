# -*- coding: utf-8 -*-
"""Normaliza um vídeo já editado para a spec de entrega Reels e valida:
1080x1920@30 (fill/fit/blur), H.264 High, AAC 48kHz, loudnorm -14 LUFS 2-pass,
+faststart.

Uso:
  python exportar.py VIDEO [--saida OUT.mp4] [--modo fill|fit|blur]
                     [--sem-loudnorm] [--encoder auto|nvenc|x264]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import (ALVO, ENC_A, ENTREGA_NVENC, ENTREGA_X264, fail, ffmpeg,
                   loudnorm_aplicar, loudnorm_medir, nvenc_disponivel, probe)
from render_edl import _vf_916
from comum import QUALIDADES

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--saida", default=None)
    ap.add_argument("--modo", default="fill", choices=["fill", "fit", "blur"])
    ap.add_argument("--sem-loudnorm", action="store_true")
    ap.add_argument("--encoder", default="auto", choices=["auto", "nvenc", "x264"])
    a = ap.parse_args()

    video = Path(a.video)
    if not video.exists():
        fail(f"vídeo não encontrado: {video}")
    saida = Path(a.saida) if a.saida else video.with_name(f"{video.stem}_reels.mp4")

    usar_nvenc = a.encoder == "nvenc" or (a.encoder == "auto" and nvenc_disponivel())
    enc = ENTREGA_NVENC if usar_nvenc else ENTREGA_X264
    vf, precisa_fc = _vf_916(a.modo, QUALIDADES["final"], 1.0)

    temp = saida.with_name(saida.stem + "_prenorm.mp4")
    args = ["-i", str(video)]
    if precisa_fc:
        args += ["-filter_complex", vf, "-map", "[vout]", "-map", "0:a?"]
    else:
        args += ["-vf", vf]
    args += ["-r", str(ALVO["fps"]), *enc, *ENC_A, "-movflags", "+faststart", str(temp)]
    ffmpeg(args, timeout=3600)

    if a.sem_loudnorm:
        temp.replace(saida)
    else:
        medida = loudnorm_medir(temp)
        if medida is None:
            print("AVISO: medição loudnorm falhou — aplicando single-pass aproximado.")
        loudnorm_aplicar(temp, saida, medida)
        temp.unlink(missing_ok=True)

    real = probe(saida)
    problemas = []
    if not real["tem_audio"]:
        problemas.append("SEM áudio")
    if real.get("largura") != ALVO["largura"] or real.get("altura") != ALVO["altura"]:
        problemas.append(f"resolução {real.get('largura')}x{real.get('altura')}")
    status = "OK" if not problemas else "PROBLEMAS: " + "; ".join(problemas)
    print(f"Exportado [{status}]: {saida} ({real['duracao']:.2f}s, "
          f"{real.get('largura')}x{real.get('altura')}, "
          f"{'nvenc' if usar_nvenc else 'x264'})")
    if problemas:
        sys.exit(2)


if __name__ == "__main__":
    main()
