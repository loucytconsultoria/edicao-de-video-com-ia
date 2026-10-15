# -*- coding: utf-8 -*-
"""Pipeline de UMA peça, com humano no circuito:  plano -> montar -> aprovar.

  plano    lê o casamento (saída do melhor_de.py) e mostra o que vai para o
           vídeo: trechos, takes, durações, frases que faltaram, pausas
           encurtadas. Nada é renderizado. É o que o revisor aprova.
  montar   casamento -> EDL -> render_edl.py (corte sem legenda) ->
           transcrição + alinhamento fonético DA SAÍDA -> legenda (opcional,
           texto do bruto + tempo medido) -> portões medidos na saída:
             checar_corte.py   palavra decepada/perdida, engasgo, repetição
             checar_palavras   o que não podia sumir, o que não podia entrar
             auditar_video     frames de vãos sem fala e de emendas (para OLHAR)
           Tudo fica em staging (<trabalho>/<nome>/final_tmp.mp4) com um
           relatorio_portoes.json. Nada vai para a pasta final aqui.
  aprovar  depois que uma PESSOA assistiu o staging e abriu os frames, promove
           a peça para a pasta final com um carimbo APROVADA.json (hash do
           EDL, versão dos portões, quem aprovou). Recusa se algum portão
           reprovou ou não rodou — salvo override declarado (--override
           "motivo"), que fica gravado no carimbo.

Uso:
  python pipeline_peca.py plano   --casamento c.json
  python pipeline_peca.py montar  --casamento c.json --roteiro R.txt --nome peca01
                                  --trabalho trabalho/ [--qualidade preview|final]
                                  [--legenda karaoke] [--identidade marca.json]
                                  [--modelo large-v3] [--sem-transcricao]
  python pipeline_peca.py aprovar --trabalho trabalho/ --nome peca01 --final final/
                                  --revisor "Nome" [--override "motivo"]

Exit: 0 ok | 2 reprovada/recusada | 1 quebra (não é reprovação).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
from comum import carregar_json, probe, salvar_json

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")

PY = sys.executable
VERSAO_PORTOES = "1"     # subir quando uma regra de portão mudar: aprovações antigas caducam


def roda(args, cwd=None):
    r = subprocess.run([PY, *map(str, args)], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r


# ------------------------------------------------------------------ plano ----

def mostrar_plano(cas: dict) -> None:
    print(f"Roteiro: {cas.get('roteiro')}  | takes usados: {', '.join(cas.get('takes_usados', []))}")
    print(f"Frases: {cas.get('frases')}  | cobertura média {100 * cas.get('cobertura_media', 0):.1f}%"
          f"  | duração útil {cas.get('dur_util', 0):.1f}s  | trocas de take {cas.get('trocas_de_take', 0)}")
    if cas.get("frases_faltando"):
        print(f"ATENÇÃO: frases sem execução aproveitável (ficaram de fora): {cas['frases_faltando']}")
    for k, c in enumerate(cas["clips"], 1):
        segs = c.get("cutmap") or [[c["in"], c["out"]]]
        print(f"  {k:2d}. {c.get('nome_take', Path(c['src']).name):<24} "
              f"{c['in']:7.2f}-{c['out']:7.2f}  ({c.get('dur_util', c['out'] - c['in']):5.1f}s úteis, "
              f"{len(segs) - 1} pausa(s) encurtada(s))  frases {c['frases'][0]}-{c['frases'][-1]}"
              + (f"  +respiro {c['silencio_antes']}s" if c.get("silencio_antes") else ""))


# ------------------------------------------------------------------- EDL -----

def casamento_para_edl(cas: dict, saida_mp4: Path, pasta_wk: Path) -> dict:
    """UMA camada decide a borda: o montador (régua fonética). O render só
    EXECUTA os [in,out] que recebe; 'cortar_silencio' fica DESLIGADO de
    propósito — refazer o corte com tempos do Whisper sobrescrevia a borda
    medida."""
    clips, ultimo_punch = [], 1.0
    for c in cas["clips"]:
        salto = c.get("salto_antes")
        # salto grande dentro do mesmo take = jump cut: um punch-in leve disfarça
        punch = 1.05 if (salto is not None and salto > 1.5 and ultimo_punch == 1.0) else 1.0
        ultimo_punch = punch
        sil = c.get("silencio_antes")
        for k, (i0, o0) in enumerate(c.get("cutmap") or [[c["in"], c["out"]]]):
            cl = {"src": c["src"], "in": round(i0, 3), "out": round(o0, 3),
                  "modo_916": c.get("modo_916", "fill"), "cortar_silencio": False,
                  "analise": c["analise"], "punch": punch}
            if sil and k == 0:
                cl["silencio_antes"] = sil      # respiro que o bruto não tinha
            clips.append(cl)
    return {"saida": {"arquivo": str(saida_mp4), "pasta_trabalho": str(pasta_wk)},
            "clips": clips, "loudnorm": True}


def palavras_do_bruto(edl: dict) -> list[dict]:
    """Legenda nasce do BRUTO: texto do Whisper do bruto + tempo do alinhamento
    fonético, remapeados pelo cutmap. Retranscrever a saída herda erros novos e
    mascara defeito com texto plausível."""
    out, t_out, cache = [], 0.0, {}
    for c in edl["clips"]:
        p = c["analise"]
        if p not in cache:
            cache[p] = carregar_json(Path(p) / "analise.json")
        t_out += float(c.get("silencio_antes", 0.0))
        for w in cache[p]["palavras"]:
            if w["s"] >= c["in"] - 0.01 and w["e"] <= c["out"] + 0.01 and (w["e"] - w["s"]) >= 0.02:
                out.append({**w, "s": round(t_out + (w["s"] - c["in"]), 3),
                            "e": round(t_out + (w["e"] - c["in"]), 3)})
        t_out += c["out"] - c["in"]
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ----------------------------------------------------------------- montar ----

def montar(a) -> int:
    cas = carregar_json(a.casamento)
    pasta = Path(a.trabalho) / a.nome
    pasta.mkdir(parents=True, exist_ok=True)
    sem_leg = pasta / "sem_legenda.mp4"
    edl = casamento_para_edl(cas, sem_leg.resolve(), (pasta / "wk").resolve())
    if a.identidade:
        edl["identidade"] = str(Path(a.identidade).resolve())
    edl_path = pasta / "edl_efetivo.json"
    salvar_json(edl_path, edl)
    rel = {"peca": a.nome, "versao_portoes": VERSAO_PORTOES, "edl_sha": sha(edl_path),
           "roteiro": str(a.roteiro), "portoes": {}}

    print(f"[1] render do corte ({a.qualidade})...", flush=True)
    r = roda([AQUI / "render_edl.py", edl_path, "--qualidade", a.qualidade, "--forcar"])
    print(r.stdout.rstrip())
    if r.returncode != 0 or not sem_leg.exists():
        print(r.stderr[-2000:], file=sys.stderr)
        return 1 if r.returncode != 2 else 2

    an_corte = pasta / "analise_corte"
    if a.sem_transcricao:
        print("[2] transcrição da saída PULADA (--sem-transcricao): portões de áudio não rodam")
        for g in ("checar_corte", "checar_palavras"):
            rel["portoes"][g] = {"status": "nao_rodou", "motivo": "--sem-transcricao"}
    else:
        print("[2] transcrição + alinhamento fonético da saída...", flush=True)
        r = roda([AQUI / "analisar.py", sem_leg, "--saida", an_corte, "--modelo", a.modelo,
                  "--idioma", a.idioma, "--force"])
        if r.returncode != 0:
            print(r.stderr[-2000:], file=sys.stderr)
            return 1
        r = roda([AQUI / "alinhar_fonetico.py", an_corte])
        if r.returncode != 0:
            print(r.stderr[-2000:], file=sys.stderr)
            return 1

    staging = pasta / "final_tmp.mp4"
    if a.legenda:
        print(f"[3] legenda '{a.legenda}' (texto do bruto, tempo medido)...", flush=True)
        an_leg = pasta / "analise_legenda"
        pal = palavras_do_bruto(edl)
        salvar_json(an_leg / "analise.json", {"video": str(sem_leg.resolve()),
                                             "meta": {"duracao": probe(sem_leg)["duracao"]},
                                             "palavras": pal})
        args = [AQUI / "gerar_ass.py", an_leg, "--estilo", a.legenda,
                "--calar-silencio", str(a.calar_silencio), "--saida-ass", pasta / "legenda.ass",
                "--queimar", staging, "--video", sem_leg]
        if a.fonte:
            args += ["--fonte", a.fonte]
        r = roda(args)
        print(r.stdout.rstrip())
        rel["portoes"]["cobertura_legenda"] = {"status": "ok" if r.returncode == 0 else "reprovada"}
        if r.returncode != 0:
            print(r.stderr[-1500:], file=sys.stderr)
    else:
        shutil.copy2(sem_leg, staging)
        print("[3] sem legenda: staging = corte sem legenda")

    if not a.sem_transcricao:
        print("[4] portões medidos na saída...", flush=True)
        for nome_g, args in (
                ("checar_corte", [AQUI / "checar_corte.py", pasta, "--roteiro", a.roteiro,
                                  "--casamento", a.casamento]),
                ("checar_palavras", [AQUI / "checar_palavras.py", pasta, "--roteiro", a.roteiro])):
            r = roda(args)
            print(r.stdout.rstrip())
            st = {0: "ok", 2: "reprovada"}.get(r.returncode, "quebrou")
            rel["portoes"][nome_g] = {"status": st, "exit": r.returncode}
            if st == "quebrou":
                print(r.stderr[-1500:], file=sys.stderr)

    r = roda([AQUI / "auditar_video.py", staging, "--render", pasta])
    print(r.stdout.rstrip())
    rel["portoes"]["auditar_video"] = {
        "status": "olhar_frames" if r.returncode == 2 else ("ok" if r.returncode == 0 else "quebrou"),
        "frames": str(pasta / "_visual")}

    rel["staging"] = str(staging)
    rel["staging_sha"] = sha(staging)
    rel["duracao"] = round(probe(staging)["duracao"], 3)
    salvar_json(pasta / "relatorio_portoes.json", rel)
    estados = {k: v["status"] for k, v in rel["portoes"].items()}
    print(f"\nPortões: {estados}")
    if any(v in ("reprovada",) for v in estados.values()):
        print("REPROVADA: corrigir o MÉTODO (montador/EDL), não o arquivo.")
        return 2
    if any(v == "quebrou" for v in estados.values()):
        return 1
    print(f"Staging pronto para revisão humana: {staging}\n"
          f"Assista, abra os frames em {pasta / '_visual'} e rode 'aprovar'.")
    return 0


# ---------------------------------------------------------------- aprovar ----

def aprovar(a) -> int:
    pasta = Path(a.trabalho) / a.nome
    relp = pasta / "relatorio_portoes.json"
    if not relp.exists():
        print("sem relatorio_portoes.json: rode 'montar' antes", file=sys.stderr)
        return 1
    rel = carregar_json(relp)
    staging = Path(rel["staging"])
    if not staging.exists() or sha(staging) != rel["staging_sha"]:
        print("staging ausente ou alterado depois dos portões: rode 'montar' de novo", file=sys.stderr)
        return 2
    if rel.get("versao_portoes") != VERSAO_PORTOES:
        print(f"aprovação recusada: portões rodaram na versão {rel.get('versao_portoes')}, "
              f"a atual é {VERSAO_PORTOES} — remonte a peça", file=sys.stderr)
        return 2
    pendentes = {k: v["status"] for k, v in rel["portoes"].items()
                 if v["status"] not in ("ok", "olhar_frames")}
    if pendentes and not a.override:
        print(f"aprovação recusada: portões não aprovados {pendentes}. "
              f"Para aprovar mesmo assim, declare --override \"motivo\" (fica no carimbo).",
              file=sys.stderr)
        return 2
    final = Path(a.final)
    final.mkdir(parents=True, exist_ok=True)
    alvo = final / f"{a.nome}.mp4"
    shutil.copy2(staging, alvo.with_suffix(".tmp.mp4"))
    os.replace(alvo.with_suffix(".tmp.mp4"), alvo)          # promoção atômica
    carimbo = {"peca": a.nome, "arquivo": alvo.name, "sha": sha(alvo),
               "edl_sha": rel["edl_sha"], "versao_portoes": rel["versao_portoes"],
               "portoes": rel["portoes"], "revisor": a.revisor,
               "aprovado_em": dt.datetime.now().isoformat(timespec="seconds")}
    if pendentes:
        carimbo["override"] = {"portoes": pendentes, "motivo": a.override}
    salvar_json(alvo.with_suffix(".APROVADA.json"), carimbo)
    print(f"APROVADA: {alvo}  (carimbo {alvo.with_suffix('.APROVADA.json').name})")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plano")
    p.add_argument("--casamento", required=True)
    m = sub.add_parser("montar")
    m.add_argument("--casamento", required=True)
    m.add_argument("--roteiro", required=True)
    m.add_argument("--nome", required=True)
    m.add_argument("--trabalho", required=True)
    m.add_argument("--qualidade", default="preview", choices=["draft", "preview", "final"])
    m.add_argument("--legenda", default=None, choices=["classica", "palavra", "blocos", "karaoke"])
    m.add_argument("--calar-silencio", type=float, default=0.30)
    m.add_argument("--fonte", default=None)
    m.add_argument("--identidade", default=None)
    m.add_argument("--modelo", default="large-v3")
    m.add_argument("--idioma", default="pt")
    m.add_argument("--sem-transcricao", action="store_true",
                   help="não transcreve a saída (portões de áudio não rodam; aprovar exigirá override)")
    ap_ = sub.add_parser("aprovar")
    ap_.add_argument("--trabalho", required=True)
    ap_.add_argument("--nome", required=True)
    ap_.add_argument("--final", required=True)
    ap_.add_argument("--revisor", required=True, help="quem assistiu e aprovou")
    ap_.add_argument("--override", default=None, help="motivo para aprovar com portão pendente")
    a = ap.parse_args()
    if a.cmd == "plano":
        mostrar_plano(carregar_json(a.casamento))
        return 0
    return montar(a) if a.cmd == "montar" else aprovar(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
