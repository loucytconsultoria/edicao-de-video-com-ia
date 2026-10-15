# -*- coding: utf-8 -*-
"""Mede o fator PIL -> libass para uma fonte num corpo dado: renderiza um .ass com a
palavra numa tela preta (1080x1920), mede a caixa dos pixels claros e compara com
o PIL. Sai: largura_libass/largura_pil e altura_caixa_alta_libass (px).
Uso: python calibrar_libass.py FONTE.ttf "Nome da família" CORPO [TEXTO]"""
import os, subprocess, sys, tempfile, shutil
from PIL import Image, ImageFont
import numpy as np
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def medir(ttf, familia, corpo, texto="HOMEM", bold=0):
    pasta = tempfile.mkdtemp()
    fontsdir = os.path.join(pasta, "fonts"); os.makedirs(fontsdir)
    shutil.copy2(ttf, fontsdir)
    ass = os.path.join(pasta, "t.ass")
    with open(ass, "w", encoding="utf-8-sig") as f:
        f.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,{familia},{corpo},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,T,,0,0,0,,{{\\pos(540,960)}}{texto}
""")
    png = os.path.join(pasta, "f.png")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=1",
           "-vf", f"ass=t.ass:fontsdir=fonts", "-frames:v", "1", "-ss", "0.5", png]
    subprocess.run(cmd, cwd=pasta, check=True)
    a = np.asarray(Image.open(png).convert("L"))
    ys, xs = np.where(a > 128)
    lw, lh = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    f = ImageFont.truetype(ttf, corpo)
    bb = f.getbbox(texto)
    pw, ph = bb[2] - bb[0], bb[3] - bb[1]
    shutil.rmtree(pasta, ignore_errors=True)
    return {"fonte": os.path.basename(ttf), "familia": familia, "corpo": corpo, "texto": texto,
            "libass_w": int(lw), "libass_h": int(lh), "pil_w": int(pw), "pil_h": int(ph),
            "fator_w": round(lw / pw, 4), "fator_h": round(lh / ph, 4)}


if __name__ == "__main__":
    ttf, fam, corpo = sys.argv[1], sys.argv[2], int(sys.argv[3])
    texto = sys.argv[4] if len(sys.argv) > 4 else "HOMEM"
    print(medir(ttf, fam, corpo, texto))
