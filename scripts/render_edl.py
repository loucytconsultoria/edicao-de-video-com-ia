# -*- coding: utf-8 -*-
"""Renderiza um EDL (Edit Decision List) JSON em vídeo vertical final.
O EDL é a camada de decisão (escrita pelo agente ou por uma pessoa a partir do
roteiro e aprovada por um humano); este script é 100% mecânico e
re-executável — padrão plan -> aprovar -> render.

Uso:
  python render_edl.py EDL.json [--qualidade final|preview|draft] [--forcar]

Schema do EDL (campos opcionais podem faltar):
{
 "saida": {"arquivo": "corte01.mp4", "pasta_trabalho": "corte01_render"},
 "identidade": "config/identidade_exemplo.json",           // cores/fontes (opcional)
 "capa": {"png": "capa.png", "dur": 0.8},                    // prependada (opcional)
 "endcard": {"texto": "SAIBA MAIS", "subtitulo": "...", "dur": 2.0}, // ou {"mp4": "..."}
 "clips": [
   {"src": "bruto.mp4", "in": 12.4, "out": 45.6,             // src obrigatório; in/out default = vídeo inteiro
    "modo_916": "fill|fit|blur|rosto",                       // default fill; rosto = crop no rosto (YuNet)
    "punch": 1.0,                                            // zoom fixo do clipe (1.0 = sem)
    "cortar_silencio": true,                                 // aplica corte de pausas DENTRO do clipe
    "punch_alternado": true,                                 // alterna 1.0/1.07 nos sub-cortes p/ disfarçar jump cut
    "transicao": {"tipo": "xfade", "nome": "fade", "dur": 0.4}, // transição PARA o próximo clipe; default corte seco
    "analise": "pasta_edicao",                               // pasta com analise.json do src (p/ silêncio+legenda)
    "silencio_antes": 0.3                                    // respiro sintético (quadro parado) antes do clipe
   }
 ],
 "cortar_silencio": {"pausa_min": 0.6, "pad_antes": 0.10, "pad_depois": 0.18,
                     "pausa_inicio": 0.10, "pad_fim": 0.35}, // parâmetros globais
 "legendas": {"estilo": "blocos|karaoke|palavra|classica",   // omitir = sem legenda
              "fonte": "Arial", "posicao": "centro",         // default: legenda.* da identidade
              "cor_destaque": "F2B134", "max_palavras": 3,
              "calar_silencio": 0.30},                        // sem palavra na tela em pausa >= 0,30 s
 "overlays": [
   {"tipo": "logo", "png": "logo.png", "opacidade": 0.55, "pos": "baixo-direita"},
   {"tipo": "png", "png": "grafico.png", "de": 3.5, "ate": 8.0, "x": 90, "y": 300, "fade": 0.3},
   {"tipo": "faixa_texto", "nome": "Nome Sobrenome", "cargo": "Gerente de Treinamento",
    "de": 1.0, "ate": 6.0},
   {"tipo": "barra_progresso", "cor": "F2B134", "altura": 12}
 ],
 "trilha": {"arquivo": "musica.mp3", "volume": 0.25, "ducking": true},
 "loudnorm": true                                            // default true
}

Pipeline: extrai segmentos normalizados (1080x1920@30) -> funde pares com xfade
-> concat -> compõe overlays + trilha + LEGENDAS POR ÚLTIMO -> loudnorm 2-pass
-> valida com ffprobe e grava relatorio.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import (ALVO, ENC_A, QUALIDADES, ENTREGA_NVENC, ENTREGA_X264,
                   carregar_identidade, carregar_json, checar_path, fail, ffmpeg,
                   loudnorm_aplicar, loudnorm_medir, nvenc_disponivel, probe,
                   remapear_palavras, salvar_json)
from cortar_silencio import montar_cutmap

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).parent.parent


# ------------------------------------------------------------ segmentos -----

def _vf_916(modo: str, q: dict, punch: float, crop_x: int | None = None) -> tuple[str, bool]:
    """(filtro de vídeo, precisa_filter_complex). Normaliza para o alvo 9:16."""
    W, H = q["escala"]
    fps = q["fps"]
    punch_pre = ""
    if punch and punch > 1.001:
        z = punch
        punch_pre = (f"crop=w='trunc(iw/{z}/2)*2':h='trunc(ih/{z}/2)*2',")
    if modo == "fit":
        return (f"{punch_pre}scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p"), False
    if modo == "blur":
        fc = (f"[0:v]split=2[bgs][fgs];"
              f"[bgs]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];"
              f"[fgs]{punch_pre}scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[vout]")
        return fc, True
    if modo == "rosto" and crop_x is not None:
        return (f"crop='trunc(ih*9/16/2)*2':ih:{crop_x}:0,scale={W}:{H},"
                f"setsar=1,fps={fps},format=yuv420p"), False
    # fill (default): cobre o quadro, corta o excesso centralizado
    return (f"{punch_pre}scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps={fps},format=yuv420p"), False


def centro_rosto_x(video: str, largura: int, altura: int) -> int | None:
    """x do crop 9:16 centrado no rosto (mediana em ~12 frames). Usa o YuNet
    (rostos.py) quando o modelo existe; senão, o Haar cascade do OpenCV."""
    try:
        from rostos import crop_x_916
    except ImportError:
        print("AVISO: opencv-python não instalado — modo 'rosto' caiu para crop central. "
              "Instale com: pip install opencv-python", file=sys.stderr)
        return None
    return crop_x_916(video, largura, altura)


def extrair_segmento(src: Path, src_in: float, src_out: float, saida: Path,
                     q: dict, modo: str, punch: float, declick: float = 0.006,
                     crop_x: int | None = None, sil_antes: float = 0.0) -> None:
    # QUANTIZA a duracao na grade de frame. Sem isto o video sai com N frames
    # inteiros (piso) e o audio com a duracao exata: 21-81 ms de sobra por
    # segmento; no concat o video TRAVA esses ms em cada emenda (medido: 19
    # travadas numa peca de 29 segmentos) e a soma estoura o limiar de duracao.
    # Audio e video cortados no MESMO instante (multiplo de 1/fps).
    fps = float(q.get("fps", 30))
    dur = round(round((src_out - src_in) * fps) / fps, 6)
    if dur <= 0:
        dur = 1.0 / fps
    vf, precisa_fc = _vf_916(modo, q, punch, crop_x)
    af = (f"afade=t=in:st=0:d={declick},afade=t=out:st={max(0.0, dur - declick):.4f}:d={declick},"
          f"atrim=0:{dur:.6f},asetpts=PTS-STARTPTS,"
          f"aformat=sample_rates=48000:channel_layouts=stereo")
    # -ss ÚNICO antes do -i (keyframe seek + decode até o alvo = preciso e rápido)
    # + -t na saída. NUNCA combinar -ss de SAÍDA com -af: no ffmpeg 8.x isso
    # trunca o áudio em exatamente o valor do seek fino (bug verificado em teste).
    args = ["-ss", f"{src_in:.3f}", "-i", str(src), "-t", f"{dur:.6f}",
            "-frames:v", str(int(round(dur * fps)))]
    sem_audio = not probe(src)["tem_audio"]
    if sem_audio:
        # fonte muda: sintetiza silêncio p/ o concat não misturar segmentos
        # com e sem stream de áudio (corrompe o demuxer -c copy)
        args += ["-f", "lavfi", "-t", f"{dur:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
    mapa_a = "1:a" if sem_audio else "0:a"
    if precisa_fc:
        args += ["-filter_complex", vf, "-map", "[vout]", "-map", mapa_a]
    elif sem_audio:
        args += ["-vf", vf, "-map", "0:v", "-map", mapa_a]
    else:
        args += ["-vf", vf]
    args += ["-af", "anull" if sem_audio else af, *q["v"], "-pix_fmt", "yuv420p", *ENC_A,
             "-avoid_negative_ts", "make_zero", "-shortest", str(saida)]
    ffmpeg(args)
    if sil_antes and sil_antes > 0.02:
        _prefixar_silencio(saida, sil_antes, q, fps)


def _prefixar_silencio(seg, dur_sil, q, fps):
    """Insere `dur_sil` de RESPIRO no inicio do segmento, congelando o 1o frame.

    Existe porque nem sempre o bruto tem silencio para emprestar na emenda: se a
    pausa daquele ponto foi cortada junto com a regravacao, duas frases ficam
    coladas e a peca soa como "uma frase so" (reprovado na revisao humana).
    Aqui o respiro e CRIADO com a imagem parada — nao ha salto visual,
    so uma micropausa antes de a frase comecar.

    Metodo: extrai o 1o frame como PNG e monta um clipe com -loop 1. Tentar
    congelar com trim+loop no filtergraph produzia arquivo de 262 bytes SEM
    STREAM (o concat entao falhava com 'Output file does not contain any
    stream') — o PNG e determinístico e nao depende de filtro de tempo.
    """
    n_sil = max(1, int(round(dur_sil * fps)))
    dur_q = n_sil / fps
    png = seg.with_suffix(".frz.png")
    freeze = seg.with_suffix(".frz.mp4")
    tmp = seg.with_suffix(".sil.mp4")
    ffmpeg(["-i", str(seg), "-frames:v", "1", "-q:v", "2", str(png)])
    if not png.exists():
        return                                   # sem respiro e melhor que quebrar
    ffmpeg(["-loop", "1", "-t", "%.6f" % dur_q, "-i", str(png),
            "-f", "lavfi", "-t", "%.6f" % dur_q, "-i", "anullsrc=r=48000:cl=stereo",
            "-r", str(fps), "-frames:v", str(n_sil),
            *q["v"], "-pix_fmt", "yuv420p", *ENC_A, "-shortest", str(freeze)])
    if not freeze.exists() or probe(freeze)["duracao"] <= 0:
        png.unlink(missing_ok=True)
        return
    lista = seg.with_suffix(".lst.txt")
    lista.write_text("file '%s'\n" % freeze.name + "file '%s'\n" % seg.name,
                     encoding="utf-8")
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lista), "-c", "copy", str(tmp)])
    if tmp.exists() and probe(tmp)["duracao"] > 0:
        tmp.replace(seg)
    for x in (png, freeze, lista):
        x.unlink(missing_ok=True)

def imagem_para_clipe(png: Path, dur: float, saida: Path, q: dict, fade_out: bool = False) -> None:
    W, H = q["escala"]
    vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    if fade_out and dur > 0.6:
        vf += f",fade=t=in:st=0:d=0.25,fade=t=out:st={dur - 0.25:.2f}:d=0.25"
    ffmpeg(["-loop", "1", "-t", f"{dur:.3f}", "-i", str(png),
            "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf, "-r", str(q["fps"]), *q["v"], "-pix_fmt", "yuv420p", *ENC_A,
            "-shortest", str(saida)])


def fundir_xfade(a: Path, b: Path, saida: Path, nome: str, dur_t: float, q: dict) -> float:
    """Funde a+b com xfade/acrossfade; retorna a duração REAL do resultado."""
    dur_a = probe(a)["duracao"]
    offset = max(0.0, dur_a - dur_t)
    ffmpeg(["-i", str(a), "-i", str(b), "-filter_complex",
            f"[0:v][1:v]xfade=transition={nome}:duration={dur_t}:offset={offset:.3f}[v];"
            f"[0:a][1:a]acrossfade=d={dur_t}[a]",
            "-map", "[v]", "-map", "[a]", *q["v"], "-pix_fmt", "yuv420p", *ENC_A, str(saida)])
    return probe(saida)["duracao"]


# ------------------------------------------------------------- overlays -----

def gerar_faixa_texto(nome: str, cargo: str, est: dict, saida: Path) -> Path:
    """Lower third (faixa nome/cargo) em PNG com alpha, na identidade dada."""
    from PIL import Image, ImageDraw
    from capa import fonte
    W, H = 900, 200
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fundo = est["cores"]["fundo"].lstrip("#")
    r, g, b = int(fundo[0:2], 16), int(fundo[2:4], 16), int(fundo[4:6], 16)
    d.rounded_rectangle([0, 0, W, H], radius=18, fill=(r, g, b, 235))
    ac = est["cores"]["acento"].lstrip("#")
    d.rectangle([0, 0, 14, H], fill=(int(ac[0:2], 16), int(ac[2:4], 16), int(ac[4:6], 16), 255))
    f_nome = fonte(est, "titulo", 56)
    f_cargo = fonte(est, "corpo", 34)
    d.text((44, 34), nome, font=f_nome, fill=est["cores"]["texto"])
    d.text((44, 116), cargo, font=f_cargo, fill=est["cores"]["acento"])
    img.save(saida)
    return saida


def montar_filtro_composicao(edl: dict, base_dur: float, workdir: Path,
                             ass_path: Path | None, q: dict,
                             ident: dict | None = None) -> tuple[str, list[str], str]:
    """Monta o filter_complex da composição final.
    Retorna (script_de_filtro, inputs_extras, label_final_video)."""
    W, H = q["escala"]
    inputs, linhas = [], []
    atual = "[0:v]null[v0]"
    linhas.append(atual)
    v = "v0"
    n_in = 1  # input 0 é a base

    for i, ov in enumerate(edl.get("overlays", [])):
        tipo = ov["tipo"]
        if tipo == "logo":
            png = ov["png"]
            op = ov.get("opacidade", 0.55)
            margem = ov.get("margem", 40)
            pos = ov.get("pos", "baixo-direita")
            xy = {"baixo-direita": f"W-w-{margem}:H-h-{margem + 300}",
                  "topo-direita": f"W-w-{margem}:{margem + 220}",
                  "topo-esquerda": f"{margem}:{margem + 220}",
                  "baixo-esquerda": f"{margem}:H-h-{margem + 300}"}[pos]
            # -loop 1: sem isso a imagem é 1 frame só e o overlay some depois do início
            inputs += ["-loop", "1", "-i", str(png)]
            escala = ov.get("largura", 180)
            linhas.append(f"[{n_in}:v]scale={escala}:-1,format=rgba,colorchannelmixer=aa={op}[ov{i}]")
            linhas.append(f"[{v}][ov{i}]overlay={xy}[v{i + 1}]")
            v = f"v{i + 1}"
            n_in += 1
        elif tipo in ("png", "faixa_texto"):
            if tipo == "faixa_texto":
                png = workdir / f"faixa_{i}.png"
                gerar_faixa_texto(ov["nome"], ov.get("cargo", ""),
                                  ident or carregar_identidade(), png)
                x, y = ov.get("x", 60), ov.get("y", H - 620)
            else:
                png = Path(ov["png"])
                x, y = ov.get("x", 60), ov.get("y", 300)
            de, ate = ov.get("de", 0.0), ov.get("ate", base_dur)
            fade = ov.get("fade", 0.3)
            inputs += ["-loop", "1", "-i", str(png)]
            linhas.append(
                f"[{n_in}:v]format=rgba,fade=t=in:st={de}:d={fade}:alpha=1,"
                f"fade=t=out:st={max(de, ate - fade):.3f}:d={fade}:alpha=1[ov{i}]")
            linhas.append(f"[{v}][ov{i}]overlay={x}:{y}:enable='between(t,{de},{ate})'[v{i + 1}]")
            v = f"v{i + 1}"
            n_in += 1
        elif tipo == "barra_progresso":
            # drawbox no ffmpeg 8.x não aceita 't' na largura — a técnica robusta
            # é uma faixa de cor deslizando via overlay (x depende de t).
            from comum import cor_hex
            acento = ((ident or {}).get("cores", {}).get("acento") or "F2B134")
            cor = cor_hex(ov.get("cor", acento), "cor da barra_progresso")
            alt = ov.get("altura", 12)
            inputs += ["-f", "lavfi", "-i", f"color=c=0x{cor}@0.9:s={W}x{alt}"]
            linhas.append(f"[{n_in}:v]format=rgba[bar{i}]")
            linhas.append(f"[{v}][bar{i}]overlay=x='-{W}+{W}*t/{base_dur:.3f}':y=H-{alt}[v{i + 1}]")
            v = f"v{i + 1}"
            n_in += 1
        else:
            fail(f"overlay de tipo desconhecido: {tipo}")

    # legendas SEMPRE por último (overlay por cima de legenda é bug silencioso)
    if ass_path:
        from gerar_ass import opcao_fontsdir
        linhas.append(f"[{v}]ass={ass_path.name}{opcao_fontsdir(workdir)}[vfinal]")
        v = "vfinal"

    # áudio: trilha com ducking opcional
    trilha = edl.get("trilha")
    if trilha:
        vol = trilha.get("volume", 0.25)
        inputs += ["-stream_loop", "-1", "-i", str(trilha["arquivo"])]
        if trilha.get("ducking", True):
            linhas.append(f"[0:a]asplit=2[det][mix]")
            linhas.append(f"[{n_in}:a]atrim=0:{base_dur:.3f},asetpts=PTS-STARTPTS,"
                          f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol}[mus]")
            linhas.append("[mus][det]sidechaincompress=threshold=0.03:ratio=8:attack=50:release=500[duck]")
            linhas.append("[mix][duck]amix=inputs=2:duration=first:normalize=0[afinal]")
        else:
            linhas.append(f"[{n_in}:a]atrim=0:{base_dur:.3f},asetpts=PTS-STARTPTS,"
                          f"aformat=sample_rates=48000:channel_layouts=stereo,volume={vol}[mus]")
            linhas.append("[0:a][mus]amix=inputs=2:duration=first:normalize=0[afinal]")
        n_in += 1
        a = "afinal"
    else:
        linhas.append("[0:a]anull[afinal]")
        a = "afinal"

    return ";\n".join(linhas), inputs, v


# ---------------------------------------------------------------- render ----

def render(edl_path: Path, qualidade: str, forcar: bool) -> None:
    edl = carregar_json(edl_path)
    q = QUALIDADES[qualidade]
    saida_cfg = edl.get("saida", {})
    saida = Path(saida_cfg.get("arquivo") or edl_path.with_suffix(".mp4"))
    if not saida.is_absolute():
        saida = edl_path.parent / saida
    workdir = Path(saida_cfg.get("pasta_trabalho") or saida.with_suffix("")) \
        if Path(saida_cfg.get("pasta_trabalho", "")).is_absolute() \
        else edl_path.parent / (saida_cfg.get("pasta_trabalho") or (saida.stem + "_render"))
    workdir.mkdir(parents=True, exist_ok=True)
    checar_path(workdir / "segmentos" / "seg_000_000.mp4")
    if saida.exists() and not forcar:
        fail(f"{saida} já existe — use --forcar para sobrescrever.")

    # resolve caminhos relativos do EDL contra a pasta do próprio EDL
    def _abs(p: str) -> str:
        q = Path(p)
        return str(q if q.is_absolute() else (edl_path.parent / q).resolve())
    for ov in edl.get("overlays", []):
        if ov.get("png"):
            ov["png"] = _abs(ov["png"])
    if edl.get("trilha", {}).get("arquivo"):
        edl["trilha"]["arquivo"] = _abs(edl["trilha"]["arquivo"])
    ident = carregar_identidade(_abs(edl["identidade"]) if edl.get("identidade") else None)

    params_sil = {"pausa_min": 0.6, "pad_antes": 0.10, "pad_depois": 0.18,
                  "pausa_inicio": 0.10, "pad_fim": 0.35,
                  **edl.get("cortar_silencio", {})}

    # 1. expandir clips em segmentos atômicos (aplicando corte de silêncio)
    segdir = workdir / "segmentos"
    segdir.mkdir(exist_ok=True)
    atomicos = []  # {src, in, out, punch, modo, transicao_depois, palavras}
    for ci, clip in enumerate(edl["clips"]):
        src = Path(clip["src"])
        if not src.is_absolute():
            src = edl_path.parent / src
        if not src.exists():
            fail(f"clip {ci}: fonte não encontrada: {src}")
        meta = probe(src)
        c_in = float(clip.get("in", 0.0))
        c_out = float(clip.get("out", meta["duracao"]))
        c_out = min(c_out, meta["duracao"])
        if c_in >= c_out:
            fail(f"clip {ci} ({src.name}): in ({c_in}) >= out ({c_out})")
        modo = clip.get("modo_916", "fill")
        punch = float(clip.get("punch", 1.0))

        analise = None
        if clip.get("analise"):
            apath = Path(clip["analise"])
            if not apath.is_absolute():
                apath = edl_path.parent / apath
            aj = apath / "analise.json" if apath.is_dir() else apath
            if aj.exists():
                analise = carregar_json(aj)

        crop_x = None
        if modo == "rosto":
            cx = centro_rosto_x(str(src), meta["largura"], meta["altura"])
            if cx is None:
                modo = "fill"
            else:
                crop_x = cx

        if clip.get("cortar_silencio") and analise and analise.get("palavras"):
            # janela com folga no fim: palavra que começa antes de c_out termina
            # inteira (a garantia central da skill), grampeada na duração real
            recorte = [p for p in analise["palavras"]
                       if p["s"] >= c_in - 0.02 and p["s"] < c_out]
            dur_janela = min(meta["duracao"], c_out + 0.35)
            cutmap, _ = montar_cutmap(recorte, dur_janela, params_sil["pausa_min"],
                                      params_sil["pad_antes"], params_sil["pad_depois"],
                                      params_sil["pausa_inicio"], params_sil["pad_fim"])
            cutmap = [c for c in cutmap if c["src_out"] > c_in]
            if cutmap:
                cutmap[0]["src_in"] = max(cutmap[0]["src_in"], c_in)
        else:
            if clip.get("cortar_silencio"):
                print(f"AVISO: clip {ci} pediu cortar_silencio sem analise.json utilizável — "
                      f"mantendo o trecho inteiro.")
            cutmap = [{"src_in": c_in, "src_out": c_out}]

        for si, c in enumerate(cutmap):
            p = punch
            if clip.get("punch_alternado") and len(cutmap) > 1:
                p = 1.0 if si % 2 == 0 else max(punch, 1.07)
            atomicos.append({"src": src, "in": c["src_in"], "out": c["src_out"],
                             "modo": modo, "punch": p, "crop_x": crop_x,
                             "sil_antes": float(clip.get("silencio_antes", 0.0)) if si == 0 else 0.0,
                             "transicao": clip.get("transicao") if si == len(cutmap) - 1 else None,
                             "palavras": analise["palavras"] if analise else None,
                             "clip_idx": ci})

    if not atomicos:
        fail("EDL sem nenhum trecho a renderizar.")

    print(f"Render [{qualidade}]: {len(edl['clips'])} clips -> {len(atomicos)} segmentos")
    arquivos = []
    for i, seg in enumerate(atomicos):
        f = segdir / f"seg_{i:03d}.mp4"
        extrair_segmento(seg["src"], seg["in"], seg["out"], f, q, seg["modo"],
                         seg["punch"], crop_x=seg["crop_x"],
                         sil_antes=seg.get("sil_antes", 0.0))
        seg["arquivo"] = f
        seg["dur"] = probe(f)["duracao"]        # com o respiro inserido, se houver
        arquivos.append(f)

    # 2. fundir pares com xfade (transição vira um segmento único)
    unidades = []
    i = 0
    while i < len(atomicos):
        seg = atomicos[i]
        tr = seg["transicao"]
        if tr and tr.get("tipo") == "xfade" and i + 1 < len(atomicos):
            prox = atomicos[i + 1]
            fdur = float(tr.get("dur", 0.4))
            fus = segdir / f"xfade_{i:03d}.mp4"
            dur_real = fundir_xfade(seg["arquivo"], prox["arquivo"], fus,
                                    tr.get("nome", "fade"), fdur, q)
            unidades.append({"arquivo": fus, "dur": dur_real,
                             "partes": [seg, prox], "xfade": fdur})
            i += 2
        else:
            unidades.append({"arquivo": seg["arquivo"], "dur": seg["dur"],
                             "partes": [seg], "xfade": 0.0})
            i += 1

    # 3. capa e endcard
    offset_capa = 0.0
    pre, pos = [], []
    if edl.get("capa"):
        capa_cfg = edl["capa"]
        png = Path(capa_cfg["png"])
        if not png.is_absolute():
            png = edl_path.parent / png
        dur = float(capa_cfg.get("dur", 0.8))
        cclip = workdir / "capa_clip.mp4"
        imagem_para_clipe(png, dur, cclip, q)
        pre.append({"arquivo": cclip, "dur": dur})
        offset_capa = dur
    if edl.get("endcard"):
        ec = edl["endcard"]
        eclip = workdir / "endcard_clip.mp4"
        if ec.get("mp4"):
            src_ec = Path(ec["mp4"])
            if not src_ec.is_absolute():
                src_ec = edl_path.parent / src_ec
            extrair_segmento(src_ec, 0, probe(src_ec)["duracao"], eclip, q, "fit", 1.0)
        else:
            from capa import desenhar_endcard
            png = workdir / "endcard.png"
            desenhar_endcard(ident, ec["texto"], ec.get("subtitulo")).save(png)
            imagem_para_clipe(png, float(ec.get("dur", 2.0)), eclip, q, fade_out=True)
        pos.append({"arquivo": eclip, "dur": probe(eclip)["duracao"]})

    # 4. concat (demuxer, -c copy — tudo já normalizado com params idênticos)
    lista = pre + unidades + pos
    concat_txt = segdir / "concat.txt"
    # apóstrofo no caminho (Sant'Anna, D'Avila) fecharia a aspa do demuxer —
    # escapar no formato do concat: ' -> '\''
    def _q(p):
        return Path(p).resolve().as_posix().replace("'", "'\\''")
    concat_txt.write_text(
        "".join(f"file '{_q(u['arquivo'])}'\n" for u in lista), encoding="utf-8")
    base = workdir / "base.mp4"
    ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_txt), "-c", "copy", str(base)])
    base_dur = probe(base)["duracao"]

    # 5. legendas: remapeia palavras das fontes para a linha do tempo da saída
    ass_path = None
    leg = edl.get("legendas")
    if leg and leg.get("estilo"):
        palavras_out = []
        off = offset_capa
        for u in unidades:
            for pi, parte in enumerate(u["partes"]):
                if pi == 1:
                    off -= u["xfade"]  # no vídeo fundido, a parte B começa em dur_A - fade
                if parte["palavras"]:
                    cm = [{"src_in": parte["in"], "src_out": parte["out"]}]
                    palavras_out += remapear_palavras(parte["palavras"], cm, off)
                off += parte["dur"]
        if not palavras_out:
            # nenhuma fonte tinha analise.json: transcreve a base pronta
            print("Legendas: nenhuma análise das fontes — transcrevendo o corte final...")
            from analisar import analisar
            an = analisar(base, workdir / "base_edicao", leg.get("modelo", "small"), "auto",
                          leg.get("idioma", "pt"), 0.45, False)
            palavras_out = an["palavras"]
        if palavras_out:
            from gerar_ass import gerar_ass, verificar_cobertura
            padrao = ident.get("legenda", {})
            leg = {**padrao, **leg}
            ass_txt = gerar_ass(palavras_out, leg.get("estilo", "blocos"),
                                leg.get("fonte", "Arial"),
                                leg.get("tamanho") or (64 if leg.get("estilo") == "classica" else 96),
                                leg.get("posicao", "centro"),
                                leg.get("max_palavras", 3), leg.get("max_chars", 18),
                                leg.get("caixa_alta", leg.get("estilo") in ("palavra", "blocos")),
                                leg.get("cor_destaque", "F2B134"), base_dur,
                                calar_silencio=leg.get("calar_silencio"))
            ass_path = workdir / "legendas.ass"
            ass_path.write_text(ass_txt, encoding="utf-8-sig")
            salvar_json(workdir / "legenda_palavras.json", {"palavras": palavras_out})
            cob = verificar_cobertura(ass_txt, palavras_out, leg.get("calar_silencio"))
            if cob["problemas"]:
                print("AVISO legenda: " + "; ".join(cob["problemas"][:5]))
        else:
            print("AVISO: sem palavras para legendar — seguindo sem legenda.")

    # 6. composição final (overlays + trilha + legenda) num único passe
    precisa_compor = bool(edl.get("overlays") or edl.get("trilha") or ass_path)
    composto = workdir / "composto.mp4"
    encoder_final = ENTREGA_NVENC if (qualidade == "final" and nvenc_disponivel()) else \
        (ENTREGA_X264 if qualidade == "final" else q["v"] + ["-pix_fmt", "yuv420p"])
    if precisa_compor:
        script, inputs_extras, vlabel = montar_filtro_composicao(edl, base_dur, workdir, ass_path, q, ident)
        fscript = workdir / "filtro_composicao.txt"
        fscript.write_text(script, encoding="utf-8")
        # cwd = workdir para o ass= usar caminho relativo (sem escapar C:\ no Windows)
        # -t explícito: fontes infinitas (trilha em -stream_loop, color da barra)
        # não podem estender a saída nem atrasar o fechamento do encode.
        ffmpeg(["-i", str(base.resolve()), *inputs_extras,
                "-filter_complex_script", fscript.name,
                "-map", f"[{vlabel}]", "-map", "[afinal]",
                "-t", f"{base_dur:.3f}",
                *encoder_final, *ENC_A, "-movflags", "+faststart",
                str(composto.resolve())], cwd=workdir, timeout=3600)
    else:
        ffmpeg(["-i", str(base), *encoder_final, *ENC_A,
                "-movflags", "+faststart", str(composto)], timeout=3600)

    # 7. loudnorm two-pass (só reencoda áudio; vídeo copiado)
    if edl.get("loudnorm", True) and qualidade != "draft":
        medida = loudnorm_medir(composto)
        if medida is None:
            print("AVISO: medição loudnorm falhou — aplicando single-pass aproximado.")
        loudnorm_aplicar(composto, saida, medida)
    else:
        ffmpeg(["-i", str(composto), "-c", "copy", "-movflags", "+faststart", str(saida)])

    # 8. validação e relatório
    dur_prevista = offset_capa + sum(u["dur"] for u in unidades) + sum(p["dur"] for p in pos)
    real = probe(saida)
    problemas = []
    if abs(real["duracao"] - dur_prevista) > 0.5:
        problemas.append(f"duração divergente: prevista {dur_prevista:.2f}s, real {real['duracao']:.2f}s")
    if not real["tem_audio"]:
        problemas.append("saída SEM áudio")
    if not real["tem_video"]:
        problemas.append("saída SEM vídeo")
    W, H = q["escala"]
    if real.get("largura") != W or real.get("altura") != H:
        problemas.append(f"resolução {real.get('largura')}x{real.get('altura')} != {W}x{H}")
    relatorio = {"edl": str(edl_path), "saida": str(saida), "qualidade": qualidade,
                 "duracao_prevista": round(dur_prevista, 3),
                 "duracao_real": round(real["duracao"], 3),
                 "segmentos": len(atomicos), "problemas": problemas}
    salvar_json(workdir / "relatorio.json", relatorio)
    status = "OK" if not problemas else "COM PROBLEMAS: " + "; ".join(problemas)
    print(f"Render concluído [{status}]: {saida} ({real['duracao']:.2f}s, "
          f"{real.get('largura')}x{real.get('altura')}) | relatório: {workdir / 'relatorio.json'}")
    if problemas:
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("edl")
    ap.add_argument("--qualidade", default="final", choices=list(QUALIDADES))
    ap.add_argument("--forcar", action="store_true")
    a = ap.parse_args()
    render(Path(a.edl), a.qualidade, a.forcar)


if __name__ == "__main__":
    main()
