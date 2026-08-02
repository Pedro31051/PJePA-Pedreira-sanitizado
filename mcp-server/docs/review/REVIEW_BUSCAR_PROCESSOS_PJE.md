# Revisão incremental — buscar_processos_pje

Data: 2026-07-28
Estado: implementado e validado offline

## Escopo

Revisão da ação `lote` da superferramenta pública
`buscar_processos_pje`, sem alterar o catálogo de nove ferramentas nem o
transporte Streamable HTTP stateless.

## Achados confirmados no código

1. Toda resposta sem CNJ e sem campo `erro` era classificada como
   `sem_resultado`, mesmo quando o PJe não havia confirmado o vazio.
2. O agregador devolvia o critério bruto no campo `valor`.
3. Valores brutos também podiam reaparecer em `resultado.valor_busca`,
   `coincidencias` e `itens_ignorados`.

## Mudanças implementadas

- `sem_resultado` agora exige `sem_resultado_confirmado=true`.
- Retorno vazio sem confirmação recebe:
  - `status="inconclusivo"`;
  - `inconclusive=true`;
  - `sem_resultado_confirmado=false`.
- O resumo do lote inclui a contagem `inconclusivos`.
- `valor_mascarado` é o campo canônico; o alias de transição `valor` contém
  exatamente o mesmo valor mascarado, nunca o critério bruto.
- Ecos do valor original são redigidos recursivamente no resultado agregado.
- Coincidências armazenam somente o valor mascarado.
- Excedentes são retornados em `itens_ignorados_mascarados`.
- CPF, CNPJ, OAB e nomes usam o mascaramento já centralizado em
  `mapa_pje.mask_process_search_value`.

## Compatibilidade

- Nome e assinatura da ferramenta pública foram preservados.
- O retorno continua sendo `Dict[str, Any]` e mantém
  `structuredContent.result`.
- Consumidores do campo `valor` continuam funcionando sem receber o dado
  bruto e podem migrar gradualmente para `valor_mascarado`.
- Nenhuma chamada real ao PJe foi necessária.

## Testes adicionados

1. Vazio explicitamente confirmado resulta em `sem_resultado`.
2. Vazio não confirmado resulta em `inconclusivo`.
3. CPF bruto não aparece no item nem no resultado aninhado.
4. OAB excedente não aparece em `itens_ignorados`.
5. Lotes com 30 entradas despacham somente as primeiras 25.
6. CNJ com DV inválido é recusado sem criar sessão de navegador.

## Resultado da suíte

```text
119 passed
2 skipped
33 subtests passed
4 warnings de depreciação do lxml
```

## Critérios de aceite

- Vazio inconclusivo nunca é apresentado como ausência de processo.
- Nenhum critério bruto do lote aparece no payload agregado.
- Catálogo público permanece com nove ferramentas.
- Testes de contrato e suíte completa permanecem verdes.

## Rollback

Reverter somente as funções auxiliares de redação e a projeção do item dentro
de `buscar_em_lote`, além dos dois testes correspondentes. Não há migração de
banco, alteração de cache nem efeito externo a desfazer.
