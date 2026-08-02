# Auditoria das caixas/tarefas e arquitetura de análise massiva

Data da validação: 27/07/2026  
Escopo validado: PJe-TJPA 1º grau, Vara de Família, Sucessões e Registros
Públicos de Marabá, perfil Diretor de Secretaria.

## Resultado executivo

A listagem antiga de processos por tarefa não consultava as tarefas internas:
ela chamava `expedientes_pendentes()` e tratava expedientes como se fossem
processos de caixa. Isso podia produzir resultados plausíveis, porém
incompletos e semanticamente incorretos.

A fonte foi substituída pelo endpoint autenticado consumido pelo próprio
Cliente Web do PJe. O índice de caixas ainda é lido semanticamente do painel
para respeitar exatamente o papel e a localização ativos; cada caixa é então
obtida pela API e persistida em SQLite.

Validação real pela URL MCP:

| Verificação | Resultado |
|---|---:|
| Lotação selecionada | Vara de Família, Sucessões e Registros Públicos de Marabá |
| Órgão julgador retornado | ID 916, somente a Vara de Família |
| Caixas/tarefas não vazias visíveis | 55 |
| Ocorrências declaradas pelo painel | 8.503 |
| Ocorrências declaradas pela API | 8.503 |
| Ocorrências efetivamente persistidas | 8.503 |
| Processos únicos | 8.498 |
| Caixas divergentes | 0 |
| Erros | 0 |
| Cobertura | 100% |
| Tempo observado | 24,6 s |
| Integridade SQLite | `ok` |
| Descritores SQLite após a coleta | 0 |

O total é de ocorrências, não de CNJs únicos. Um processo que estiver em mais
de uma tarefa é preservado em cada ocorrência; deduplicá-lo silenciosamente
apagaria estado operacional. Entre duas leituras reais o total mudou de 8.504
para 8.503. O snapshot imutável é necessário porque o acervo é vivo.

## Contrato de completude

Um snapshot só recebe `status=completo` quando, para todas as caixas:

1. a contagem exibida no painel é igual ao `count` da API;
2. o `count` é igual ao número de entidades recebido;
3. não existe chave de ocorrência duplicada;
4. nenhuma caixa terminou com erro.

Qualquer diferença produz `status=incompleto`, identifica a caixa e conserva
as quantidades conflitantes. Não existe arredondamento nem perda silenciosa.

O escopo é sempre registrado com:

- lotação solicitada e lotação efetivamente selecionada;
- órgão julgador e ID encontrados nos registros;
- papel/localização ativos;
- data do snapshot;
- fonte do índice e fonte dos processos.

O PJe só mostra no agrupamento **Tarefas** as tarefas que possuem processo
pendente. Logo, as 55 caixas são todas as tarefas não vazias visíveis para
essa combinação de usuário, papel e localização; tarefas vazias não são
materializadas pelo painel.

## Metadados preservados

Cada ocorrência possui colunas pesquisáveis e o JSON integral de origem.
Campos normalizados:

- tarefa e IDs de task instance atual/próxima;
- ID interno e número CNJ;
- classe, assunto, órgão julgador e cargo;
- polos ativo e passivo;
- entrada na tarefa e último movimento, em epoch e ISO-8601;
- descrição do último movimento;
- sigilo, prioridade, conferência e indicador de pessoa em situação de rua;
- etiquetas completas;
- todos os campos futuros/desconhecidos em `metadados_origem_json`.

O banco fica em
`$PJE_STORAGE_DIR/inventario/caixas_tarefas.sqlite3`, com modo `0600`, WAL,
índices por tarefa/CNJ/processo e transações atômicas por caixa.

## Ações MCP

Na ferramenta `painel_e_prazos_pje`:

- `sincronizar_caixas`: seleciona opcionalmente `lotacao`, coleta tudo e
  devolve prova de cobertura;
- `listar_caixas`: lê o snapshot em disco, sem navegador;
- `listar_processos_caixa`: consulta paginada por tarefa e termo;
- `diagnosticar_caixas`: lista controles e perfis/lotações disponíveis.
- `consultar_acervo_estruturado`: devolve o contrato versionado
  `pje.acervo-tarefas/v1`, com ocorrência, processo, polos, unidade, fluxo,
  datas, indicadores, etiquetas, capacidades em lote e facetas;
- `schema_acervo_tarefas`: descreve os campos, filtros, ordenações e limites
  de completude sem carregar registros.

Parâmetros relevantes:

- `lotacao="Vara de Família"` evita inventariar a unidade errada;
- `concorrencia=1..8`, com redução automática para acervos muito grandes;
- `max_retentativas=1..5`;
- `snapshot_id`, `nome_tarefa`, `termo_busca`, `pagina`,
  `itens_por_pagina` (máximo 500);
- `incluir_metadados_origem=true` quando o agente precisar do JSON integral.

### Contrato estruturado para interfaces

O contrato `pje.acervo-tarefas/v1` não concatena polos. `poloAtivo` e
`poloPassivo` viram listas distintas dentro de `partes.polos`, e cada entrada
conserva o papel, o indicador de parte principal e a fonte. Como o endpoint da
fila expõe somente uma string principal por polo, o bloco de completude marca
explicitamente `partes_completas=false` e `advogados_incluidos=false`.

A classe é separada em `sigla_pje`, `codigo_tpu`, `descricao_completa`,
`exibicao` e `resolucao`. O endpoint observado normalmente fornece somente a
sigla; nesse caso descrição e código ficam nulos, com
`resolucao=somente_sigla_fornecida_pelo_pje`. Não há expansão heurística:
descrições por extenso só entram quando fornecidas pelo PJe ou por futuro
catálogo oficial versionado.

Campos raros da origem também são estruturados: responsável da tarefa,
lembretes e as seis capacidades de ação em lote. No snapshot real, o PJe
serializou as capacidades exatamente uma vez por tarefa; a consulta as
propaga aos registros da mesma tarefa com `escopo=tarefa` e resolução explícita
na proveniência. Campos ainda desconhecidos ficam em
`campos_extras_origem`, evitando perda quando o PJe evoluir.

As facetas são calculadas sobre todo o resultado filtrado antes da paginação e
incluem tarefas, classes, assuntos, órgãos, etiquetas, indicadores e faixas de
permanência. A idade da fila usa a data final do snapshot como referência, o
que torna consultas históricas determinísticas.

## Robustez e escala

A coleta usa uma fila limitada de trabalhadores. Cada caixa é persistida assim
que chega e seu JSON é liberado, em vez de acumular todas as caixas em RAM.
Para uma caixa individual, o endpoint atual do PJe devolve todas as entidades
em uma resposta; portanto, o piso de memória ainda é o tamanho da maior caixa.
A concorrência é reduzida automaticamente quando a maior caixa ou o total
indicar risco de memória.

Respostas HTTP do Playwright são explicitamente descartadas. Um teste de
regressão executa 100 consultas SQLite repetidas e verifica que o número de
descritores não aumenta. O serviço permanece com `LimitNOFILE=65535`, mas
esse limite é somente proteção: não substitui fechamento correto.

## Arquitetura recomendada para centenas de milhares de documentos

Não se deve colocar milhares de PDFs no contexto do modelo nem pedir para um
agente “ler tudo” a cada pergunta. A arquitetura deve separar ingestão,
recuperação e julgamento:

1. **Manifesto dos autos**: listar documentos e anexos, com ID PJe, tipo,
   data, autor, sigilo, ordem, tamanho e hash. O manifesto decide o que mudou;
   bytes já processados não são relidos.
2. **Extração em cascata**: texto nativo primeiro; OCR apenas nas páginas sem
   camada textual; visão somente em páginas com tabela, manuscrito, carimbo ou
   diagrama. Cada página recebe status e confiança.
3. **Proveniência obrigatória**: todo trecho guarda CNJ, ID do documento,
   nome da peça, página, intervalo de caracteres, hash da fonte e versão do
   extrator. Uma resposta jurídica sem documento+página não passa.
4. **Índice híbrido**: SQL para metadados, busca lexical/BM25 para números,
   nomes e expressões legais, busca vetorial para semântica e reranking para
   formar um conjunto pequeno de evidências.
5. **Consulta em duas fases**: primeiro filtrar processos/peças; depois buscar
   trechos dentro do conjunto. Só então o modelo sintetiza.
6. **Map-reduce jurídico**: análise estruturada por peça/processo em jobs
   idempotentes; consolidação posterior por tema, prazo, pedido, prova e
   contradição. Resultados intermediários também ficam versionados.
7. **Controle de qualidade**: cobertura de documentos e páginas, taxa de OCR,
   páginas falhas, recall@k, precisão de citações e teste de perguntas
   negativas (“não encontrado”), com amostra humana estratificada.

O File Search da OpenAI pode compor essa camada, com lotes assíncronos,
atributos por arquivo e filtros de metadados. Porém, para centenas de milhares
de peças, o catálogo local deve continuar sendo a fonte de verdade e os
vector stores devem ser particionados por unidade/período/processo. A API
permite atributos estruturados e estratégia de chunking; o padrão documentado
é 800 tokens com sobreposição de 400, ajustável. Os objetos de vector store e
arquivos persistem até exclusão, o que exige decisão de retenção compatível
com sigilo e política institucional.

## Fontes oficiais

- [Manual do usuário interno do PJe](https://docs.pje.jus.br/manuais-de-uso/Manual%20do%20usuario%20interno/):
  tarefas e contagens dependem do papel e da localização; o painel afirma
  exibir todos os processos da tarefa.
- [Regra de interface RI193](https://docs.pje.jus.br/configura%C3%A7%C3%B5es-do-pje/Regras%20de%20interface/#ri193):
  exige grid com todos os processos, órgão, classe, CNJ, assunto, partes,
  data, último movimento, pendência e total encontrado.
- [Notas do Cliente Web PJe](https://docs.pje.jus.br/servicos-negociais/servico-pje-legacy/cliente-web/notas-da-versao/):
  versão 2.5.0 em 02/07/2026; versões recentes alteraram filtros, paginação e
  download da lista de tarefas.
- [OpenAI Vector Store File Batches](https://developers.openai.com/api/reference/resources/vector_stores/subresources/file_batches):
  ingestão assíncrona com contagens de concluídos, falhos e em andamento.
- [OpenAI Vector Store Files](https://platform.openai.com/docs/api-reference/vector-stores-files):
  atributos pesquisáveis e estratégia de chunking.
- [Controles de dados da OpenAI](https://developers.openai.com/api/docs/guides/your-data#default-usage-policies-by-endpoint):
  retenção e elegibilidade ZDR variam por endpoint; vector stores e files
  mantêm estado até exclusão.

## Próxima etapa

O inventário de caixas está resolvido. A próxima entrega deve ser o
**manifesto incremental de documentos por processo**, sem baixar peças para
devolução ao usuário: streaming para hash/extração/indexação, checkpoint por
documento e relatório de cobertura por processo. Só depois deve entrar a
camada de pergunta e resposta jurídica com citações verificáveis.
