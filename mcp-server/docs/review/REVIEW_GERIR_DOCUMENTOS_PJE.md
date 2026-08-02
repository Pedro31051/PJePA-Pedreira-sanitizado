# Revisão incremental — gerir_documentos_pje

Data: 2026-07-28
Estado: implementado e validado offline

## Estado confirmado antes da mudança

- O lazy-load já exigia cinco ciclos estáveis de 400 ms.
- O timeout da árvore já era de 30 segundos.
- Título, tipo, data e ID já eram preservados.
- A aba dos autos já era fechada em `finally`.
- A pesquisa profunda já usava leitura em lote com uma aba por processo,
  orçamento total e propagação de falhas.

Esses mecanismos foram preservados.

## Lacunas corrigidas

- Formato e DV do CNJ agora são validados antes do navegador.
- Erros de CNJ retornam somente número redigido.
- Limites são validados offline:
  - `max_paginas`: 1..500;
  - `max_documentos`: 1..100;
  - `tempo_maximo_segundos`: 10..50;
  - `limite`: 0..500.
- `termo_busca` é limitado a 200 caracteres.
- `id_doc` aceita somente identificador numérico.
- A parcialidade é fail-closed: quando a fonte não declara
  `arvore_completa=true`, ações derivadas retornam:
  - `arvore_completa=false`;
  - `status=partial`;
  - aviso para tratar totais como piso.

## Testes adicionados

1. DV inválido não chama `get_cliente`.
2. Todos os limites inválidos falham antes do navegador.
3. Termo excessivo e `id_doc` inseguro falham offline.
4. Ausência da flag de completude propaga parcialidade transitiva.

## Resultado

```text
126 passed
2 skipped
39 subtests passed
4 warnings de depreciação do lxml
```

## Limites remanescentes

- Não foi executado canário real.
- `historico_decisorio(incluir_teor=true)` continua lendo atos
  sequencialmente; deve ser validado no ciclo de vida real antes de qualquer
  alteração de estratégia.
- Testes de árvore com 500 peças dependem de massa HTML sintética adicional.

## Rollback

Reverter somente a validação do dispatcher, a regra fail-closed de
`_propagar_arvore` e os testes correspondentes. Não houve migração nem efeito
externo.
