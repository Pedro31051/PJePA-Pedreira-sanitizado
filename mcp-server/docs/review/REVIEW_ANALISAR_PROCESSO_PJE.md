# Revisão incremental — analisar_processo_pje

Data: 2026-07-28
Estado: implementado e validado offline

## Escopo

Revisão da superferramenta pública `analisar_processo_pje`, preservando o
catálogo de nove ferramentas, o transporte Streamable HTTP stateless e a
fronteira estritamente consultiva.

## Achados confirmados

1. O dispatcher verificava apenas o comprimento do CNJ; um número com 20
   dígitos e DV inválido ainda podia abrir o navegador.
2. `diagnostico_saude` e `estrategia` carregavam o relatório do mesmo processo
   novamente ao calcular a linha do tempo.
3. As análises derivadas não propagavam um indicador explícito de completude.
4. O extrator de audiências possuía quatro baldes, mas não publicava a prova de
   fechamento matemático dos contadores.
5. A comparação aceitava CNJs duplicados e verificava somente o comprimento,
   não o DV.

## Mudanças implementadas

- Validação completa de formato e DV antes do dispatcher abrir o navegador.
- Erros de entrada retornam CNJ redigido, nunca o número bruto.
- Comparação rejeita offline:
  - menos de dois ou mais de dez processos;
  - CNJs duplicados;
  - formato ou DV inválido.
- `diagnostico_saude` e `estrategia` carregam o relatório uma única vez e o
  reutilizam no resumo e na linha do tempo.
- As duas análises publicam:
  - cobertura do resumo;
  - confiabilidade da linha do tempo;
  - percentual de datas reconhecidas.
- Audiências publicam contadores de futuras, passadas, canceladas e sem data,
  além de `contadores_fecham`.
- A ação `audiencias` publica indicador explícito de completude da fonte.

## Testes adicionados

1. CNJ com DV inválido é recusado sem `get_cliente`.
2. Comparação duplicada é recusada sem navegador e sem eco do CNJ bruto.
3. `diagnostico_saude` consulta `relatorio_processo` exatamente uma vez.
4. O diagnóstico publica completude e cobertura de datas.
5. Massa sintética com uma audiência futura, duas passadas e uma cancelada
   fecha exatamente quatro menções.

## Resultado da suíte

```text
123 passed
2 skipped
33 subtests passed
4 warnings de depreciação do lxml
```

## Critérios de aceite verificados

- Validação de CNJ ocorre antes de qualquer navegador.
- Comparação permanece limitada a dez processos.
- Movimentações permanecem limitadas à faixa 1..200.
- O relatório do processo é reutilizado dentro da mesma análise composta.
- Recomendações e score continuam acompanhados de fatos de suporte.
- Completude é explícita.
- Toda menção de audiência pertence a exatamente um balde.
- Nenhuma capacidade de assinatura, protocolo ou movimentação foi criada.

## Limite desta etapa

Nenhum canário real foi executado. A validação usou clientes simulados e dados
sintéticos, sem acesso ao PJe.

## Rollback

Reverter apenas os parâmetros privados de reutilização de relatório, a
validação reforçada do dispatcher, os campos de completude/contadores e os
testes correspondentes. Não houve migração de banco nem alteração de dados.
