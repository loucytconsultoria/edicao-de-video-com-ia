---
name: edicao-video
description: Edita vídeo falado de forma 100% programática (ffmpeg + libass + faster-whisper + alinhamento fonético) para formatos verticais 9:16 — corte de silêncio sem cortar palavra, montagem do melhor trecho entre takes a partir de um roteiro, legendas com palavra ativa, enquadramento por rosto, transição de zoom suave, cama musical em nível constante, lote com retomada. Sempre no fluxo plano -> aprovação humana -> render -> portões medidos na saída. Gatilhos: "edita esse vídeo", "corta os silêncios", "faz um corte de X", "legenda esse vídeo", "monta o vídeo a partir do roteiro", "processa os vídeos desta pasta".
---

# edicao-video

Os scripts em `../scripts/` fazem o trabalho mecânico e determinístico. **Você
(o agente) faz o trabalho de editor:** decidir cortes, casar roteiro com fala,
escrever o EDL, mostrar o plano ao usuário e revisar o resultado com os
próprios olhos (frames) e ouvidos (medição na saída). Antes de qualquer edição,
leia `../docs/licoes.md`: cada regra ali veio de um defeito real medido.

**Requisitos:** `ffmpeg` com libass; Python 3.10+ com `faster-whisper`,
`torch`/`torchaudio` (alinhador MMS_FA), `numpy`, `Pillow`, `rapidfuzz`,
`num2words`; `opencv-python` para rosto (modelo YuNet em `../models/`). Todos os
scripts têm `--help` e rodam de qualquer diretório. Atalho:
`python ../scripts/video_ia.py <comando>`.

## Regra de ouro: nunca renderize final sem plano aprovado

1. `analisar.py VIDEO --saida PASTA` — texto por palavra (faster-whisper + VAD).
2. `alinhar_fonetico.py PASTA` — **a régua de tempo**: mede foneticamente onde
   cada palavra está; sem isso o montador aborta. Depois, `limpar_gaps.py PASTA`.
3. Decidir o corte:
   - pedido simples ("tira os silêncios"): `cortar_silencio.py PASTA` imprime o
     plano e grava `corte.json`;
   - com roteiro e vários takes: `melhor_de.py ROTEIRO PASTAS... --saida c.json`
     (frase atômica, best-of entre takes, cutmap medido);
   - sem roteiro ("faz um corte bom"): leia `analise.json` e escreva o EDL você
     mesmo, com critério editorial (gancho forte primeiro), listando o que ficou
     de fora e por quê.
4. **Mostrar o plano ao usuário** (`pipeline_peca.py plano --casamento c.json`
   ou o plano do `cortar_silencio`): trechos, durações, o que sai, frases que
   faltaram. Esperar a aprovação.
5. `pipeline_peca.py montar ... --qualidade preview` (ou `render_edl.py EDL
   --qualidade preview`): o render só EXECUTA o cutmap, não recalcula.
6. **Portões** (rodam no `montar`): `checar_corte.py` (palavra decepada/perdida,
   engasgo na emenda, repetição, cobertura), `checar_palavras.py` (o que não
   podia sumir/entrar), `auditar_video.py` (frames de vãos sem fala e emendas —
   ABRA os frames). Exit 2 = reprovada: conserte o método, não o arquivo.
   Exit 1 = o portão quebrou: não é reprovação.
7. Aprovado pelo usuário: `--qualidade final` e `pipeline_peca.py aprovar
   --revisor NOME` (staging -> pasta final + carimbo `APROVADA.json`).

## Princípios que não se negociam

- **Whisper lê, alinhador fonético mede, energia cala.** O Whisper erra ~0,25 s
  por borda (mediana) e fecha a palavra cedo; borda, corte e legenda usam a
  régua fonética. Reouvir com o mesmo Whisper não corrige nada.
- **Uma camada decide a borda** (o montador), o render executa, o portão mede.
  No EDL vindo do montador, `cortar_silencio: false`.
- **Áudio limpo não é vídeo limpo:** a peça só é aprovada depois de alguém
  OLHAR os frames que a auditoria marcou.
- **Acusação sobre o corte se confirma no bruto, não no roteiro:** improviso
  não é defeito; se a palavra não existe em take nenhum, não houve corte.
- **Duração é alvo, não teto.** Sobrar é melhor que faltar.
- **Legenda sem palavra no silêncio** (`--calar-silencio 0.30`) e com
  cobertura exata (cada palavra, um evento). Legenda nunca sobre o rosto
  (`checar_legenda_rosto.py`) nem sob a UI do aplicativo.
- **Zoom suave, não corte seco nem punch brusco** (`transicoes.py`, padrão
  3,5% em 0,55 s, subpixel), aplicado no corte MEDIDO.
- **Música é acabamento, nunca remendo:** `musica.py` em nível constante
  relativo à voz medida, escrevendo em outra pasta; peça densa vai sem música.
- **Quem produz não verifica:** o portão é outro programa, e a aprovação é de
  uma pessoa.

## Triagem do material (antes de editar)

Extraia 4–6 frames espalhados e OLHE: aspecto, legenda ou texto já queimado.
Legenda existente que cabe no corte é herdada (nunca gere outra por cima).
Texto que o enquadramento mutilaria pede `fit`/`blur` ou outro crop — e, se
nada preservar tudo, mostre o trade-off (frame + recomendação) antes de
renderizar. A decisão é do usuário.

## Ferramentas (../scripts/)

| Script | Faz |
|---|---|
| `analisar.py` | transcrição word-level + blocos de fala + pausas + suspeitas de alucinação |
| `alinhar_fonetico.py` | régua de tempo por alinhamento forçado (MMS_FA); saneia cauda alucinada; retranscreve áudio não explicado |
| `limpar_gaps.py` | remove inserções do alinhador que são ruído/duplicata |
| `calibrar_fa.py` | mede o viés do alinhador contra a energia (1x por setup) |
| `cortar_silencio.py` | plano de corte de pausas sem cortar palavra (`--render` opcional) |
| `casar_roteiro.py` | onde cada linha do roteiro foi dita (fuzzy; score < 60 = não encontrado) |
| `melhor_de.py` | montador best-of entre takes, frase a frase, com cutmap medido |
| `bordas.py` | fim/início de fala medido por energia; reprova borda sem silêncio |
| `pipeline_peca.py` | `plano` / `montar` (render + portões, em staging) / `aprovar` (carimbo) |
| `render_edl.py` | EDL JSON -> vídeo (schema no `--help`): 9:16 fill/fit/blur/rosto, xfade, capa, end card, overlays, trilha, legendas, loudnorm 2-pass, relatório |
| `gerar_ass.py` | legendas ASS (`classica`, `palavra`, `blocos`, `karaoke`) + portão de cobertura |
| `transicoes.py` | `zoom_suave` (padrão), `blur_cross`, `push` nos cortes medidos |
| `rostos.py` | rosto principal e linha dos olhos (YuNet) -> crop 9:16 |
| `checar_legenda_rosto.py` | legenda sobre o rosto? recomenda e aplica nova MarginV |
| `checar_corte.py` / `checar_palavras.py` / `auditar_video.py` | portões medidos na saída |
| `musica.py` | cama musical em nível constante medido (ebur128) |
| `capa.py` | capa e end card na identidade (`../config/identidade_exemplo.json`) |
| `exportar.py` | normaliza vídeo pronto para 1080x1920, H.264, AAC, -14 LUFS |
| `lote.py` | pasta inteira com instruções, manifest e retomada idempotente |
| `calibrar_libass.py` | fator de escala PIL -> libass de uma fonte |

## Lote

`lote.py PASTA --instrucoes I.json --etapas analisar` (só transcreve, com
cache) -> você escreve um `edl.json` por vídeo (o lote nunca sobrescreve) ->
`--etapas render --qualidade preview`, QA por amostragem (todos os
`relatorio.json` + frames de 2–3 vídeos), aprovação -> `--qualidade final
--force` só nos aprovados. Falhou no meio? Rode de novo: o manifest pula o que
já passou.

## Specs de entrega (embutidas no render)

1080x1920@30, H.264 High yuv420p, ~8 Mbps (NVENC com fallback x264), AAC
192k 48 kHz estéreo, loudnorm -14 LUFS / -1 dBTP (2 passadas), +faststart.
Área segura das plataformas verticais: topo ~250 px, base ~500 px, direita
~120 px.

## Armadilhas (detalhe em ../docs/licoes.md, seção 6)

- ffmpeg sempre via `comum.ffmpeg()` (lista de args, `-nostdin`, timeout,
  fallback NVENC -> x264). Nunca string de shell com filtro dentro.
- Filtro `ass=`: cwd na pasta do `.ass` e caminho RELATIVO.
- `-c copy` não corta com precisão; `-ss` de saída com `-af` trunca o áudio;
  `-to` com `-ss` de entrada dá duração errada — use `-ss T -i src -t DUR`.
- Duração de segmento quantizada na grade de quadros, senão o vídeo trava na
  emenda.
- O libass troca de fonte em silêncio: confira um frame renderizado.

## Conformidade

Editar imagem real (cortar, legendar, enquadrar) não é conteúdo sintético. Se
o pedido incluir b-roll, imagem ou voz GERADOS por IA, avise no plano: muitas
plataformas e normas exigem rótulo. Corte que muda o sentido da fala de
terceiros é risco jurídico — sinalize antes de renderizar.
