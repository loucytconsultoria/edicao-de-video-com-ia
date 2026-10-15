# -*- coding: utf-8 -*-
"""Infra compartilhada do motor: ffmpeg/ffprobe, caminhos, cutmap, presets de
qualidade, loudnorm, fontes, modelos e identidade visual. Regras (ver
docs/licoes.md, seção "Receitas ffmpeg"):

- ffmpeg sempre com lista de argumentos + -y -nostdin + stdin=DEVNULL + timeout
  (sem isso, lote trava em prompt de overwrite ou em hang de NVENC).
- Falha com NVENC cai automaticamente para libx264 antes de abortar.
- Filtros que referenciam arquivos (ass=, subtitles=) usam caminho RELATIVO com
  cwd na pasta do arquivo — evita escapar `C:\\` em filtergraph no Windows.
- Filtergraph longo vai para arquivo (-filter_complex_script) — limite de ~8k
  chars de linha de comando no Windows.
- Paths acima de ~240 chars falham cedo com mensagem clara (MAX_PATH).
- Toda captura de texto é utf-8 com errors=replace (console pt-BR é cp1252).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = os.environ.get("VIDEO_IA_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("VIDEO_IA_FFPROBE", "ffprobe")

# Raiz do repositório (scripts/ fica um nível abaixo). Fontes, modelos e
# configuração podem ser apontados para fora do repo por variável de ambiente.
RAIZ_REPO = Path(__file__).resolve().parent.parent

# console do Windows em cp1252 derruba lote longo no print de texto acentuado
for _s in (sys.stdout, sys.stderr):
    if getattr(_s, "encoding", "").lower() != "utf-8" and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# Alvo de entrega vertical (Reels/Shorts/TikTok): 1080x1920@30, H.264 High, AAC 48kHz.
ALVO = {"largura": 1080, "altura": 1920, "fps": 30, "sample_rate": 48000}

# Escada de qualidade: draft p/ conferir corte, preview p/
# revisão humana, final p/ entrega. Segmentos intermediários usam CRF baixo
# para não acumular perda em 2 gerações de encode.
QUALIDADES = {
    "draft":   {"escala": (540, 960),  "v": ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"], "fps": 24},
    "preview": {"escala": (1080, 1920), "v": ["-c:v", "libx264", "-preset", "fast", "-crf", "20"], "fps": 30},
    "final":   {"escala": (1080, 1920), "v": ["-c:v", "libx264", "-preset", "fast", "-crf", "16"], "fps": 30},
}
ENC_A = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

# Encoder de passes intermediários (transições, queima): NVENC quando houver;
# comum.ffmpeg() troca sozinho para libx264 se a GPU falhar.
VENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "18", "-b:v", "16M",
        "-maxrate", "24M", "-pix_fmt", "yuv420p", "-profile:v", "high"]

# Entrega final (a plataforma recomprime; acima de ~10 Mbps não se ganha nada)
ENTREGA_NVENC = ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19",
                 "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M",
                 "-profile:v", "high", "-pix_fmt", "yuv420p"]
ENTREGA_X264 = ["-c:v", "libx264", "-crf", "19", "-preset", "slow",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]


def fail(msg: str, code: int = 1):
    print(f"ERRO: {msg}", file=sys.stderr)
    sys.exit(code)


def checar_path(p: str | Path, limite: int = 240) -> Path:
    p = Path(p)
    if len(str(p.resolve())) > limite:
        fail(f"caminho com mais de {limite} caracteres (limite MAX_PATH do Windows) — "
             f"use uma pasta de trabalho curta: {p}")
    return p


def run(cmd: list[str], cwd: str | Path | None = None, timeout: int = 1800,
        check: bool = True) -> subprocess.CompletedProcess:
    """Roda um comando com timeout; em falha, mostra o stderr e aborta (check=True)."""
    try:
        p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        fail(f"comando excedeu {timeout}s (possível hang de GPU/NVENC): {' '.join(map(str, cmd))}")
    if check and p.returncode != 0:
        print(f"COMANDO FALHOU (exit {p.returncode}): {' '.join(str(c) for c in cmd)}", file=sys.stderr)
        print(p.stderr[-4000:], file=sys.stderr)
        sys.exit(1)
    return p


def ffmpeg(args: list[str], cwd: str | Path | None = None, timeout: int = 1800) -> subprocess.CompletedProcess:
    """ffmpeg com fallback automático NVENC -> libx264 (driver/sessões podem falhar)."""
    base = [FFMPEG, "-hide_banner", "-y", "-nostdin", "-loglevel", "error"]
    p = run(base + [str(a) for a in args], cwd=cwd, timeout=timeout, check=False)
    if p.returncode != 0 and any("nvenc" in str(a) for a in args):
        args_cpu = [str(a).replace("h264_nvenc", "libx264").replace("hevc_nvenc", "libx265")
                    for a in args]
        # remove opções exclusivas de NVENC que o x264 rejeita
        limpo, pular = [], False
        for i, a in enumerate(args_cpu):
            if pular:
                pular = False
                continue
            if a in ("-rc", "-cq", "-tune"):
                pular = True
                continue
            if a.startswith("p") and a[1:].isdigit() and limpo and limpo[-1] == "-preset":
                a = "medium"          # presets p1..p7 só existem no NVENC
            limpo.append(a)
        print("AVISO: NVENC falhou, refazendo com libx264...", file=sys.stderr)
        p = run(base + limpo, cwd=cwd, timeout=timeout, check=False)
    if p.returncode != 0:
        print(f"COMANDO FALHOU (exit {p.returncode}): ffmpeg {' '.join(str(a) for a in args)}",
              file=sys.stderr)
        print(p.stderr[-4000:], file=sys.stderr)
        sys.exit(1)
    return p


def probe(video: str | Path) -> dict:
    """Retorna duracao, fps, largura, altura, sample_rate, canais, tem_audio/video."""
    p = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(video)])
    data = json.loads(p.stdout)
    info = {"duracao": float(data["format"].get("duration", 0.0)),
            "tamanho_bytes": int(data["format"].get("size", 0)),
            "tem_audio": False, "tem_video": False}
    for st in data.get("streams", []):
        if st["codec_type"] == "video" and not info["tem_video"]:
            info["tem_video"] = True
            info["largura"] = st.get("width")
            info["altura"] = st.get("height")
            num, den = (st.get("r_frame_rate") or "30/1").split("/")
            info["fps"] = round(float(num) / float(den), 3) if float(den) else 30.0
            info["codec_video"] = st.get("codec_name")
            info["pix_fmt"] = st.get("pix_fmt")
        elif st["codec_type"] == "audio" and not info["tem_audio"]:
            info["tem_audio"] = True
            info["sample_rate"] = int(st.get("sample_rate", 48000))
            info["canais"] = st.get("channels", 2)
            info["codec_audio"] = st.get("codec_name")
    return info


def nvenc_disponivel() -> bool:
    """Testa de verdade (encode de 1 frame), não só a presença na lista."""
    p = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
             "-c:v", "h264_nvenc", "-f", "null", "-"], check=False, timeout=60)
    return p.returncode == 0


# -------------------------------------------------------------- loudnorm ----

def loudnorm_medir(video: str | Path, I: float = -14, TP: float = -1, LRA: float = 11) -> dict | None:
    """Passo 1 do loudnorm two-pass: mede e retorna o JSON, ou None se falhar."""
    p = run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(video),
             "-af", f"loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json",
             "-vn", "-f", "null", "-"], check=False)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.DOTALL)
    if not m:
        return None
    try:
        medida = json.loads(m.group(0))
        # áudio 100% silencioso mede -inf — vira filtro inválido no passo 2
        import math
        for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
            if not math.isfinite(float(medida[k])):
                return None
        return medida
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def loudnorm_aplicar(video: str | Path, saida: str | Path, medida: dict | None,
                     I: float = -14, TP: float = -1, LRA: float = 11) -> None:
    """Passo 2: aplica ganho linear medido (vídeo copiado, só o áudio reencoda).
    Sem medida (passo 1 falhou), aplica single-pass aproximado em vez de travar."""
    if medida:
        af = (f"loudnorm=I={I}:TP={TP}:LRA={LRA}"
              f":measured_I={medida['input_i']}:measured_TP={medida['input_tp']}"
              f":measured_LRA={medida['input_lra']}:measured_thresh={medida['input_thresh']}"
              f":offset={medida['target_offset']}:linear=true")
    else:
        af = f"loudnorm=I={I}:TP={TP}:LRA={LRA}"
    ffmpeg(["-i", str(video), "-c:v", "copy", "-af", af, *ENC_A,
            "-movflags", "+faststart", str(saida)])


# ---------------------------------------------------------------- json ------

def carregar_json(path: str | Path) -> dict:
    # utf-8-sig: aceita JSON com e sem BOM (PowerShell 5.1 e Notepad gravam BOM)
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def salvar_json(path: str | Path, data) -> None:
    """Gravação atômica (tmp + replace): queda no meio da escrita não pode
    deixar um manifest/cache truncado que derrube a retomada do lote."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def cor_hex(s: str, contexto: str = "cor") -> str:
    """Normaliza '#C9A87C'/'C9A87C' -> 'C9A87C'; falha claro em formato inválido."""
    h = str(s).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        fail(f"{contexto} inválida: {s!r} (esperado 6 dígitos hex, ex. C9A87C)")
    return h.upper()


# ---------------------------------------------------------------- cutmap ----
# Um cutmap descreve os trechos da fonte mantidos na saída:
# [{"src_in": 2.0, "src_out": 15.3}, ...] na ordem de saída. Com ele se converte
# um instante da fonte para o instante na saída sem retranscrever o corte.

def duracao_cutmap(cutmap: list[dict]) -> float:
    return sum(c["src_out"] - c["src_in"] for c in cutmap)


def remapear_palavras(palavras: list[dict], cutmap: list[dict],
                      offset_dst: float = 0.0) -> list[dict]:
    """Converte palavras {'w','s','e'} da fonte para a linha do tempo da saída.
    Palavra parcialmente fora é grampeada na borda; totalmente fora, cai.
    offset_dst desloca tudo (ex.: capa prependada antes do conteúdo)."""
    out = []
    for p in palavras:
        off = offset_dst
        for c in cutmap:
            dur = c["src_out"] - c["src_in"]
            ini, fim = max(p["s"], c["src_in"]), min(p["e"], c["src_out"])
            if fim - ini > 0.02:
                out.append({**p, "s": round(off + (ini - c["src_in"]), 3),
                            "e": round(off + (fim - c["src_in"]), 3)})
                break
            off += dur
    return out


# ------------------------------------------------- fontes, modelos, config --

def pastas_de_fontes() -> list[Path]:
    """Onde procurar .ttf/.otf: $VIDEO_IA_FONTS, fonts/ do repo e as pastas de
    fontes do sistema (Windows, Linux, macOS). Fontes não vêm no repositório."""
    pastas = []
    if os.environ.get("VIDEO_IA_FONTS"):
        pastas.append(Path(os.environ["VIDEO_IA_FONTS"]))
    pastas.append(RAIZ_REPO / "fonts")
    if os.environ.get("WINDIR"):
        pastas.append(Path(os.environ["WINDIR"]) / "Fonts")
    pastas += [Path.home() / ".fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
               Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    return [p for p in pastas if p.exists()]


def achar_fonte(*arquivos: str) -> Path | None:
    """Primeiro arquivo de fonte encontrado entre os nomes pedidos (busca
    recursiva nas pastas de fontes). Devolve None se nenhum existir."""
    for nome in arquivos:
        if not nome:
            continue
        direto = Path(nome)
        if direto.is_absolute() and direto.exists():
            return direto
        for pasta in pastas_de_fontes():
            alvo = pasta / nome
            if alvo.exists():
                return alvo
            achados = list(pasta.rglob(nome)) if pasta.is_dir() else []
            if achados:
                return achados[0]
    return None


# fontes de sistema de reserva, da mais "grossa" para a mais comum
FONTES_RESERVA = ("seguibl.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")


def modelo_yunet() -> Path | None:
    """Caminho do detector de rosto YuNet (ONNX). Não vem no repositório:
    ver models/README.md. Procura $VIDEO_IA_YUNET e depois models/."""
    cands = []
    if os.environ.get("VIDEO_IA_YUNET"):
        cands.append(Path(os.environ["VIDEO_IA_YUNET"]))
    cands += sorted((RAIZ_REPO / "models").glob("face_detection_yunet*.onnx"))
    for c in cands:
        if c.exists():
            return c
    return None


def carregar_identidade(caminho: str | Path | None = None) -> dict:
    """Identidade visual (cores, fontes, textos de marca) em JSON. Ordem:
    argumento, $VIDEO_IA_IDENTIDADE, config/identidade_exemplo.json."""
    p = caminho or os.environ.get("VIDEO_IA_IDENTIDADE") or (RAIZ_REPO / "config" / "identidade_exemplo.json")
    p = Path(p)
    if not p.exists():
        fail(f"identidade não encontrada: {p}")
    return carregar_json(p)


def wav_temporario(prefixo: str = "videoia") -> str:
    """Caminho único de WAV temporário (um por processo e chamada)."""
    fd, caminho = tempfile.mkstemp(prefix=f"{prefixo}_", suffix=".wav")
    os.close(fd)
    return caminho


def duracao(video: str | Path) -> float:
    return probe(video)["duracao"]
