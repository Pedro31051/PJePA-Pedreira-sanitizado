# Baseline das 9 superferramentas

Data: 28/07/2026  
Servidor: PJePA Pedreira  
SDK: MCP Python 1.28.1  
Protocolo observado: MCP 2025-06-18

## Catálogo publicado

1. `analisar_processo_completo_pje`
2. `status_e_auditoria_pje`
3. `painel_e_prazos_pje`
4. `buscar_processos_pje`
5. `analisar_processo_pje`
6. `gerir_documentos_pje`
7. `download_e_cache_pje`
8. `producao_minutas_e_relatorios`
9. `auditar_fluxo_processual_pje`

Não existe ferramenta pública de protocolo.

## Contrato estruturado

As nove ferramentas publicam `outputSchema`. O FastMCP 1.28.1 gera o
seguinte contrato para retornos `Dict[str, Any]`:

```json
{
  "type": "object",
  "properties": {
    "result": {
      "type": "object",
      "additionalProperties": true
    }
  },
  "required": ["result"]
}
```

Cada chamada preserva simultaneamente:

- `result.content[0].type == "text"`, contendo o JSON legado;
- `result.structuredContent.result`, contendo o objeto nativo;
- igualdade semântica entre os dois payloads.

Não foi introduzido envelope de negócio comum. Busca, auditoria, acervo,
download e dossiê mantêm seus contratos especializados.

## Verificação local

```text
109 passed
2 skipped
33 subtests passed
4 warnings de depreciação do lxml
```

Os testes de contrato verificam:

- exatamente nove ferramentas;
- `outputSchema` em todas;
- annotations conservadoras;
- equivalência entre payload textual e estruturado em chamadas offline seguras.

## Verificação do endpoint

Após reinício de `pjepa-mcp.service`:

- unidade ativa;
- nove ferramentas publicadas;
- nove `outputSchema`;
- chamada `status_e_auditoria_pje(acao="capacidades", filtro="prazo")`
  retornou sem erro;
- `structuredContent.result` foi idêntico ao JSON textual.

Nenhuma sincronização, abertura de autos, download ou alteração processual foi
executada nesta validação.

## Evidência do caderno

A consulta dirigida ao caderno `mcp pje` recomendou preservar os payloads
especializados e habilitar a entrega estruturada sem inventar um envelope
comum. O runtime acrescentou uma restrição não explícita nas fontes:
`structured_output=True` rejeita retorno anotado apenas como `dict`; o tipo
precisa ser serializável, e `Dict[str, Any]` reproduz o contrato já usado por
`painel_e_prazos_pje`.

