# Lições aprendidas

Estas regras saíram da produção real de um motor de edição programática de vídeo falado. Cada uma nasceu de um defeito que alguém encontrou e que depois foi medido. Para cada regra há o mecanismo técnico que a explica e, quando existe, a forma de medir ou aplicar. Os números citados são medições feitas sobre o material real, e não estimativas.

---

## 1. Princípios

**Whisper lê, o alinhador fonético mede, a energia cala.**
O Whisper reconhece bem o texto e marca mal o tempo. Medido num take de 562 palavras: erro mediano de 0,25 s por borda, p90 de 0,44 s, máximo de 1,24 s. Ele marca o fim da palavra cedo, comprime números e incha palavras de até 3 letras em cerca de 2×. Toda borda de corte, todo corte de silêncio e todo tempo de legenda usam a régua do alinhamento fonético forçado (MMS_FA). A decisão final sobre onde há fala fica com o envelope de energia.

**Reouvir com o mesmo instrumento não corrige o instrumento.**
Comparar a duração que o Whisper dá no bruto com a que ele dá no corte é usar a mesma régua errada nos dois lados. Uma heurística montada sobre uma régua errada produz, ao mesmo tempo, falso positivo e falso negativo.

**Uma camada decide a borda, o render executa e o portão mede.**
Num primeiro desenho, o montador media a borda e o render a recalculava com o Whisper por cima. A correção nunca chegava ao vídeo. Agora o montador entrega um cutmap completo (bordas e silêncios internos), o render só executa `[in, out]` e o portão confere o arquivo final.

**Conferir a legenda não é conferir o corte.**
O texto pode estar certo sobre um áudio quebrado. Uma palavra decepada foi retranscrita como outra palavra plausível e passou na comparação de texto, porque o diff a viu como substituição e não como remoção. Conferir o corte é alinhar o texto esperado no áudio de saída e comparar o span de cada palavra com o span dela no bruto.

**Quem produz não verifica.**
O portão é um programa separado do montador, e a revisão humana recebe material próprio para conferência: uma faixa de áudio só com as emendas e um preview em baixa resolução. Delegar uma auditoria a agentes não delega a responsabilidade de conferir o instrumento que eles usam.

**Áudio limpo não é vídeo limpo.**
O portão de áudio inteiro mede som. Um trecho mudo com o apresentador mexendo no celular passa em todas as conferências, porque ali não há áudio para medir. A peça só é aprovada depois que alguém abre os frames que a auditoria visual marcou.

**Saída 1 do portão é quebra; saída 2 é reprovação.**
Um driver que tratava qualquer código diferente de zero como reprovação relatou "17 reprovadas". Na verdade nenhuma delas tinha reprovado: faltava um arquivo de entrada do portão. Uma reprovação sempre traz a lista de problemas e as medições. Se as duas coisas faltam, o portão quebrou, e o que se investiga é a ferramenta, não a peça.

**Uma regra de portão que muda invalida as aprovações antigas.**
O carimbo registra que a peça passou por uma versão do portão, e não pela versão atual. Por isso a limpeza de disco tem de preservar as entradas do portão, como o áudio sem legenda e o alinhamento da saída. Sem elas, reverificar custa um render inteiro.

**Toda regra que pergunta "isso está repetido?" declara a granularidade em que olha.**
Trecho, segmento, junção entre segmentos, junção entre clipes, bloco agrupado: uma repetição escapa por baixo de qualquer regra que olhe numa granularidade maior que a dela. A mesma frase dita duas vezes passou várias vezes porque o dedup olhava trechos inteiros e a repetição estava na junção. A deduplicação roda sobre a estrutura final que vai para o render, e não sobre uma estrutura intermediária.

**Antes de declarar defeito, remeça os dois lados com a mesma janela.**
O CTC distribui as palavras curtas entre vizinhas de um jeito diferente a cada passagem. A mesma palavra mediu 0,30 s num bloco longo e 0,12 s numa janela local, nos dois lados. Uma comparação entre medições feitas em janelas diferentes gera falso alarme.

**Quando o portão acusa, a primeira ação é reouvir, não corrigir.**
Foram oito tentativas ajustando borda e pad para um "gaguejo" que não existia: era o Whisper partindo uma vogal alongada em dois tokens. Numa janela curta e isolada, o ASR acerta.

**Valide o extrator antes de agir sobre um relatório.**
Um número redondo e alarmante ("50 omissões em 20 de 22 peças") tem mais chance de ser bug de medição do que epidemia real. Nesse caso o extrator deduplicava linhas idênticas de legenda, e a medição real era zero. Quando o relatório e o código discordam, o código é a testemunha.

**Desconfie do objeto medido, não da existência do defeito.**
Se a medição não acha nada onde o defeito claramente está, ela está olhando o lugar errado. O silêncio que sobrevivia ao corte não estava entre palavras: estava dentro do span de uma palavra.

**Uma detecção correta com uma ação errada é pior do que nenhuma detecção.**
A detecção de silêncio dentro do span acertou os intervalos ao centésimo, mas inseriu alucinações retranscritas no lugar, e com cara de conteúdo legítimo. Antes de agir sobre uma medição nova, pergunte o que acontece quando a ação falha e garanta que o estado anterior possa ser preservado.

**Uma ação que remove algo do que o portão mede é uma mudança no portão.**
Apagar um token "quase todo mudo" tirou o fim de um número da lista de palavras esperadas. Nenhum portão acusaria esse número cortado ao meio, porque ele não era mais esperado. Encolha o token, mas nunca o remova.

**Uma decisão, uma função.**
A classificação de disfluência existia em dois lugares. Toda melhoria ia para um deles, e o vídeo continuava usando a cópia antiga. Se o teste isolado passa e o vídeo não muda, procure a segunda implementação antes de mexer no critério.

**Quando uma regra "não pega", verifique se ela roda.**
Uma comparação com uma variável fora de escopo era sempre falsa, e o pad de fim de peça nunca foi aplicado. Em outro caso, um `max()` aplicado depois de um `min()` desfazia o limite que o `min()` impunha. Um `print` que não aparece aponta o caminho melhor do que uma hipótese.

**Todo número fica num lugar só, com a origem declarada.**
Cada limiar deve ser marcado como medido no falante, fixo ou calibrado. Um limiar solto dentro de uma função vira regra local calibrada em um caso só.

**Incerteza não se grava como certeza.**
Um token com confiança 0,06, um span sem medida ou uma região sem texto viravam valores falsos gravados no mesmo campo que o montador consumia. A transcrição crua deve ser imutável, a medida ausente fica `null` e o token sem medida vai para uma lista de reescuta, sem virar borda.

---

## 2. Corte e bordas

**A frase é a unidade atômica.**
Um alinhamento global maximiza as palavras casadas sem olhar de qual tentativa vem cada uma, e acaba montando uma frase com pedaços de três execuções. Cada frase sai de uma execução contígua de um único take, encontrada por janela deslizante, com programação dinâmica e custo por troca.

**Não remende clipes por proximidade temporal.**
Uma gaguejada cortada deixa um vão de 0,2 a 0,3 s. A regra de "juntar clipes próximos" recolava justamente a palavra removida.

**A sobra de fala tem quatro causas, e só duas pedem corte.**
Se o ASR ouviu diferente, mantém. Se é falsa partida ou gaguejo (repete o que vem antes ou depois), corta. Se é regravação (5 ou mais palavras sem contrapartida), corta. O resto é improviso e fica. Na escolha da janela, a disfluência pesa muito e o improviso quase nada.

**A muleta sonora inclui só não-palavras.**
Pôr "e" na lista de muletas reprovava aberturas de frase legítimas. Conjunções e marcadores do discurso são palavras de verdade.

**A falsa partida com palavra trocada se reconhece pelo recomeço.**
Quando o falante troca uma palavra e refaz a frase, nem a igualdade literal nem a comparação posicional pegam a repetição. O sinal que funciona é a sequência recomeçar dentro de si mesma (a palavra de abertura reaparece). Uma similaridade de sequência de 0,55 ou mais com o que vem depois complementa.

**Enumeração não é repetição.**
A repetição-defeito recomeça: o que vem depois das duas ocorrências é parecido. A enumeração segue adiante com um item novo. Se o trecho depois da segunda ocorrência diverge mais de 50% do trecho depois da primeira, não é repetição.

**A anáfora do roteiro é proteção.**
Uma sequência que aparece duas ou mais vezes no próprio roteiro é recurso retórico, e o aparo de repetição não toca nela.

**Duas tentativas se reconhecem pela cobertura, e não por um rótulo.**
Agrupar trechos pela tupla de frases do roteiro falhava dos dois lados: descartava metades complementares e deixava passar repetições com tuplas diferentes. Dois trechos que cobrem as mesmas palavras do roteiro (60% ou mais do menor) são duas tentativas, e fica a mais completa. Trechos com palavras disjuntas são partes complementares e ficam os dois.

**Na junção, apare; não descarte.**
Se as primeiras 3 ou mais palavras do segmento seguinte aparecem nas últimas ~14 do anterior, o anterior foi uma tentativa abandonada. Monte as duas opções (aparar a cauda ou a cabeça), fique com a de maior aderência ao roteiro e emende depois da última palavra em comum, que é onde a emenda não se ouve.

**Contenção precisa de uma métrica de contenção.**
Uma razão por conjunto de tokens não passa do limiar quando um bloco tem cinco vezes o tamanho do outro. Some `partial_ratio(curto, longo) >= 90` para detectar "um está dentro do outro".

**Uma frase sem candidato não pode derrubar a peça.**
Uma programação dinâmica sem saída falhava em cascata por causa de uma frase muito reformulada. O estado PULAR, com penalidade alta, perde uma frase, nunca a peça, e as puladas vão para o relatório.

**Integridade vem antes de cobertura.**
A nota do montador somava cobertura e escolheu uma execução que dizia mais, mas com um embolo escondido dentro do span de uma palavra. Cada segundo sem fala dentro de uma palavra de duração implausível custa mais do que qualquer ganho de cobertura.

**O silêncio pode estar dentro do span de uma palavra.**
O alinhador colapsou 3 s de nada num único token, e toda regra que olhava o vão entre palavras ficou cega. O sinal certo é a energia dentro do span, não a duração por letra: um número falado é legitimamente longo.

**O limiar de silêncio é relativo ao falante.**
Com o piso da sala em −80 dB e a voz em −33 dB, um detector fixo a −38 dB não acha nada, porque ruído de sala e estalo de boca ficam acima disso. A fórmula `piso + 30% × (voz − piso)` achou o silêncio com precisão de centésimos.

**Encolha, nunca remova.**
Se o que resta de fala no span é menor que 0,12 s, o token cobria silêncio de verdade. Se resta 0,12 s ou mais e pelo menos 30% do span é mudo, o token encolhe até a parte falada. Se a retranscrição não tem credibilidade, o original fica intacto.

**Uma retranscrição se aceita pelo melhor score do pedaço.**
Fala real mediu de 0,33 a 0,89 no alinhador. Alucinação sobre silêncio mediu de 0,02 a 0,20. O pedaço entra com score 0,25 ou mais, e só substitui o token original se contiver a palavra que sai.

**Um token fantasma não participa de decisão de corte.**
Tokens com span abaixo de 0,05 s já partiram um vão de silêncio em dois pedaços abaixo do limiar, já travaram a extensão de fim de peça e já viraram fim de frase. Nenhuma decisão de corte olha para eles.

**Um span implausível é medição inválida.**
Uma palavra de 4 ou mais letras com menos de 0,03 s por letra, ou com duração zero ou negativa, é reinterpolada entre as vizinhas e nunca usada como borda. Calibre no falante: 0,045 s por letra invalidava 409 de 612 palavras.

**Tire a cauda alucinada antes de medir.**
Um texto que o falante não disse obriga o alinhador a dar tempo a ele, e esse tempo é roubado da palavra vizinha. Medido: de 0,42 s para 0,18 s.

**Áudio sem palavra correspondente se retranscreve isolado.**
Uma repetição colapsada pelo ASR aparece como energia de fala sem palavra. Retranscrita, alinhada e inserida, ela fica visível para o montador, que então corta a repetição inteira, e não o meio dela.

**Números ditos de vários jeitos se unem antes do alinhamento.**
Algarismos se falam por extenso e se partem em tokens. Os pedaços são unidos antes de alinhar, e um número escrito em algarismos fica fora de qualquer limite de duração por letra: três caracteres podem levar mais de um segundo para ser falados.

**Dois normalizadores, dois propósitos.**
O normalizador de fonema descarta dígitos, porque alimenta o alinhador. Usá-lo para comparar identidade de palavra transformava números em string vazia, e a guarda era pulada exatamente nos tokens que mais incham.

**O fim de fala se mede pela energia.**
O Whisper fecha a palavra cedo: mediana de 180 ms, com casos de quase meio segundo. A partir do fim declarado, ande no envelope de 10 ms até achar 80 ms seguidos abaixo de 10% do nível de fala. Com 60 ms, as oclusivas passavam por silêncio.

**A busca do fim não se limita pelo início declarado da palavra seguinte.**
Esse número também vem do Whisper e chegou a vir 1,2 s adiantado, escondendo um silêncio real de 1,29 s. Use uma trava de cauda de 0,45 s: se o silêncio só aparece depois disso, a busca atravessou a fala seguinte.

**O respiro sai de dentro da pausa, e o fade tem de caber nele.**
O piso de respiro é um desejo, não uma garantia: numa pausa de 0,08 s, ele fabricava fôlego 60 ms dentro da palavra seguinte. Use uma guarda de 0,12 s antes do próximo ataque e um fade de `min(0,12 s, respiro)`. Um fade fixo abafava a consoante final mesmo com o corte no lugar certo.

**Fade só onde o áudio corta.**
Uma troca de enquadramento dentro do mesmo bloco de áudio não é corte de áudio. Um fade ali abriu um buraco de 160 ms no meio de uma palavra.

**Sem borda, a peça reprova; não se corta.**
Quando o falante emenda a frase seguinte sem nenhum silêncio, o script reprova a borda e lista as bordas próximas com silêncio real, mostrando o texto que se ganha e o que se perde. A preferência é estender.

**A pausa depois de ponto final é respiro, não silêncio a remover.**
Com a régua exata, uma `pausa_min` fixa passou a apagar as pausas naturais entre frases, e "tudo virou uma frase só". Só pausas acima do p90 do falante são tocadas, e elas ficam no p75 dele.

**O fim da peça segue a fala, não o roteiro.**
No último trecho, se a última palavra tem entonação de continuação (pausa menor que o respiro), o corte estende até o próximo fim de frase do bruto, com teto de 14 palavras ou 7 s. Nos trechos do meio isso nunca se faz.

**A cauda de fim de peça é silêncio, nunca a palavra seguinte.**
Um pad de fim aplicado depois do limite da palavra vizinha sobrescreveu esse limite e terminou a peça numa conjunção pendurada.

**Sonde as micro-pausas antes de fixar o trecho.**
O alinhador às vezes estica o fim da palavra por cima do silêncio: um vão real de 1 s apareceu como 20 ms. Só se entra ou sai onde houver 40 a 60 ms de silêncio real. Se a fala está colada, a palavra de borda muda.

**A emenda entre frases precisa de pausa medida.**
Em corte de entrevista, 80 ms entre duas frases soa atropelado. O alvo é 0,35 s ou mais de silêncio medido. Quando o silêncio não existe nas pontas da peça, complete com quadro parado e silêncio.

**Recortou a fonte, retranscreva.**
Uma fonte reextraída com segundos a mais continuou usando a transcrição do corte curto: havia fala no áudio e nenhuma palavra no texto.

---

## 3. Legendas

**Sem palavra na tela durante o silêncio.**
Uma pausa medida de 0,30 s ou mais fecha o bloco, mesmo no meio da frase, e o bloco sai 0,12 s depois da última palavra. Isso só funciona com tempos medidos no vídeo montado. Os tempos do Whisper dos takes erram até 0,8 s e acendiam palavras no silêncio.

**Varra as palavras curtas pousadas sobre o silêncio.**
Depois do alinhamento, palavras com menos de 50% do span com voz (envelope de 10 ms, 15 dB acima do ruído) são reposicionadas pelo ataque medido.

**Cobertura: cada palavra falada aparece exatamente uma vez.**
O gerador tem dois portões: nenhuma palavra perdida ou duplicada, e cada palavra com um evento cobrindo o instante em que é dita. Se falha, o script para. As exceções (palavras cobertas por título ou por um grafismo numérico) são contadas e nomeadas no relatório.

**`a[-2] += a.pop()` grava no índice errado.**
O índice −2 é resolvido antes do `pop`, e isso apagou blocos de legenda (15 de 172 palavras sem tela). Foi o portão de cobertura que expôs o problema.

**A legenda nasce do texto do bruto e do tempo medido.**
Retranscrever a saída herda erros novos e mascara defeitos com texto plausível. O texto vem do bruto, corrigido contra o roteiro por arbitragem de áudio (as duas hipóteses alinhadas, vence o maior score). O tempo é mapeado do bruto ou realinhado na saída com o alinhador fonético.

**Ao mapear, some as durações reais dos arquivos, não as pedidas.**
O ffmpeg fecha cada plano num número inteiro de quadros. Com 26 planos e meio quadro de erro em cada, a legenda chegava 0,26 s atrasada no fim. Um respiro prefixado e não somado no remapeamento adiantou a legenda de 0,3 a 0,4 s.

**No fim do bloco, a palavra tem de terminar dentro.**
Filtrar pelo ponto médio serve para o começo do bloco. No fim, o Whisper abre a palavra seguinte cedo, e a legenda escrevia uma palavra que o vídeo não chega a dizer.

**Fim de frase sempre quebra o bloco, e o ponto final aparece.**
Com tempos exatos, as palavras vêm coladas e a quebra por vão parou de quebrar em fim de frase. A troca de falante e a emenda também fecham o bloco.

**Um bloco curto pisca.**
Um bloco de uma palavra só, ou com menos de 0,4 s na tela, é juntado ao vizinho mais próximo no tempo, sem atravessar uma emenda.

**Legenda nunca sobre o rosto e sempre fora da UI do aplicativo.**
As zonas de topo, rodapé e coluna de botões são mapeadas em pixels e conferidas por frame. Quando há uma estampa ou um objeto sob a posição padrão, a legenda sobe com uma âncora no topo. Na tela dividida, ela fica dentro de um painel, com folga da costura.

**O libass escala o corpo pela métrica OS/2 da fonte.**
O corpo é escalado pela soma winAscent + winDescent, e não pelo em. Por isso o PIL e o libass discordam: o fator medido variou de 0,575 a 0,760 conforme a fonte, e um título mediu 433 px renderizados contra 649 px no PIL. Meça o fator da própria fonte uma vez e aplique-o em todo cálculo de "cabe".

**O libass troca de fonte em silêncio.**
Se não acha a família, ele usa uma fonte genérica, sem erro nem aviso. A conferência é um frame renderizado, e não "rodou sem erro".

**O corpo não encolhe para caber.**
Encolher o bloco largo produziu legendas pequenas "do nada" (68 de 140 blocos). Um bloco largo vira duas linhas, quebrado no ponto mais equilibrado. Só uma palavra isolada mais larga que a área encolhe, e isso fica registrado.

**Na palavra ativa que cresce, aumente o espaço entre palavras.**
Com espaço normal, a palavra que cresce 8% parecia grudar na vizinha. O espaço de 1,8× resolveu.

**Na legenda progressiva, o bloco inteiro é posicionado antes.**
Cada palavra é um evento com a posição calculada para o bloco completo. A palavra só muda de alfa invisível para visível no instante em que é dita, então nada pula de lugar.

**A maiúscula do ASR não é nome próprio.**
O Whisper capitaliza todo começo de segmento dele. Só é nome próprio a palavra que aparece com maiúscula no meio de um segmento.

**Pausa não é ponto.**
Uma pausa de hesitação cai no meio da frase. Só se põe ponto quando a palavra seguinte tipicamente abre uma frase.

**Pontuação pode inverter o sentido.**
"Não. Pode entrar." e "Não pode entrar." dizem coisas opostas. Correção de grafia se faz sem perguntar. Correção que muda sentido fica registrada por peça, com a janela de tempo e o motivo escrito.

**O texto queimado passa por aprovação humana antes de queimar.**
Um documento único com o texto de cada bloco resolve na origem a alucinação queimada, o nome próprio errado e a palavra que não foi dita. Depois da aprovação, o hash do texto permite requeimar sem nova revisão.

**Uma palavra só entra na legenda se for confirmada de ouvido.**
Um som de 0,27 s lido como interjeição em todos os takes era um pigarro. Uma inserção exige reescuta isolada mais roteiro, ou confiança alta.

---

## 4. Enquadramento e visual

**O detector de rosto do ffmpeg não existe.**
O `cropdetect` só acha barras pretas. Sem um detector externo (YuNet), o crop vertical cai no centro geométrico e corta o rosto quando a pessoa se move.

**A linha dos olhos ancora melhor do que a caixa do rosto.**
A caixa do detector vai da testa ao queixo, e o quanto sobra acima dela muda com a pessoa e o ângulo. Procurar a coroa da cabeça por silhueta devolveu números errados com cara de medidos sobre fundos escuros. Os olhos são marcos e não dependem do fundo: pô-los numa fração fixa do painel resolve os dois extremos.

**O topo da cabeça se mede, não se infere da caixa.**
Numa cabeça calva, o topo real fica cerca de 150 px acima da caixa. Subir pela pele a partir da testa (critério HSV validado contra o fundo, com mediana de colunas) evitou títulos cobrindo o alto da cabeça. A conferência tinha aprovado esses títulos porque comparava com a caixa.

**Filtre os falsos positivos do detector por trilha.**
Pôsteres com rostos desenhados voltavam como rosto com confiança 0,80 a 0,87. Mão, tronco e barba voltavam grandes e esporádicos. Uma detecção pequena demais em relação à maior trilha cai, e uma trilha só fica se aparece em pelo menos 20% dos instantes com confiança mediana de 0,75 ou mais. As descartadas vão para o relatório, para alguém olhar.

**Meça a janela inteira em que o elemento fica na tela.**
Três quadros amostrados não bastam: a pessoa se move. A medida cobre todos os instantes a cada 0,1 s, e o frame em tamanho cheio continua obrigatório, porque o script não vê cabelo, chapéu nem barba longa.

**A geometria não se copia de uma peça para outra.**
Uma posição de título copiada de outro vídeo cobriu boca e queixo. A zona do título e a cor do texto lateral se medem em cada peça.

**Uma medida ausente que cai num valor de reserva tem de doer.**
Brutos sem medida usaram calado a posição e a altura de reserva, com 40 px de erro horizontal e 400 px de erro vertical em relação aos planos vizinhos. O código passou a imprimir um aviso por bruto sem medida.

**O zoom é suave e subpixel.**
Um zoom punch de 7% em 0,30 s foi reprovado como brusco. O plano que entra nasce 3,5% maior e assenta em 0,55 s, com recorte subpixel. Um crop inteiro, preso a dimensões pares, avança em degraus de pixel e treme. A crítica a um recurso pede que ele seja suavizado, e não trocado por corte seco.

**Em vídeo de conversa, a transição de zoom só entra na troca de falante.**
Dentro da frase, ela não entra. Zoom alternado dentro da mesma frase ficou frenético.

**O zoom tem teto.**
Num recorte de uma fonte 1080p de transmissão, acima de 1,2× a imagem amolece.

**A transição cai no corte medido, não no corte declarado.**
Os planos declaravam o corte um quadro antes da mudança real de cena. A transição é ajustada à cena medida (`select='gt(scene,0.25)'`).

**A tela dividida entra e sai por corte seco.**
Numa referência medida quadro a quadro, as dez fronteiras eram corte seco, sem deslizamento nem fade. O painel de baixo atravessa o corte contínuo: a diferença de pixel deu 0,85 contra 0,70 de movimento normal. Só a janela de cima troca.

**O painel preenche, não encaixota.**
O material entra pela dimensão que preenche o painel e corta o excesso. Tarjas de fundo acima e abaixo foram reprovadas.

**A auditoria de frames olha os vãos, as emendas e as pontas.**
Um vão sem fala de mais de 0,7 s gera um frame. Um movimento médio de 6 ou mais (luminância 0–255, quadro reduzido) é suspeito: os dois vãos reclamados mediram 24,4 e 24,3, os maiores da peça. Um salto de 12 ou mais na emenda é um corte visível. As pontas cobrem 0,6 s de cada lado. A medição diz onde olhar e não substitui olhar.

**Um vão sem fala não se esconde.**
Legenda, corte rápido ou música por cima não resolvem: o defeito é o trecho existir, e o conserto é no montador.

**Num corte multi-cenário, as trocas declaradas saem da checagem de salto.**
Toda troca de cômodo marcaria suspeita e transformaria o portão em ruído. A métrica certa é por cenário: luminância e temperatura de cor por bloco, para pegar a peça que pisca.

**Confira os quadros por tempo, não por número de quadro.**
Uma folha de QA com `select='eq(n,…)'` mostrou uma legenda "sumida" que existia.

**Quem fala se mede pelo movimento da boca.**
Um frame parado não distingue fala, mastigação e riso. Compare a energia de movimento na faixa da boca de cada lugar, quadro a quadro, com tamanho de saída fixo: deduzir a altura da imagem pela contagem de bytes fazia o detector dar a mesma resposta para todos.

**Duas câmeras se sincronizam por correlação.**
A correlação cruzada da envoltória de energia do áudio dá o deslocamento, com o pico reportado. Um pico abaixo de ~0,30 indica que não há sobreposição, e não uma falha de sincronia.

---

## 5. Áudio e música

**Loudness em duas passadas, com alvo medido no resultado.**
O alvo é −14 LUFS integrado e −1 dBTP. Medido na saída: −14,1 LUFS e −0,94 dBTP. Em áudio 100% silencioso, o loudnorm devolve −inf e precisa de um tratamento à parte.

**Takes diferentes se nivelam antes da normalização.**
Uma cópia bit a bit do bruto saía por volta de −25 LUFS, com saltos de 6 dB entre blocos. Um `dynaudnorm` (f=200, g=9, p=0,95, m=10) reduziu o salto para 3 dB, e o `loudnorm` põe a peça no alvo.

**Meça o áudio antes de nivelar.**
O `dynaudnorm` levanta o ruído de sala nas pausas, e a pausa passa a ler como fala. A conferência de bordas usa a base crua.

**Numa transmissão normalizada, meça em janelas de 20 ms.**
O loudnorm dá de +15 a +19 dB de ganho, e a janela de 5 ms acusava "voz na borda" em bordas limpas. Fala de verdade fica cerca de 10 dB acima do limiar em qualquer janela de 20 ms.

**A cama musical fica em nível constante, relativo à voz medida.**
A loudness integrada da voz e da faixa se mede por ebur128, e a cama é normalizada para `voz + dB`. Com sidechain, a cama ducada mediu muito abaixo do alvo, e o número pedido deixou de ser o número medido. Música que sobe e desce a cada pausa chama atenção para si.

**O nível da cama se decide ouvindo, em escala relativa.**
Os primeiros valores relativos testados ficaram altos demais ou quase inaudíveis. O valor aprovado ficou perto de 30 dB abaixo da voz. Medido: 0,15 LU de erro entre o alvo e a cama renderizada, efeito dos fades.

**A cama vai a zero no fim.**
Um fade de saída de 2,5 s começa em `duração − 2,5 s`.

**Quando não usar música.**
Explicação densa, aula com slides, corte de entrevista e qualquer peça em que a versão com e sem trilha, ouvida na mesma sessão, mostre que ela atrapalha. Na dúvida, entrega sem. A trilha nunca é remendo.

**A mixagem nunca escreve sobre a peça aprovada.**
A saída com música vai para outra pasta, e a original continua intacta e verificável.

---

## 6. Receitas de ffmpeg e do ambiente

**`-ss` de saída com `-af` trunca o áudio.**
No ffmpeg 8.x, o seek em dois passos (grosso antes do `-i`, fino depois), combinado com `afade` ou `aformat`, corta o áudio exatamente no valor do seek fino e completa com silêncio, sem erro. O padrão que funciona é `-ss T -i src -t DUR`.

**`-to` com `-ss` de entrada dá duração errada.**
Use `-t` com a duração.

**`-c copy` não corta com precisão.**
Ele corta no keyframe, até segundos fora do ponto pedido, e também elimina a queima de legenda. Corte é sempre reencode.

**Caminho absoluto do Windows dentro de um filtergraph é uma fonte infinita de bug.**
Os dois-pontos e a barra invertida são caracteres de controle do parser. Rode o filtro `ass=` com o diretório de trabalho na pasta do `.ass` e use caminho relativo. Para grafos longos, use `-filter_complex_script`.

**Monte o comando como lista de argumentos, nunca como string de shell.**
Rode sempre com `-y -nostdin`, `stdin=DEVNULL` e um timeout explícito. Sem isso, um arquivo de saída já existente faz o ffmpeg esperar resposta e trava o lote sem stack trace.

**NVENC com fallback.**
Se o encode por GPU falha, o mesmo comando roda com libx264. Decodificar com `-hwaccel cuda` e aplicar filtros de CPU (ass, overlay, drawtext, zoompan) quebra o grafo, a menos que haja um `hwdownload` explícito.

**Imagem de overlay pede `-loop 1`; fonte infinita pede `-t`.**
Sem `-loop 1`, a imagem não tem quadros para animar. Uma trilha em loop ou uma fonte de cor sem `-t` na saída nunca termina.

**`drawbox` e `drawtext` só animam com `eval=frame`.**
O padrão avalia a expressão uma vez só, e a animação parece travada, sem erro.

**Quantize a duração na grade de quadros.**
Com `-t` cru, o vídeo fecha em quadros inteiros e o áudio na duração exata. O concat avança pelo áudio e segura o último quadro, travando de 20 a 80 ms em cada emenda. A receita é `round(dur*fps)/fps`, com `-frames:v` no vídeo e `atrim` no mesmo instante.

**Um timebase em milissegundos duplica quadros na emenda.**
Juntar planos com `-c:v copy` herdou tempos repetidos. O certo é um arquivo de lista com a `duration` exata de cada plano e reencode com `setpts=N/(30*TB)` e timescale fixo. A conferência compara o número de quadros entre a base e o final.

**Uma fonte de celular com timestamps variáveis deriva no `trim` por segundos.**
Aplique `fps=30` na fonte antes de dividir e corte por quadro, com o `atrim` nos mesmos instantes já quantizados.

**O filtro `fps` descarta o quadro 0 quando o vídeo começa em pts > 0.**
Sem `setpts=PTS-STARTPTS`, todos os instantes amostrados saíam deslocados.

**Em MKV com PCM, a duração da trilha de vídeo volta `N/A`.**
A conta certa muda com o formato: em MP4 com AAC, o padding infla o container, e em MKV com PCM vale o container. Para achar a emenda do áudio, some amostras decodificadas, e não a duração declarada nem a duração do vídeo: a diferença chegou a 0,13 s.

**Uma etapa que morre dentro de um pipe morre calada.**
Um `| tail` engoliu o traceback, e a etapa seguinte queimou a legenda do build anterior. Confira o código de saída, não o texto.

**Uma edição por script confere depois de gravar.**
Um `str.replace` sem correspondência (por um acento) não reclamou, e o script disse "atualizada" sem gravar nada.

**Use a lista do concat com barra normal e escape do apóstrofo.**
Uma barra invertida ou um apóstrofo no caminho quebram o parser da lista sem erro claro.

**Caminhos com mais de 240 caracteres quebram o binário.**
O erro é um genérico "No such file or directory". Trabalhe numa pasta curta, copie só o entregável final e falhe cedo com uma mensagem clara.

**Force UTF-8 em todo o processo.**
O console em cp1252 derrubava lotes longos no `print` de texto acentuado, com um traceback que parecia falha do ffmpeg.

**O `hallucination_silence_threshold` descartou fala real.**
Com cerca de 2 s de silêncio inicial, o faster-whisper descartou a primeira frase legítima, e o corte de silêncio removeu fala. Ficaram `condition_on_previous_text=False` (contra o loop de repetição em takes longos), `temperature=0` e checagens posteriores: caracteres por segundo plausíveis e trigrama repetido 3 ou mais vezes.

**Uma coordenada negativa num desenho ASS (`\p1`) cai fora do lugar.**
O libass alinha o desenho pela origem. Use coordenadas ≥ 0.

**Escape `{`, `}` e `\` no texto ASS e salve com BOM UTF-8.**
Sem isso, a acentuação corrompe ou o texto vira uma tag.

**A legenda vem por último no grafo.**
Um overlay aplicado depois tapa a legenda sem erro.

**Não alinhe mais de ~20 s por chamada nem carregue dois modelos grandes no mesmo processo.**
Isso esgotou a memória virtual e derrubou renders.

---

## 7. Decisões de produto

**A duração é alvo, não teto.**
A duração recomendada para cada tipo de peça é informação para quem decide, e não critério de reprovação. A coerência vence o tempo. Acima de um limite, a peça sai em partes.

**Improviso não é defeito: o roteiro é intenção.**
Divergência entre fala e roteiro não é motivo de corte. Quem fala acrescenta artigos e pronomes, e cortar isso mutila a frase. Perder conteúdo para fugir de um improviso custa mais do que incluir fala a mais.

**Uma acusação sobre o corte se confirma no bruto, não no roteiro.**
Se as palavras que faltam não existem em take nenhum, não houve corte. A comparação usa folga (60% das palavras numa mesma janela), porque quem fala parafraseia. Quase todo falso positivo do portão veio de comparar a peça com o roteiro e tirar uma conclusão sobre o corte.

**Gaguejo só é defeito se existir no bruto.**
Um "gaguejo" acusado na transcrição do corte era invenção do ASR. Duração não prova nada; a prova é o par existir no bruto.

**Erro de gravação não descarta o começo.**
Quando quem fala se perde e recomeça, o pipeline compara as repetições de cada frase, dentro do take e entre takes, e escolhe a melhor versão de cada trecho. "Fica sempre o último take" está errado.

**A gravação não vira protocolo mecânico.**
Achar a retomada e escolher o melhor trecho é trabalho do pipeline, e não de quem grava.

**Faça a triagem do material antes de editar.**
Extraia de 4 a 6 frames e olhe. Se já existe uma legenda queimada que cabe no corte, ela é herdada e nunca duplicada. Se há texto queimado que o enquadramento pedido mutilaria, troque de técnica.

**Mostre o trade-off antes de renderizar.**
Quando nenhuma opção preserva tudo, mostre um frame de exemplo com uma recomendação, e a decisão é humana. A entrega reprovada que originou esta regra tinha legenda nova sobre legenda existente e um crop que cortou texto, sem aviso.

**Quando o editor decide, sobrar é melhor que faltar.**
Uma frase completa com uma palavra a mais custa menos do que conteúdo perdido. O que fica de fora é listado no plano com o motivo.

**Uma peça publicada não se reprocessa.**
O diagnóstico de uma peça publicada serve de aprendizado, e não de fila de refação. O conserto vale para o que ainda não saiu.

**Um recurso dominante por vídeo.**
O tipo de peça decide o recurso: corte multi-cenário para uma tese, inserção ou tela dividida para mostrar uma prova, plano único para um relato pessoal. Cabe no máximo um secundário. Ritmo é sensação, não meta: não se picota fala para atingir um número de cortes por minuto.

**Música, legenda e corte rápido não consertam trecho fraco.**
Um defeito se resolve na camada em que nasceu.

---

## 8. Processo com humano no circuito

**Plano aprovável antes de qualquer render.**
O fluxo é: transcrição, régua fonética, decisão de corte num EDL JSON, plano mostrado ao revisor (trechos, durações, o que sai e por quê), preview, portão, aprovação e só então o final. Pedidos triviais também imprimem o plano antes.

**Valide o plano antes de gastar render.**
Invariantes (segmento só sobre fala medida, borda fora do span de palavra, sem repetição no texto final, linha do tempo recomputada igual à gravada, sem movimento em silêncio incluído) podem rodar sobre o JSON em segundos. Uma peça reprovada passa a custar segundos, e não o render inteiro de minutos.

**Uma linha do tempo, uma função.**
Cópias diferentes do remapeamento do cutmap para a saída divergiam. Uma função única deve servir ao validador, ao render, ao portão e à conferência.

**O julgamento humano entra por override declarado.**
Forçar um take, vetar um trecho ou aceitar uma emenda seca fica registrado por peça, lido antes da decisão, listado no relatório e validado como qualquer outra escolha. Nenhum julgamento pontual vira regra em código.

**Uma frase faltante é entrega, não falha.**
Quando o take não tem execução limpa de uma frase, a saída legítima é a frase marcada como faltante, com o motivo, numa lista de reescuta.

**Escada de qualidade: rascunho, preview e final.**
A iteração acontece no preview. O final só roda nas peças aprovadas.

**Staging e carimbo de aprovação.**
Um render interrompido gera um MP4 sem `moov` que "existe e tem mais de 1 MB". A queima vai para um arquivo temporário, o portão roda, e só então um `os.replace` promove a peça para a pasta final, com um carimbo ao lado contendo o hash do plano e as medições.

**Lote com manifest e retomada idempotente.**
Rodar de novo pula o que já passou. O manifest e o cache são gravados de forma atômica, os nomes de saída não colidem entre extensões diferentes do mesmo arquivo, e o orquestrador distingue reprovação de quebra pelo código de saída.

**Lote: transcreva primeiro, decida depois e renderize por último.**
A etapa de análise roda com cache, os EDLs são escritos e nunca sobrescritos, o preview é amostrado por QA e o final roda só nos aprovados.

**Entrega incremental.**
Cada peça é avisada ao ficar pronta, testada e auditada, e não num lote no fim.

**Nada se apaga antes do aprovado final.**
Bruto, base sem música e intermediários ficam até a aprovação. As entradas do portão ficam mesmo depois dela.

**Um script de acabamento por etapa.**
Separar legenda, queima, cartela, música e limitador permite refazer só a etapa que mudou.

**Todo defeito novo vira regra mais teste, não conserto pontual.**
Um caso novo entra no corpus de regressão antes de qualquer mudança no planejador, e o corpus roda em nível de plano, em segundos e sem render.
