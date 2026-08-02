# Performance e contrato do acervo de tarefas

## Caminho recomendado

Para consulta rápida de listagens:

```json
{
  "acao": "consultar_acervo_estruturado",
  "formato": "compacto",
  "envelope": "minimo",
  "incluir_facetas": false,
  "campos": "tarefa,classe,dias,flags",
  "itens_por_pagina": 500
}
```

Esse caminho:

- lê um snapshot imutável;
- preserva ocorrências repetidas do mesmo CNJ em tarefas distintas;
- usa `ORDER BY` estável e índices SQLite quando não há filtros;
- devolve dicionários restritos à página;
- retorna `next_cursor` autenticado;
- publica o resultado em `structuredContent` e em texto JSON compatível.

Filtros textuais, TPU, facetas globais e metadados semânticos continuam no
caminho completo em Python para preservar normalização e completude.

## Cache lógico

Toda resposta contém `snapshot.revision`. Uma chamada posterior pode enviar
`if_revision`; se o snapshot não mudou, a resposta contém:

```json
{
  "status": "not_modified",
  "registros": []
}
```

O transporte continua sendo JSON-RPC HTTP 200; não se usa HTTP 304 para
resultado de ferramenta MCP.

## Observabilidade

`acao="metricas_desempenho_acervo"` retorna p50, p95, p99, mínimo e máximo
para:

- duração total;
- leitura SQLite;
- filtros;
- projeção;
- montagem da resposta;
- itens e bytes JSON.

As métricas permanecem em memória, limitadas às últimas 2.048 consultas, e
não registram CNJ, partes, termos de busca ou teor processual.

## Benchmark sintético

Comando:

```bash
python3 scripts/benchmark_acervo.py --ocorrencias 100000 --repeticoes 5
```

Resultado observado em 28/07/2026, neste host:

| Ocorrências | Página | p50 |
|---:|---:|---:|
| 100.000 | 1 | 1,085 ms |
| 100.000 | 100 | 2,314 ms |
| 100.000 | 500 | 7,189 ms |
| 100.000 | 5.000 | 58,974 ms |

Os números são do processo local, com massa sintética e sem transporte HTTP.
Eles não representam SLA de rede nem desempenho do PJe.

## Segurança do cursor

O cursor contém apenas versão, snapshot, próxima página e fingerprint da
consulta. A assinatura HMAC usa uma chave local exclusiva criada em
`$PJE_STORAGE_DIR/inventario/.cursor_hmac_key`, com permissão `0600`.
A chave de cursor não reutiliza a chave AES dos dossiês processuais.
