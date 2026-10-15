# fonts/ — fontes (não versionadas)

Nenhuma fonte vem no repositório. O motor procura arquivos `.ttf`/`.otf`, nesta
ordem: `$VIDEO_IA_FONTS`, esta pasta, e as pastas de fontes do sistema
(Windows, Linux, macOS). Sem nenhuma, capas e faixas caem na fonte embutida do
Pillow e a legenda usa a família pedida se o sistema a tiver.

## A identidade de exemplo usa

| Papel | Fonte | Onde baixar | Licença |
|---|---|---|---|
| Títulos de capa e end card | Montserrat ExtraBold (`Montserrat-ExtraBold.ttf`) | [Google Fonts › Montserrat](https://fonts.google.com/specimen/Montserrat) | SIL Open Font License 1.1 |
| Corpo | Montserrat Medium (`Montserrat-Medium.ttf`) | idem | SIL OFL 1.1 |
| Legenda | Arial (fonte do sistema) | já vem no Windows/macOS; no Linux, use outra família em `config/identidade_exemplo.json` | — |

Para a sua marca, troque `fontes` e `legenda.fonte` no JSON de identidade e
coloque os arquivos aqui (ou em `$VIDEO_IA_FONTS`).

## Armadilha do libass

O libass (legendas) resolve a fonte pelo **nome de família** e, se não achar,
**troca por uma genérica sem erro nem aviso**. Depois de configurar, renderize
um frame e olhe:

```bash
ffmpeg -i video.mp4 -vf "ass=legendas.ass:fontsdir=fonts" -frames:v 1 -y teste.png
```

O libass também escala o corpo pela métrica OS/2 da fonte (winAscent +
winDescent), então a largura que o Pillow mede não é a que aparece na tela.
`scripts/calibrar_libass.py FONTE.ttf "Família" CORPO` mede o fator.
