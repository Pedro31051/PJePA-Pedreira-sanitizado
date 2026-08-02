# Análise processual efêmera

O fluxo novo usa o PDF integral gerado pela própria interface do PJe como fonte
primária. Ele é executado por `analisar_processo_completo_pje` e não substitui
nem apaga caches históricos já existentes.

## Ciclo

1. `preparar_pdf_integral`: cria uma cápsula opaca, lista a árvore completa,
   baixa o PDF nativo em ordem crescente e agenda OCR/segmentação local.
2. `status_pdf_integral`: informa apenas estado e contagens, sem conteúdo.
3. `resultado_pdf_integral`: entrega manifesto e lotes textuais somente se todas
   as páginas e todos os IDs estiverem cobertos, sem truncamento.
4. `confirmar_descarte`: após a confirmação de entrega, apaga a cápsula e
   verifica que o diretório deixou de existir.

Em treinamento, use `modo_dados=training`. A ação
`registrar_resultado_teste` com `teste_aprovado=true` sempre apaga a cápsula
imediatamente. Falhas podem permanecer por no máximo 24 horas para diagnóstico;
o watchdog remove cápsulas expiradas.

## Agente sobre a cápsula (orquestração efêmera)

`iniciar_agente` com o `job_id` do preparo efêmero roda o orquestrador Vertex
diretamente sobre o manifesto da cápsula, sem tocar o banco persistente:

- cabendo no orçamento de contexto, uma única chamada cobre o processo;
- excedendo, cada shard local vira uma chamada de operário e os achados
  verificados são consolidados com `finding_id` prefixado pelo shard;
- o resultado é gravado apenas em `agent/result.json` dentro da cápsula e
  morre com ela no descarte (`confirmar_descarte` ou teste aprovado);
- `status_agente`, `resultado_agente` e `explicar_agente` aceitam o
  `agent_run_id` efêmero; o status expõe somente contagens.

## Aterramento literal

Antes da verificação fail-closed, o verificador tenta recuperar cada prova
pelo índice local (`evidence_grounding` no MCP; `app/grounding.py` no agente):
hash alterado é corrigido pelo hash local, página trocada é reapontada e
paráfrase é substituída pelo fragmento literal mais próximo da própria página
(limiar de similaridade 0.72). Nenhum texto novo entra: toda substituição sai
literalmente das páginas extraídas; prova irrecuperável continua sendo
rejeitada. As contagens ficam em `verification.literal_grounding`.

## Retenção zero nos caminhos legados

Com `PJE_ZERO_RETENTION=1`, as escritas legadas que retêm dados processuais
por dias falham em modo fechado apontando este fluxo efêmero: job/resultado da
análise completa, cache cifrado de peças, dossiê de auditoria, execução
persistente do agente, lotes, exportação de acervo e downloads persistentes.
Leituras de dados já persistidos continuam valendo até a expiração natural.

## Limites de segurança

- PDF, imagem e captura nunca seguem para o Vertex AI.
- OCR usa Tesseract/PyMuPDF localmente; o PDF derivado é marcado como cópia de
  análise e não preserva a validade das assinaturas do original.
- ID de documento e ID de assinatura são tratados separadamente. Associação
  incerta interrompe o fluxo.
- Sessões ADK são somente em memória no modo padrão de zero retenção.
- Dossiê incompleto, página vazia, peça ausente ou contexto truncado produz zero
  chamadas ao modelo.
- O recibo de descarte contém apenas identificador opaco, horário, motivo e
  contagem de arquivos removidos.
