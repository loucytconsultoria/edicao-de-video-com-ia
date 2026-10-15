# -*- coding: utf-8 -*-
"""Gera capa (cover 1080x1920) e end card (CTA final) com Pillow, na
identidade visual definida num JSON (default: config/identidade_exemplo.json,
ou $VIDEO_IA_IDENTIDADE).

Uso:
  python capa.py capa --titulo "TÍTULO DA CAPA" [--subtitulo "..."]
                 [--identidade minha_marca.json] [--fundo-video VIDEO --t 1.5]
                 [--saida capa.png] [--como-video 0.8]
  python capa.py endcard --texto "SAIBA MAIS" [--subtitulo "..."]
                 [--dur 2.0] [--saida endcard.mp4]

- capa: com --fundo-video usa um frame do vídeo escurecido como fundo; sem,
  fundo chapado na cor do estilo. --como-video também gera .mp4 mudo da capa
  (para prepender ao Reel). O .png serve como thumbnail de upload.
- endcard: clipe curto de encerramento com fade, pronto para concatenar.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from comum import FONTES_RESERVA, achar_fonte, carregar_identidade, fail, ffmpeg

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

W, H = 1080, 1920


def fonte(estilo: dict, papel: str, tamanho: int) -> ImageFont.FreeTypeFont:
    """Fonte do papel ('titulo', 'corpo') da identidade; sem o arquivo, cai nas
    fontes de reserva do sistema e, por último, na fonte embutida do Pillow."""
    cfg = estilo.get("fontes", {}).get(papel, {})
    achada = achar_fonte(cfg.get("arquivo", ""), cfg.get("reserva", ""), *FONTES_RESERVA)
    if achada:
        return ImageFont.truetype(str(achada), tamanho)
    return ImageFont.load_default(tamanho)


def quebrar(draw: ImageDraw.ImageDraw, texto: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    linhas, atual = [], ""
    for w in texto.split():
        teste = f"{atual} {w}".strip()
        if draw.textlength(teste, font=f) <= max_w or not atual:
            atual = teste
        else:
            linhas.append(atual)
            atual = w
    if atual:
        linhas.append(atual)
    return linhas


def frame_de_video(video: Path, t: float, destino: Path) -> Image.Image:
    ffmpeg(["-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(destino)])
    img = Image.open(destino).convert("RGB")
    # cobre 1080x1920 (crop central após escala)
    esc = max(W / img.width, H / img.height)
    img = img.resize((round(img.width * esc), round(img.height * esc)))
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def desenhar_capa(estilo: dict, titulo: str, subtitulo: str | None,
                  fundo: Image.Image | None) -> Image.Image:
    cores = estilo["cores"]
    if fundo is not None:
        img = fundo
        veu = Image.new("RGB", (W, H), cores["fundo"])
        img = Image.blend(img, veu, 0.55)  # escurece na cor do estilo
    else:
        img = Image.new("RGB", (W, H), cores["fundo"])
    d = ImageDraw.Draw(img)

    # título central-baixo (área segura), com barra de acento acima
    f_tit = fonte(estilo, "titulo", 104)
    linhas = quebrar(d, titulo, f_tit, W - 180)
    if len(linhas) > 4:  # título longo: reduz
        f_tit = fonte(estilo, "titulo", 84)
        linhas = quebrar(d, titulo, f_tit, W - 180)
    alt_linha = int(f_tit.size * 1.18)
    bloco_h = len(linhas) * alt_linha
    y = H - 620 - bloco_h  # âncora: acima da área da UI, terço inferior
    d.rectangle([90, y - 46, 90 + 140, y - 30], fill=cores["acento"])
    for ln in linhas:
        d.text((90, y), ln, font=f_tit, fill=cores["texto"])
        y += alt_linha
    if subtitulo:
        f_sub = fonte(estilo, "corpo", 44)
        for ln in quebrar(d, subtitulo, f_sub, W - 180):
            d.text((90, y + 12), ln, font=f_sub, fill=cores["acento"])
            y += int(f_sub.size * 1.3)
    return img


def desenhar_endcard(estilo: dict, texto: str, subtitulo: str | None) -> Image.Image:
    cores = estilo["cores"]
    img = Image.new("RGB", (W, H), cores["fundo"])
    d = ImageDraw.Draw(img)
    f_tit = fonte(estilo, "titulo", 96)
    linhas = quebrar(d, texto, f_tit, W - 200)
    alt = int(f_tit.size * 1.2)
    bloco = len(linhas) * alt
    y = (H - bloco) // 2 - 60
    barra_w = 140
    d.rectangle([(W - barra_w) // 2, y - 56, (W + barra_w) // 2, y - 40], fill=cores["acento"])
    for ln in linhas:
        lw = d.textlength(ln, font=f_tit)
        d.text(((W - lw) // 2, y), ln, font=f_tit, fill=cores["texto"])
        y += alt
    if subtitulo:
        f_sub = fonte(estilo, "corpo", 46)
        lw = d.textlength(subtitulo, font=f_sub)
        d.text(((W - lw) // 2, y + 24), subtitulo, font=f_sub, fill=cores["acento"])
    return img


def imagem_para_clipe(img_path: Path, dur: float, saida: Path, fade: bool = True) -> None:
    vf = f"scale={W}:{H},format=yuv420p"
    if fade and dur > 0.5:
        vf += f",fade=t=in:st=0:d=0.25,fade=t=out:st={dur - 0.25:.2f}:d=0.25"
    ffmpeg(["-loop", "1", "-t", f"{dur:.3f}", "-i", str(img_path),
            "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf, "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "16",
            "-c:a", "aac", "-b:a", "192k", "-shortest", str(saida)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tipo", choices=["capa", "endcard"])
    ap.add_argument("--titulo", default=None)
    ap.add_argument("--texto", default=None, help="texto principal do endcard")
    ap.add_argument("--subtitulo", default=None)
    ap.add_argument("--identidade", default=None, help="JSON de identidade visual")
    ap.add_argument("--fundo-video", default=None)
    ap.add_argument("--t", type=float, default=1.0, help="instante do frame de fundo")
    ap.add_argument("--dur", type=float, default=2.0, help="duração do endcard/capa-vídeo")
    ap.add_argument("--como-video", type=float, default=None, metavar="DUR",
                    help="também gera .mp4 mudo da capa com essa duração")
    ap.add_argument("--saida", default=None)
    a = ap.parse_args()

    estilo = carregar_identidade(a.identidade)

    if a.tipo == "capa":
        if not a.titulo:
            fail("capa exige --titulo")
        fundo = None
        saida = Path(a.saida) if a.saida else Path("capa.png")
        saida.parent.mkdir(parents=True, exist_ok=True)
        if a.fundo_video:
            fundo = frame_de_video(Path(a.fundo_video), a.t, saida.with_suffix(".frame.png"))
        img = desenhar_capa(estilo, a.titulo, a.subtitulo, fundo)
        img.save(saida)
        print(f"OK: {saida}")
        if a.como_video:
            clip = saida.with_suffix(".mp4")
            imagem_para_clipe(saida, a.como_video, clip, fade=False)
            print(f"OK: {clip} ({a.como_video}s)")
    else:
        if not a.texto:
            fail("endcard exige --texto")
        saida = Path(a.saida) if a.saida else Path("endcard.mp4")
        saida.parent.mkdir(parents=True, exist_ok=True)
        png = saida.with_suffix(".png")
        desenhar_endcard(estilo, a.texto, a.subtitulo).save(png)
        imagem_para_clipe(png, a.dur, saida)
        print(f"OK: {saida} ({a.dur}s) + {png}")


if __name__ == "__main__":
    main()
