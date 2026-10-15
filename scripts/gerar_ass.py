# -*- coding: utf-8 -*-
"""Gera legendas queimáveis (.ass, libass) a partir das palavras do analise.json.

Estilos (--estilo):
  classica  frases inteiras embaixo (área segura), 1-2 linhas — sóbrio
  palavra   uma palavra por vez no centro, com "pop" — dinâmico, muito Reels
  blocos    2-4 palavras por bloco, caixa alta — estilo retenção/Hormozi
  karaoke   bloco fixo com a palavra corrente destacada em cor — acompanhamento

Uso:
  python gerar_ass.py PASTA_EDICAO [--estilo blocos] [--cutmap corte.json]
                      [--queimar SAIDA.mp4 --video VIDEO_CORTADO]
                      [--fonte "Arial"] [--cor-destaque F2B134]
                      [--calar-silencio 0.30] ...

--calar-silencio S: nenhuma palavra fica na tela durante uma pausa >= S
segundos (o bloco fecha 0,12 s depois da última palavra). Depois de gerar, o
script confere a COBERTURA: cada palavra tem exatamente um evento ativo no
instante em que é dita e nenhuma legenda fica acesa no silêncio; falha = exit 2.

--cutmap: use quando o analise.json é do vídeo ORIGINAL e as legendas são para
o vídeo CORTADO — as palavras são remapeadas pela linha do tempo do corte
(sem retranscrever). Sem --cutmap, os tempos são usados como estão.

O .ass usa PlayResX=1080/PlayResY=1920 e escala junto com qualquer resolução
9:16. Área segura das plataformas verticais: evitamos os ~220px do topo e
~560px da base (UI do aplicativo).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import (FONTES_RESERVA, RAIZ_REPO, achar_fonte, carregar_json, fail,
                   ffmpeg, pastas_de_fontes, probe, remapear_palavras)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PLAYX, PLAYY = 1080, 1920
# Área segura da UI dos aplicativos em 1080x1920: topo ~250px,
# base ~500px, direita ~120px, esquerda ~60px. Posições dentro da zona segura:
# baixo = an2 com MarginV acima da UI; centro = an5 fixado por \pos no miolo.
POSICOES = {"baixo": ("2", 520, None), "centro": ("5", 0, (540, 860)), "alto": ("8", 300, None)}
MARGEM_LATERAL = 90


def _t(seg: float) -> str:
    seg = max(0.0, seg)
    h = int(seg // 3600)
    m = int(seg % 3600 // 60)
    s = seg % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _cor(hex_rgb: str) -> str:
    """'FFD24A' (RGB) -> '&H004AD2FF' (AABBGGRR do ASS)."""
    from comum import cor_hex
    h = cor_hex(hex_rgb, "cor de destaque da legenda")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}"


def _limpar(w: str) -> str:
    # {, } e \ são sintaxe de override do ASS — texto do Whisper com eles
    # apagaria pedaços da legenda silenciosamente
    return w.strip().replace("{", "(").replace("}", ")").replace("\\", "/")


def _medidor(fonte_nome: str, tamanho: int):
    """Medidor de largura de texto em px usando Pillow com a MESMA fonte do Style,
    para dividir blocos que estourariam a área segura (o libass não avisa — só
    renderiza cortado). Margem extra de 8% porque o hinting do libass difere."""
    from PIL import ImageFont
    mapa = {"segoe ui black": "seguibl.ttf", "impact": "impact.ttf",
            "arial": "arialbd.ttf", "arial black": "ariblk.ttf",
            "bahnschrift": "bahnschrift.ttf", "dejavu sans": "DejaVuSans-Bold.ttf"}
    chave = fonte_nome.lower().replace(" ", "")
    candidatos = []
    for pasta in pastas_de_fontes()[:2]:          # $VIDEO_IA_FONTS e fonts/ do repo
        for f in pasta.glob("*.[to]tf"):
            if chave in f.stem.lower().replace("-", "").replace(" ", ""):
                candidatos.append(f)
    achada = achar_fonte(mapa.get(fonte_nome.lower(), ""), *FONTES_RESERVA)
    if achada:
        candidatos.append(achada)
    for c in candidatos:
        if c.exists():
            f = ImageFont.truetype(str(c), tamanho)
            return lambda txt: f.getlength(txt) * 1.08
    return lambda txt: len(txt) * tamanho * 0.62  # aproximação sem fonte


def agrupar(palavras: list[dict], max_palavras: int, max_chars: int,
            gap_quebra: float = 0.6, mede=None, max_px: float | None = None,
            caixa_alta: bool = False) -> list[list[dict]]:
    """Agrupa palavras em blocos de legenda: fecha no limite de palavras/chars,
    quando a largura medida estoura a área segura, em pausa perceptível ou em
    fim de frase (.!?)."""
    tx = (lambda s: s.upper()) if caixa_alta else (lambda s: s)
    blocos, atual, chars = [], [], 0

    def estoura(prox: str) -> bool:
        if mede is None or max_px is None:
            return False
        texto = tx(" ".join(_limpar(p["w"]) for p in atual) + " " + prox)
        return mede(texto) > max_px

    for p in palavras:
        w = _limpar(p["w"])
        if atual and (len(atual) >= max_palavras or chars + len(w) + 1 > max_chars
                      or estoura(w) or p["s"] - atual[-1]["e"] > gap_quebra):
            blocos.append(atual)
            atual, chars = [], 0
        atual.append(p)
        chars += len(w) + 1
        if re.search(r"[.!?…]$", w) and len(atual) >= 2:
            blocos.append(atual)
            atual, chars = [], 0
    if atual:
        blocos.append(atual)
    return blocos


PALAVRAS_VAZIAS = {"para", "pela", "pelo", "como", "essa", "esse", "esta", "este",
                   "aqui", "mais", "muito", "quando", "porque", "então", "entao",
                   "sobre", "ainda", "depois", "antes", "tudo", "cada", "eles", "elas"}


def palavra_chave(bloco: list[dict]) -> int | None:
    """Índice da palavra a destacar num bloco (estilo Hormozi): número > palavra
    longa não-vazia > nada. Destacar tudo = não destacar nada."""
    for i, p in enumerate(bloco):
        if re.search(r"\d", p["w"]):
            return i
    melhores = [(len(_limpar(p["w"])), i) for i, p in enumerate(bloco)
                if len(_limpar(p["w"]).strip(".,!?…")) >= 7
                and _limpar(p["w"]).lower().strip(".,!?…") not in PALAVRAS_VAZIAS]
    if melhores:
        return max(melhores)[1]
    return None


def _fim_evento(bloco: list[dict], prox_inicio: float | None, segurar: float, dur_video: float,
                calar: float | None = None) -> float:
    # sem palavra na tela no silêncio: se a pausa seguinte passa de `calar`,
    # o bloco sai 0,12 s depois da última palavra em vez de segurar
    if calar is not None:
        gap = None if prox_inicio is None else prox_inicio - bloco[-1]["e"]
        if gap is None or gap >= calar:
            segurar = min(segurar, 0.12)
    fim = bloco[-1]["e"] + segurar
    if prox_inicio is not None:
        fim = min(fim, prox_inicio - 0.02)
    fim = min(fim, dur_video)
    # piso mínimo para o evento existir — mas nunca invadindo o evento seguinte
    # (palavras grampeadas na mesma borda de corte empilhariam na tela)
    piso = bloco[-1]["e"] + 0.05
    if prox_inicio is not None:
        piso = min(piso, prox_inicio - 0.02)
    return max(fim, piso, bloco[0]["s"] + 0.04)


def gerar_eventos(palavras: list[dict], estilo: str, max_palavras: int, max_chars: int,
                  caixa_alta: bool, cor_destaque: str, dur_video: float,
                  pos_tag: str, mede, max_px: float, calar: float | None = None) -> list[str]:
    ev = []
    gq = 0.6 if calar is None else min(0.6, calar)   # pausa que quebra bloco
    tx = (lambda s: s.upper()) if caixa_alta else (lambda s: s)
    # pop com bounce: nasce a 60% e invisível, cresce a 105% opaco em 90ms, assenta
    pop = pos_tag + r"{\fscx60\fscy60\alpha&HFF&\t(0,90,\fscx105\fscy105\alpha&H00&)\t(90,150,\fscx100\fscy100)}"
    cor = _cor(cor_destaque)

    if estilo == "classica":
        blocos = agrupar(palavras, max_palavras=8, max_chars=max(max_chars, 34),
                         gap_quebra=gq, mede=None, max_px=None)  # classica pode quebrar em 2 linhas
        for i, b in enumerate(blocos):
            prox = blocos[i + 1][0]["s"] if i + 1 < len(blocos) else None
            texto = tx(" ".join(_limpar(p["w"]) for p in b))
            ev.append(f"Dialogue: 0,{_t(b[0]['s'])},{_t(_fim_evento(b, prox, 1.2, dur_video, calar))},Legenda,,0,0,0,,{pos_tag}{texto}")

    elif estilo == "palavra":
        for i, p in enumerate(palavras):
            prox = palavras[i + 1]["s"] if i + 1 < len(palavras) else None
            fim = _fim_evento([p], prox, 0.25, dur_video, calar)
            ev.append(f"Dialogue: 0,{_t(p['s'])},{_t(fim)},Legenda,,0,0,0,,{pop}{tx(_limpar(p['w']))}")

    elif estilo == "blocos":
        blocos = agrupar(palavras, max_palavras, max_chars, gap_quebra=gq, mede=mede,
                         max_px=max_px, caixa_alta=caixa_alta)
        for i, b in enumerate(blocos):
            prox = blocos[i + 1][0]["s"] if i + 1 < len(blocos) else None
            chave = palavra_chave(b)
            partes = []
            for k, q in enumerate(b):
                w = tx(_limpar(q["w"]))
                partes.append(rf"{{\c{cor}}}{w}{{\c&H00FFFFFF&}}" if k == chave else w)
            ev.append(f"Dialogue: 0,{_t(b[0]['s'])},{_t(_fim_evento(b, prox, 0.6, dur_video, calar))},Legenda,,0,0,0,,{pop}{' '.join(partes)}")

    elif estilo == "karaoke":
        blocos = agrupar(palavras, max_palavras, max_chars, gap_quebra=gq, mede=mede,
                         max_px=max_px, caixa_alta=caixa_alta)
        for i, b in enumerate(blocos):
            prox = blocos[i + 1][0]["s"] if i + 1 < len(blocos) else None
            fim_bloco = _fim_evento(b, prox, 0.6, dur_video, calar)
            for j, p in enumerate(b):
                ini = p["s"]
                fim = b[j + 1]["s"] if j + 1 < len(b) else fim_bloco
                partes = []
                for k, q in enumerate(b):
                    w = tx(_limpar(q["w"]))
                    partes.append(rf"{{\c{cor}}}{w}{{\c&H00FFFFFF&}}" if k == j else w)
                ev.append(f"Dialogue: 0,{_t(ini)},{_t(fim)},Legenda,,0,0,0,,{pos_tag}{' '.join(partes)}")
    else:
        fail(f"estilo desconhecido: {estilo}")
    return ev


def gerar_ass(palavras: list[dict], estilo: str, fonte: str, tamanho: int,
              posicao: str, max_palavras: int, max_chars: int, caixa_alta: bool,
              cor_destaque: str, dur_video: float, calar_silencio: float | None = None) -> str:
    an, margem_v, pos_xy = POSICOES[posicao]
    pos_tag = rf"{{\pos({pos_xy[0]},{pos_xy[1]})}}" if pos_xy else ""
    palavras = sorted(palavras, key=lambda p: p["s"])
    mede = _medidor(fonte, tamanho)
    max_px = PLAYX - 2 * MARGEM_LATERAL
    palavras = [p for p in palavras if _limpar(p["w"])]
    eventos = gerar_eventos(palavras, estilo, max_palavras, max_chars,
                            caixa_alta, cor_destaque, dur_video, pos_tag, mede, max_px,
                            calar_silencio)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {PLAYX}
PlayResY: {PLAYY}
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Legenda,{fonte},{tamanho},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,{an},{MARGEM_LATERAL},{MARGEM_LATERAL},{margem_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(eventos) + "\n"


_RE_DIALOGO = re.compile(r"^Dialogue:\s*\d+,([^,]+),([^,]+),")


def _seg_ass(t: str) -> float:
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def verificar_cobertura(ass_txt: str, palavras: list[dict], calar: float | None = None) -> dict:
    """PORTÃO da legenda, medido no próprio .ass gerado:
      - cada palavra falada tem EXATAMENTE um evento ativo no instante em que
        é dita (zero = palavra sem tela; dois = legenda empilhada/duplicada);
      - com `calar`, nenhuma legenda fica acesa no meio de uma pausa >= calar.
    Texto certo na legenda não prova nada sobre o tempo; isto mede o tempo."""
    evs = [(_seg_ass(m.group(1)), _seg_ass(m.group(2)))
           for m in map(_RE_DIALOGO.match, ass_txt.splitlines()) if m]
    pal = sorted((p for p in palavras if _limpar(p["w"])), key=lambda p: p["s"])

    def ativos(t):
        return sum(1 for a, b in evs if a <= t < b)

    sem_tela, empilhada, no_silencio = [], [], []
    for p in pal:
        t = (p["s"] + p["e"]) / 2
        n = ativos(t)
        if n == 0:
            sem_tela.append(f"{_limpar(p['w'])}@{t:.2f}s")
        elif n > 1:
            empilhada.append(f"{_limpar(p['w'])}@{t:.2f}s")
    if calar is not None:
        for a, b in zip(pal, pal[1:]):
            gap = b["s"] - a["e"]
            if gap >= max(calar, 0.30):
                t = (a["e"] + 0.12 + b["s"]) / 2
                if ativos(t):
                    no_silencio.append(f"{a['e']:.2f}-{b['s']:.2f}s")
    problemas = []
    if sem_tela:
        problemas.append(f"{len(sem_tela)} palavra(s) sem legenda: " + ", ".join(sem_tela[:6]))
    if empilhada:
        problemas.append(f"{len(empilhada)} palavra(s) com legenda empilhada: " + ", ".join(empilhada[:6]))
    if no_silencio:
        problemas.append(f"{len(no_silencio)} pausa(s) com legenda acesa: " + ", ".join(no_silencio[:6]))
    return {"palavras": len(pal), "eventos": len(evs), "sem_tela": sem_tela,
            "empilhada": empilhada, "tela_no_silencio": no_silencio, "problemas": problemas}


def opcao_fontsdir(cwd: str | Path | None = None) -> str:
    """Trecho ':fontsdir=...' para o filtro ass= quando há fontes em
    $VIDEO_IA_FONTS ou fonts/ do repo. O libass procura a família ali antes
    do sistema; sem a fonte, ele TROCA em silêncio (confira um frame).
    Caminho RELATIVO ao cwd do ffmpeg sempre que possível: os dois-pontos de
    um caminho absoluto do Windows dentro do filtergraph são fonte de bug."""
    import os
    for pasta in (os.environ.get("VIDEO_IA_FONTS"), RAIZ_REPO / "fonts"):
        if pasta and Path(pasta).is_dir() and any(Path(pasta).glob("*.[to]tf")):
            alvo = Path(pasta).resolve()
            try:
                p = Path(os.path.relpath(alvo, Path(cwd or ".").resolve())).as_posix()
            except ValueError:                       # outro drive no Windows
                p = alvo.as_posix().replace(":", "\\:")
            if " " in p or "'" in p:
                return ""                            # sem fontsdir é melhor que filtro quebrado
            return f":fontsdir={p}"
    return ""


def _t_srt(seg: float) -> str:
    seg = max(0.0, seg)
    h = int(seg // 3600)
    m = int(seg % 3600 // 60)
    s = int(seg % 60)
    ms = int(round((seg - int(seg)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def gerar_srt(palavras: list[dict], dur_video: float, max_chars: int = 38) -> str:
    """SRT por frase/bloco — para importar em editor externo (CapCut etc.)."""
    palavras = sorted(palavras, key=lambda p: p["s"])
    blocos = agrupar(palavras, max_palavras=9, max_chars=max_chars)
    linhas = []
    for i, b in enumerate(blocos):
        prox = blocos[i + 1][0]["s"] if i + 1 < len(blocos) else None
        fim = _fim_evento(b, prox, 1.0, dur_video)
        texto = " ".join(_limpar(p["w"]) for p in b)
        linhas += [str(i + 1), f"{_t_srt(b[0]['s'])} --> {_t_srt(fim)}", texto, ""]
    return "\n".join(linhas) + "\n"


def queimar(video: Path, ass: Path, saida: Path) -> None:
    """Queima o .ass no vídeo. Roda com cwd na pasta do .ass e caminho relativo
    no filtro para não escapar 'C:\\' dentro do filtergraph (Windows)."""
    filtro = f"ass={ass.name}{opcao_fontsdir(ass.parent)}"
    ffmpeg(["-i", str(video.resolve()), "-vf", filtro,
            "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(saida.resolve())],
           cwd=ass.parent)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta", help="pasta de edição contendo analise.json")
    ap.add_argument("--estilo", default="blocos", choices=["classica", "palavra", "blocos", "karaoke"])
    ap.add_argument("--cutmap", default=None, help="corte.json p/ remapear palavras à linha do tempo cortada")
    ap.add_argument("--fonte", default="Arial")
    ap.add_argument("--tamanho", type=int, default=None, help="default: 96 (blocos/palavra/karaoke), 64 (classica)")
    ap.add_argument("--posicao", default="centro", choices=list(POSICOES))
    ap.add_argument("--max-palavras", type=int, default=3)
    ap.add_argument("--max-chars", type=int, default=18)
    ap.add_argument("--caixa-alta", action="store_true", default=None)
    ap.add_argument("--sem-caixa-alta", dest="caixa_alta", action="store_false")
    ap.add_argument("--cor-destaque", default="F2B134", help="RGB hex p/ karaoke/blocos")
    ap.add_argument("--calar-silencio", type=float, default=None, metavar="S",
                    help="sem palavra na tela em pausa >= S segundos (ex.: 0.30)")
    ap.add_argument("--saida-ass", default=None)
    ap.add_argument("--srt", default=None, metavar="SAIDA.srt",
                    help="também exporta .srt por frase (p/ importar em editor externo)")
    ap.add_argument("--queimar", default=None, metavar="SAIDA.mp4")
    ap.add_argument("--video", default=None, help="vídeo alvo do --queimar (default: o do analise.json)")
    a = ap.parse_args()

    pasta = Path(a.pasta)
    analise = carregar_json(pasta / "analise.json")
    palavras = analise["palavras"]
    if not palavras:
        fail("analise.json sem palavras — nada para legendar.")

    if a.cutmap:
        plano = carregar_json(a.cutmap)
        palavras = remapear_palavras(palavras, plano["cutmap"])
        dur_video = plano["duracao_prevista"]
    else:
        dur_video = analise["meta"]["duracao"]

    caixa_alta = a.caixa_alta if a.caixa_alta is not None else (a.estilo in ("palavra", "blocos"))
    tamanho = a.tamanho or (64 if a.estilo == "classica" else 96)

    ass_txt = gerar_ass(palavras, a.estilo, a.fonte, tamanho, a.posicao,
                        a.max_palavras, a.max_chars, caixa_alta, a.cor_destaque, dur_video,
                        calar_silencio=a.calar_silencio)
    ass_path = Path(a.saida_ass) if a.saida_ass else pasta / f"legendas_{a.estilo}.ass"
    ass_path.write_text(ass_txt, encoding="utf-8-sig")
    n_ev = ass_txt.count("Dialogue:")
    print(f"OK: {ass_path} ({n_ev} eventos, estilo {a.estilo}, fonte {a.fonte})")
    cob = verificar_cobertura(ass_txt, palavras, a.calar_silencio)
    if cob["problemas"]:
        print("PORTÃO DA LEGENDA REPROVOU: " + " | ".join(cob["problemas"]))
        sys.exit(2)
    print(f"Cobertura: {cob['palavras']} palavras, cada uma com exatamente 1 evento ativo"
          + ("; nenhuma legenda acesa no silêncio" if a.calar_silencio is not None else ""))

    if a.srt:
        srt_path = Path(a.srt)
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(gerar_srt(palavras, dur_video), encoding="utf-8-sig")
        print(f"OK: {srt_path} (SRT por frase)")

    if a.queimar:
        video = Path(a.video) if a.video else Path(analise["video"])
        queimar(video, ass_path, Path(a.queimar))
        print(f"OK: legendas queimadas em {a.queimar}")


if __name__ == "__main__":
    main()
