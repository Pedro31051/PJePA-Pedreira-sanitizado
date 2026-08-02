# Padrão de relatório e sugestões da análise completa

Contrato: `pje.relatorio-processual/v1` — implementado em
`mcp-server/src/report_format.py` e devolvido por `resultado_agente` nos campos
`relatorio` (estrutura) e `relatorio_markdown` (renderização determinística).
O renderizador só reorganiza conteúdo já aprovado pelo verificador: não cria
fato, prova, nem sugestão nova.

## Princípios obrigatórios

1. **Prova literal em tudo**: cada achado publicado cita ao menos uma prova no
   formato `[doc <document_id> p.<página> sha256:<8 primeiros dígitos>]`,
   verificada localmente (documento, página, trecho literal e hash).
2. **Separação de camadas**: fato provado (seções de achados) ≠ interpretação
   (síntese executiva/estado atual) ≠ recomendação (sugestões). A síntese vem
   marcada como interpretação assistida.
3. **Sugestões nunca são executadas**: todo item de sugestão carrega
   `execucao_automatica: false` e `exige_conferencia_humana: true`.
4. **Fail-closed visível**: a seção Verificação publica contagens de achados
   verificados/rejeitados e do aterramento literal; sugestões rejeitadas pelo
   revisor determinístico aparecem em `sugestoes_rejeitadas` com o motivo.
5. **Lacunas explícitas**: o que não pôde ser analisado entra em
   `desconhecidos`/`questoes_abertas`, nunca é completado por inferência.

## Estrutura (ordem fixa)

| Campo / seção | Conteúdo |
|---|---|
| `sintese_executiva` | Narrativa do especialista consolidador (ou o resumo bruto verificado, quando a síntese falhou — ver `sintese_status`). |
| `estado_atual` | Fase processual atual, derivada só de achados verificados. |
| `atos_e_fatos` | Achados das categorias fato, pedido, defesa, decisão e prova. |
| `contradicoes` | Achados de contradição (cada um com ≥1 prova; no contrato ADK, ≥2). |
| `pendencias` | Pendências identificadas (fiscais, documentais, processuais). |
| `proximos_atos` | Próximos atos possíveis identificados nos autos. |
| `desconhecidos` | Lacunas declaradas pelo modelo e mantidas pelo verificador. |
| `questoes_abertas` | Questões da síntese sem resposta nos autos. |
| `sugestoes` | Ver formato abaixo. |
| `sugestoes_rejeitadas` | Sugestões descartadas pelo revisor, com motivo. |
| `verificacao` | Contagens fail-closed + `literal_grounding`. |
| `avisos` | Avisos padrão fixos (somente leitura, conferência humana etc.). |

Cada achado: `{finding_id, categoria, titulo, conclusao, ressalva, provas[]}`.

## Formato de sugestão

```json
{
  "item": "Intimar o Ministério Público.",
  "fundamento": "Há interesse de incapaz (CPC art. 178, II).",
  "prioridade": "alta | media | baixa",
  "achados_suporte": ["full:f-1"],
  "execucao_automatica": false,
  "exige_conferencia_humana": true
}
```

Regras: verbo no infinitivo; `achados_suporte` deve conter apenas
`finding_id` verificados (o revisor determinístico descarta o restante);
fundamento normativo, quando houver, deve citar fonte oficial.

## Pipeline hierárquico que alimenta o relatório

1. Operários por shard (paralelos) analisam lotes locais e devolvem achados.
2. O verificador local aterra e confirma cada prova (fail-closed).
3. O especialista consolidador recebe **somente** os achados verificados
   (nunca as páginas do processo) e produz síntese + sugestões.
4. O revisor determinístico descarta sugestão sem `finding_id` de suporte.
5. `report_format` monta o contrato e o Markdown.

## Markdown

Seções na ordem: Síntese executiva, Estado atual, Atos e fatos verificados,
Contradições, Pendências, Próximos atos identificados, Lacunas e
desconhecidos, Sugestões (não executadas automaticamente), Verificação,
Avisos. Seção sem conteúdo declara "Nenhum item verificado nesta seção." em
vez de ser omitida.
