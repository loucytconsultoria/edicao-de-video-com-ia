# -*- coding: utf-8 -*-
"""Processa uma PASTA de vídeos brutos em lote: analisa (transcrição), gera EDL
por vídeo a partir de instruções, e renderiza — com manifest por vídeo e
retomada idempotente (falha no vídeo 7 de 20 não reprocessa os 6 anteriores).

Uso:
  python lote.py PASTA [--instrucoes instrucoes.json] [--saida DIR]
                 [--qualidade preview] [--etapas analisar,edl,render] [--force]

instrucoes.json (todas as chaves opcionais):
{
 "padrao": {                       // aplicado a todos os vídeos
   "cortar_silencio": true,
   "legendas": {"estilo": "karaoke", "calar_silencio": 0.30},
   "identidade": "config/identidade_exemplo.json",
   "endcard": {"texto": "SAIBA MAIS", "dur": 2.0},
   "overlays": [...], "trilha": {...}, "modo_916": "fill"
 },
 "videos": {                       // override por arquivo (nome exato)
   "webinar.mp4": {"in": 120.0, "out": 210.0, "legendas": {"estilo": "karaoke"}},
   "descartar.mp4": {"pular": true}
 }
}

Fluxo por vídeo: <saida>/<nome>/ com analise.json -> edl.json -> <nome>_final.mp4
+ manifest.json de etapas. Se um edl.json JÁ EXISTE (escrito à mão/pelo Claude),
o lote NÃO o sobrescreve — só renderiza. Relatório consolidado no final.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from comum import carregar_json, fail, salvar_json

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXTENSOES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def montar_edl(video: Path, pasta_video: Path, cfg: dict, nome: str) -> dict:
    clip = {"src": str(video.resolve()), "analise": str(pasta_video.resolve()),
            "modo_916": cfg.get("modo_916", "fill")}
    for k in ("in", "out", "punch"):
        if k in cfg:
            clip[k] = cfg[k]
    if cfg.get("cortar_silencio", True):
        clip["cortar_silencio"] = True
        clip["punch_alternado"] = cfg.get("punch_alternado", False)
    edl = {"saida": {"arquivo": f"{nome}_final.mp4"},
           "clips": [clip], "loudnorm": cfg.get("loudnorm", True)}
    for k in ("legendas", "endcard", "capa", "overlays", "trilha", "identidade",
              "cortar_silencio_params"):
        if k in cfg and cfg[k]:
            edl["cortar_silencio" if k == "cortar_silencio_params" else k] = cfg[k]
    for k in ("identidade",):                 # caminhos relativos ao cwd do lote
        if edl.get(k):
            edl[k] = str(Path(edl[k]).resolve())
    if edl.get("trilha", {}).get("arquivo"):
        edl["trilha"] = {**edl["trilha"], "arquivo": str(Path(edl["trilha"]["arquivo"]).resolve())}
    return edl


def etapa_ok(manifest: dict, etapa: str) -> bool:
    return manifest.get("etapas", {}).get(etapa) is True


def marcar(manifest_path: Path, manifest: dict, etapa: str, ok: bool, erro: str | None = None):
    manifest.setdefault("etapas", {})[etapa] = ok
    manifest["ultimo_erro"] = erro
    salvar_json(manifest_path, manifest)


def processar_video(video: Path, saida_base: Path, cfg: dict, etapas: list[str],
                    qualidade: str, force: bool, nome: str) -> dict:
    pasta = saida_base / nome
    pasta.mkdir(parents=True, exist_ok=True)
    mpath = pasta / "manifest.json"
    resultado = {"video": video.name, "status": "ok", "detalhe": ""}

    try:
        try:
            manifest = carregar_json(mpath) if mpath.exists() else {"video": str(video)}
        except Exception:
            manifest = {"video": str(video)}  # manifest corrompido: recomeça este vídeo
        st = video.stat()
        assinatura = {"tamanho": st.st_size, "mtime": int(st.st_mtime)}
        if manifest.get("assinatura") != assinatura:
            manifest = {"video": str(video), "assinatura": assinatura}  # fonte mudou: zera
        if "analisar" in etapas and (force or not etapa_ok(manifest, "analisar")):
            from analisar import analisar
            an = analisar(video, pasta, cfg.get("modelo", "small"), "auto",
                          cfg.get("idioma", "pt"), 0.45, force)
            marcar(mpath, manifest, "analisar", True)
            if an.get("suspeitas_alucinacao"):
                resultado["detalhe"] += f"{len(an['suspeitas_alucinacao'])} suspeita(s) de alucinação; "

        edl_path = pasta / "edl.json"
        if "edl" in etapas:
            if edl_path.exists() and not force:
                resultado["detalhe"] += "edl.json existente mantido; "
            else:
                salvar_json(edl_path, montar_edl(video, pasta, cfg, nome))
            marcar(mpath, manifest, "edl", True)

        if "render" in etapas and (force or not etapa_ok(manifest, f"render_{qualidade}")):
            if not edl_path.exists():
                raise RuntimeError("sem edl.json para renderizar")
            from render_edl import render
            render(edl_path, qualidade, forcar=True)
            marcar(mpath, manifest, f"render_{qualidade}", True)
            rel = pasta / f"{nome}_final_render" / "relatorio.json"
            if rel.exists():
                problemas = carregar_json(rel).get("problemas", [])
                if problemas:
                    resultado["status"] = "com_problemas"
                    resultado["detalhe"] += "; ".join(problemas)
    except SystemExit as e:
        resultado["status"] = "erro"
        resultado["detalhe"] += f"abortado (exit {e.code})"
        marcar(mpath, manifest, "erro", False, resultado["detalhe"])
    except Exception as e:
        resultado["status"] = "erro"
        resultado["detalhe"] += f"{type(e).__name__}: {e}"
        marcar(mpath, manifest, "erro", False, traceback.format_exc()[-2000:])
    return resultado


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pasta")
    ap.add_argument("--instrucoes", default=None)
    ap.add_argument("--saida", default=None, help="default: <pasta>/_edicao")
    ap.add_argument("--qualidade", default="preview", choices=["draft", "preview", "final"])
    ap.add_argument("--etapas", default="analisar,edl,render")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pasta = Path(a.pasta)
    if not pasta.is_dir():
        fail(f"pasta não encontrada: {pasta}")
    videos = sorted(p for p in pasta.iterdir() if p.suffix.lower() in EXTENSOES)
    if not videos:
        fail(f"nenhum vídeo em {pasta} (extensões: {', '.join(sorted(EXTENSOES))})")

    instrucoes = carregar_json(a.instrucoes) if a.instrucoes else {}
    padrao = instrucoes.get("padrao", {})
    por_video = instrucoes.get("videos", {})
    saida_base = Path(a.saida) if a.saida else pasta / "_edicao"
    etapas = [e.strip() for e in a.etapas.split(",")]

    # a.mp4 e a.mov colidiriam na mesma pasta de trabalho — desambigua pela extensão
    stems = [v.stem for v in videos]
    nomes = {v: (v.stem if stems.count(v.stem) == 1 else f"{v.stem}_{v.suffix.lstrip('.')}")
             for v in videos}

    print(f"Lote: {len(videos)} vídeo(s), etapas {etapas}, qualidade {a.qualidade} -> {saida_base}")
    resultados = []
    for i, v in enumerate(videos, 1):
        cfg = {**padrao, **por_video.get(v.name, {})}
        if cfg.get("pular"):
            print(f"[{i}/{len(videos)}] {v.name}: pulado (instrução)")
            continue
        print(f"[{i}/{len(videos)}] {v.name}...")
        r = processar_video(v, saida_base, cfg, etapas, a.qualidade, a.force, nomes[v])
        resultados.append(r)
        print(f"    -> {r['status']} {r['detalhe']}")

    consolidado = saida_base / "relatorio_lote.json"
    salvar_json(consolidado, {"pasta": str(pasta), "qualidade": a.qualidade,
                              "etapas": etapas, "resultados": resultados})
    ok = sum(1 for r in resultados if r["status"] == "ok")
    print(f"\nLote concluído: {ok}/{len(resultados)} ok -> {consolidado}")
    for r in resultados:
        if r["status"] != "ok":
            print(f"  {r['status'].upper()}: {r['video']} — {r['detalhe'][:120]}")


if __name__ == "__main__":
    main()
