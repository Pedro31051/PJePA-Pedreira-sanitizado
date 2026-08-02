# Análise processual completa

`analisar_processo_completo_pje` executa inventário, triagem rápida ou leitura
integral de um processo como job persistente e estritamente somente leitura.

## Fluxo recomendado

```text
analisar_processo_completo_pje(
    acao="iniciar_rapida",
    numero_cnj="<CNJ>",
    autorizacao_leitura=true,
    autorizacao_ref="<referência da autorização>"
)
```

A resposta inicial contém `job_id` e não espera a leitura das peças. Acompanhe
com `acao="status"` e obtenha o dossiê com `acao="resultado"`.

As ações disponíveis são:

- `planejar`: consulta manifesto/progresso anterior sem abrir o PJe;
- `inventariar`: abre os autos uma vez e coleta somente capa, movimentos e
  árvore documental, sem ler o teor;
- `iniciar_rapida`: lê no máximo 12 peças prioritárias e 20 páginas por peça;
- `aprofundar`: lê todas as peças e páginas, reutilizando o cache da triagem;
- `iniciar`: interface compatível que escolhe `modo="inventario"`, `"rapida"`
  ou `"integral"` (integral por padrão);
- `status`: lê apenas SQLite;
- `resultado`: devolve dossiê completo ou parcial;
- `explicar`: filtra conclusões e respectivas citações;
- `cancelar`: solicita interrupção local, mantendo retomada;
- `reanalisar`: cria nova execução, reaplica os extratores ao cache
  criptografado e não abre o navegador quando o dossiê anterior está válido.

`resultado` e `explicar` exigem novamente a autorização explícita.

## Pipeline incremental

O fluxo recomendado para ganhar velocidade é `inventariar` →
`iniciar_rapida` → `aprofundar`. Cada execução salva um manifesto por ID e
fingerprint. Na próxima abertura, o MCP identifica peças adicionadas,
removidas, alteradas e inalteradas; texto já extraído com o mesmo fingerprint
vem do cache criptografado.

A triagem prioriza decisões, inicial, defesa, manifestações do Ministério
Público, prova técnica, comunicações, petições e certidões, com desempate por
recência. Ela lê até quatro peças concorrentemente. Procurações e anexos
administrativos recebem prioridade menor. O resultado rápido marca
`analysis_level="rapid"`, publica a seleção realizada e recomenda
`aprofundar`; ele nunca se apresenta como cobertura integral.

## Vários processos em paralelo

Para criar, preencher e iniciar o lote em uma chamada:

```text
analisar_processo_completo_pje(
    acao="analisar_varios",
    numero_cnj="<CNJ-1>, <CNJ-2>, <CNJ-3>",
    modo="rapida",
    concorrencia_processos=2,
    autorizacao_leitura=true,
    autorizacao_ref="<referência da autorização>"
)
```

Cada processo usa uma aba isolada da mesma sessão autenticada. A abertura das
abas permanece serializada, mas a coleta posterior ocorre em paralelo. O
limite padrão é dois processos e o teto defensivo é três.

O escalonador trabalha por vagas, não por ondas: com limite `N`, mantém até
`N` processos em `running`. Quando um deles termina, falha ou é cancelado, a
vaga é liberada e o próximo item `queued` entra imediatamente. `status_lote`
publica `running`, `queued`, `completed`, `failed`, `cancelled` e
`available_slots`. O modo pode ser `inventario`, `rapida` ou `integral`; cada
job conserva cache, retomada e resultado próprios.

Após um job rápido ou integral, o dossiê pode ser submetido ao agente Gemini
3.5 Flash do Vertex AI. Essa etapa é opcional e está documentada em
[AGENTE_VERTEX_PROCESSUAL.md](AGENTE_VERTEX_PROCESSUAL.md). As conclusões do
modelo só são publicadas após verificação local dos trechos e hashes.

## Garantias

- uma abertura dos autos por execução;
- manifesto documental `pje.document-manifest/v2` antes das leituras, com
  ordem, vínculo pai/anexo, origem e preservação de campos desconhecidos;
- segunda leitura leve do manifesto ao final para detectar peças novas,
  removidas ou alteradas;
- progresso persistido após cada documento;
- hash SHA-256 dos bytes originais calculado em memória antes do descarte,
  separado do hash do texto extraído;
- cache criptografado por documento, fingerprint e SHA-256;
- anexos idênticos contabilizados, mas analisados uma única vez;
- modo integral sem teto oculto de páginas;
- inventário sem leitura de conteúdo e triagem com limites explícitos;
- no máximo quatro leituras simultâneas para não sobrecarregar o PJe;
- retomada após cancelamento, falha ou reinício;
- nenhuma escrita no PJe;
- nenhum CNJ ou teor processual em texto aberto no banco;
- conclusão material acompanhada por documento, página, trecho e hash;
- caixa/tarefa não é afirmada sem snapshot atual.

O OCR local é seletivo: só é tentado em páginas sem texto nativo, quando
PyMuPDF e Tesseract estão disponíveis. Cada página registra método, status e
confiança. Ausência do motor, falha de OCR, documento truncado, árvore
incompleta ou falha isolada produzem `partial_with_gaps`, nunca uma falsa
análise completa. A análise semântica permanece separada e desabilitada até
aprovação institucional.

O dossiê publica `completeness` nas dimensões `source`, `documents`, `pages`,
`metadata` e `domain_analysis`, além de `gap_details` com código, dimensão,
severidade e mensagem. Expediente vazio só representa zero quando
`expedients.complete=true`; falha de acesso à fonte mantém os itens já obtidos
e registra `SOURCE_EXPEDIENTS_UNAVAILABLE`.

O manifesto v2 inicial ainda deriva da árvore DOM. Enquanto o cruzamento com a
fonte estruturada do PJe não comprovar vínculos e metadados, a dimensão
`metadata` permanece parcial sem impedir a entrega das demais evidências.

No serviço Linux, a chave AES-256 deve ser entregue como credencial systemd
`audit_master_key`; o drop-in está em
`deploy/systemd/pjepa-mcp-audit-credential.conf`. O segredo não pertence ao
repositório, à linha de comando do servidor nem aos logs.
