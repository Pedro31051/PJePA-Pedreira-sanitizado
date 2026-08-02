<h1 align="center">
    <img alt="MCP PJe-TJPA 1º e 2º Graus" src="docs/assets/banner.svg">
    <br>
    <small>Expedientes, prazos e autos do PJe do TJ-PA em linguagem natural — 1º e 2º graus num único servidor, sem nunca escrever no PJe</small>
</h1>

<p align="center">
    <img alt="Python" src="https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white">
    <img alt="Ferramentas" src="https://img.shields.io/badge/superferramentas-11-brightgreen">
    <img alt="Graus" src="https://img.shields.io/badge/graus-1g%20%2B%202g-blueviolet">
    <img alt="MCP" src="https://img.shields.io/badge/MCP-Claude%20Desktop-d97757">
    <img alt="Login" src="https://img.shields.io/badge/login-CPF%20%2B%20senha%20%2B%20TOTP%20autom%C3%A1tico-black">
    <img alt="Somente leitura" src="https://img.shields.io/badge/PJe-somente%20leitura-8b0000">
</p>

> **Fork local** de [fxbarros/MCP-PJe-TJMA](https://github.com/fxbarros/MCP-PJe-TJMA) adaptado para o **TJPA** (Tribunal de Justiça do Pará). O upstream não publica licença — uso pessoal; não redistribuir sem autorização do autor.

Servidor [MCP](https://modelcontextprotocol.io) que permite ao Claude consultar o **Processo Judicial Eletrônico** do Tribunal de Justiça do Pará (PJe-TJPA) — **1º e 2º graus** — em linguagem natural. Família iniciada no [MCP PJe-TJPI](https://github.com/fxbarros/MCP-PJe-TJPI), com as duas instâncias do TJPA atendidas pelo mesmo servidor.

## 🏛️ As duas instâncias

| Grau | URL | client_id (SSO PDPJ) |
|---|---|---|
| **1g** (varas) | `https://pje.tjpa.jus.br/pje` | `pje-tjpa-1g` |
| **2g** (câmaras/turmas) | `https://pje.tjpa.jus.br/pje-2g` | `pje-tjpa-2g` |

Toda ferramenta aceita o parâmetro `grau` (`"1"` padrão, `"2"` para o 2º grau — aceita também "segundo", "apelação", "câmara"...). O singleton mantém **uma** sessão de Chromium por vez, chaveada por `(persona, grau)`: trocar de grau fecha a sessão anterior e loga na outra instância.

> O login é no **SSO nacional do PDPJ** (`sso.cloud.pje.jus.br`) — as mesmas credenciais CPF + senha + TOTP valem para os dois graus (e para outros tribunais). Se você já usa o MCP PJe-TJPI neste Mac, as credenciais do Keychain são reaproveitadas automaticamente.

## ✨ Funcionalidades

- 🔐 **Login 100% automatizado**: CPF + senha + 2FA (TOTP)
- ⚖️ **1º e 2º graus** no mesmo servidor, com pastas de download separadas por grau (o mesmo nº CNJ existe nos dois graus)
- 📋 **Expedientes pendentes** e ⏰ **alertas de prazos urgentes** (com matriz de risco `analisar_risco_prazos` e dashboard BI `analisar_estatisticas_painel_expedientes`)
- 📅 **Calculadora de prazos processuais em DIAS ÚTEIS (`calcular_prazos_processuais`)**: computa prazos segundo o CPC/2015 descontando finais de semana, feriados nacionais e o recesso forense (art. 220 CPC)
- ⏱️ **Linha do tempo & análise de inatividade (`analisar_linha_do_tempo_processo`)**: calcula duração total da ação, dias sem movimentação e períodos de paralisação > 30 dias
- 🧠 **Recomendador de estratégia jurídica & classificação de decisões (`recomendar_estrategia_processual`, `classificar_teor_decisao`)**: classifica o resultado jurídico (Procedente, Improcedente, Tutela Deferida...) e sugere a melhor conduta
- 🔍 **6 formas de busca nativa**: nº CNJ, nome da parte, nome do
  advogado/representante, CPF, CNPJ e OAB passam pelo mesmo formulário
  `Processo → Consulta processos`
- 🧭 **Resultado auditável**: devolve critério aplicado, total, paginação,
  completude e aviso de resposta inconclusiva; CPF, CNPJ, OAB e nomes são
  mascarados nos metadados
- ⚖️ **Comparativo de processos (`comparar_processos`)**: análise lado a lado de múltiplos CNJs num único retorno
- 🔢 **Validação CNJ & auto-formatação**: valida digito verificador (módulo 97) via `validar_numero_cnj` e normaliza CNJs sem formatação automaticamente
- 📄 **Listagem, índice remissivo & leitura de documentos (`listar_documentos`, `gerar_indice_remissivo_autos`, `ler_documento`)**: estrutura tabela de conteúdos dos autos por categoria
- 🔎 **Busca textual profunda no TEOR dos autos (`pesquisar_autos_texto`)**: lê o conteúdo interno de N peças e extrai trechos (snippets) onde a palavra-chave aparece
- 🔍 **Filtro rápido de documentos (`filtrar_documentos_processo`)**: busca peças por tipo/termo (ex: contestação, laudo, inicial)
- 📜 **Filtro histórico de movimentações (`buscar_movimentacoes_por_termo`)**: encontra atos por palavras-chave (ex: citação, penhora, audiência)
- 👥 **Quadro qualificado de polos (`quadro_partes_advogados`)**: organiza partes por pólo ativo/passivo e advogados
- 📊 **Resumo executivo, folha de rosto, dossiê ZIP & exportação (`gerar_relatorio_markdown_processo`, `gerar_relatorio_html_processo`, `exportar_relatorio_pdf_processo`, `gerar_folha_de_rosto_processual`, `gerar_dossie_executivo_zip`)**: empacota minutas, relatórios, capas e documentos num `.zip` do processo
- 🩺 **Diagnóstico de saúde & auditoria completa (`status_servidor`, `auditoria_mcp_pje`)**: verifica ambiente Python, pacotes, espaço livre em disco, permissões e conectividade HTTP ao TJPA 1g, 2g e PDPJ SSO
- 🧹 **Gestão de armazenamento (`limpar_cache_processos`)**: estatísticas de uso de disco e suporte a diretório customizado via `PJE_STORAGE_DIR`
- 🗂️ **Histórico completo de expedientes** de um processo (inclusive fechados/vencidos)
- 📝 **Fluxo de produção**: modelos de petição, preenchimento automático de variáveis (`duplicar_e_preencher_modelo`), salvamento e listagem de minutas geradas (`listar_minutas_processo`)
- 🛡️ **Tratamento automático** do aviso da Resolução CNJ 121/2010 (processos de terceiros)
- 🧭 **Auditoria processual consultiva (`auditar_fluxo_processual_pje`)**:
  confronta ocorrências de um snapshot completo com playbook aprovado, explica
  destino e próximos atos ou se abstém; nunca movimenta o PJe
- 📚 **Análise processual completa (`analisar_processo_completo_pje`)**:
  pipeline retomável de inventário, triagem rápida e aprofundamento integral;
  abre os autos uma vez por etapa, reaproveita cache criptografado e entrega
  cobertura, contradições e próximos atos citáveis; aceita lotes paralelos com
  reposição imediata de cada vaga liberada
- 🧠 **Agente processual Vertex AI (`gemini-3.5-flash`)**: analisa dossiês já
  coletados e só publica conclusões cujos trechos, páginas e hashes forem
  confirmados localmente

## 🛠️ Catálogo atual: 17 ferramentas

O servidor registra 12 ferramentas principais e quatro adaptadores de
compatibilidade. A fonte canônica é `docs/architecture/current.md` na raiz do
repositório; este documento apresenta a interface principal.
Use `status_e_auditoria_pje(acao="capacidades")` para obter o inventário
versionado de ações, modos e aliases.

| Superferramenta | Escopo |
|---|---|
| `status_e_auditoria_pje` | saúde, auditoria, jobs e inventário de capacidades |
| `painel_e_prazos_pje` | painel, prazos, caixas, acervo e exports cifrados |
| `buscar_processos_pje` | buscas simples e avançadas da consulta nativa e validação de CNJ |
| `analisar_processo_pje` | resumo, partes, movimentos, pendências e comparação |
| `gerir_documentos_pje` | listagem e leitura de peças e decisões |
| `download_e_cache_pje` | downloads locais, integridade e cache |
| `producao_minutas_e_relatorios` | produção exclusivamente local, sem envio ao PJe |
| `auditar_fluxo_processual_pje` | auditoria consultiva, fail-closed e sem movimentação |
| `analisar_processo_completo_pje` | inventário, análise rápida e integral retomáveis e citáveis |
| `pje_ler_autos_digitais` | timeline dos autos e leitura individual ou em lote de peças |
| `pje_rastrear_ar_correios` | extração e rastreamento de avisos de recebimento |
| `automacao_navegador_pje` | integração local MCP → bridge Node → Playwright → Chrome/PJe |
| `retificar_autuacao_pje` | alteração confirmada de classe, assuntos, partes e características da autuação |

`automacao_navegador_pje` mantém um único dono para a sessão do Chrome e
expõe ações de estado, busca, abertura dos autos, listagem e navegação das
peças. O PDF exibido pode ser baixado para o storage protegido e analisado
localmente; a análise valida o SHA-256 e usa OCR seletivo somente nas páginas
com pouco ou nenhum texto nativo.

A aba `Expedientes` é aberta pelo controle oficial de envelope e pode ser
listada sem acionar ciência ou resposta. A `Retificação da Autuação` usa uma
ferramenta separada: primeiro produz uma prévia vinculada ao processo,
alterações e estado atual do formulário; a aplicação exige o token efêmero da
prévia, aceita somente uma seção por vez e reconfirma o contexto antes de
qualquer gravação.

### Limite de autoridade

O MCP Pedreira permanece observador por padrão. Ele pode autenticar, navegar,
pesquisar, ler, baixar e persistir artefatos localmente. A única exceção
publicada para cadastro é `retificar_autuacao_pje`, sempre em duas etapas e
com confirmação vinculada ao estado. O MCP não responde expedientes, não
anexa para protocolo, não assina, não protocola e não movimenta tarefa no PJe.
A antiga rota `protocolar_processo_pje` não é publicada e sua entrada interna
permanece bloqueada por política.

Perfis internos nunca são selecionados pelo texto exibido. O diagnóstico
somente leitura procura o identificador publicado pelo próprio PJe em
`option.value`, atributos `data-*`, resposta de API, parâmetro de requisição ou
JavaScript, nessa ordem. `pje_id`, rótulo, unidade, localização e papel são
mantidos separadamente. Hash do rótulo serve apenas para separar armazenamento
provisório e não autoriza navegação. Sem um `pje_id` real, a operação falha com
`PERFIL_SEM_IDENTIFICADOR_ESTAVEL`; essa falha fica restrita à operação e ao
perfil afetados.

Quando o PJe não publica esse identificador, existe uma via separada:
`painel_e_prazos_pje(acao="sessao_contexto_fixado")`. Ela abre um navegador
novo sem restaurar cookies, usa o rótulo completo uma única vez como
localizador não confiável e, antes de qualquer consulta ou cache, confirma
usuário, persona, grau, unidade, localização, papel, título e rota. A identidade
opaca da sessão é calculada somente desses fatos confirmados. A sessão não
troca de perfil, não é reutilizada por outra persona e é destruída se expirar
ou divergir. A troca automatizada tradicional continua exigindo `pje_id`.

Localização interna tem estado explícito: `PRESENTE`,
`AUSENTE_NAO_VERIFICADA` ou `NAO_APLICAVEL`. Ausência nunca é convertida
automaticamente em “não aplicável”; o último estado exige confirmação humana
específica e validação independente dos demais campos. Falha indeterminada do
SSO interrompe retentativas e abre circuit breaker com cooldown.

Downloads e relatórios locais são efeitos fora do PJe e aparecem nas
anotações MCP. Exports confidenciais ficam cifrados em repouso, têm TTL e só
podem ser lidos pela ação explícita correspondente após nova validação da
referência de autorização.

Arquivos baixados só entram ou voltam do cache após validação do conteúdo:
PDF exige `%PDF-`, `%%EOF` e tamanho mínimo; HTML exige estrutura válida e
ausência de página de login/erro. A gravação é atômica e o retorno inclui
SHA-256 e MIME detectado. Um escopo sem arquivo verificado nunca produz
`tudo_integro=true`.

<!-- Catálogo histórico detalhado removido: o inventário em execução é a fonte canônica.


**Painel, prazos e diagnóstico**

| Ferramenta | O que faz |
|---|---|
| `status_servidor` | diagnóstico de saúde, credenciais do Keychain e sessão Chromium ativa |
| `auditoria_mcp_pje` | auditoria completa do ambiente: pacotes, Keychain, espaço em disco e teste de conectividade HTTP |
| `expedientes_pendentes` | intimações/despachos pendentes de ciência ou resposta |
| `verificar_prazos_urgentes` | expedientes com data limite em ≤ N dias (padrão 3 dias) |
| `analisar_risco_prazos` | matriz categorizada de risco de prazos (Crítico, Alto, Médio, Baixo) |
| `analisar_estatisticas_painel_expedientes` | dashboard analítico (BI) de expedientes agrupado por Vara/Órgão e tipo de ato |
| `calcular_prazos_processuais` | calcula data final de prazos em dias úteis (CPC/2015), feriados e recesso forense |
| `pendencias_processo` | pendências (expedientes + prazos) de UM processo |
| `expedientes_do_processo` | histórico COMPLETO de expedientes (inclui fechados/vencidos) |

**Consulta e busca**

| Ferramenta | O que faz |
|---|---|
| `validar_numero_cnj` | valida checksum (módulo 97 - Resolução CNJ 65/2008) e analisa a estrutura do CNJ |
| `consultar_processo` | dados básicos do processo por nº CNJ |
| `buscar_processos_pje` | pesquisa na tela nativa por identificadores/partes e também assunto, classe, jurisdição, órgão julgador, prioridade, intervalos de autuação/valor, movimento e dados criminais; `processo_referencia` permanece deliberadamente fora; confere polos e distingue zero confirmado de resposta inconclusiva |
| `comparar_processos` | compara dados básicos, classe, vara e movimentações de múltiplos CNJs |
| `quadro_partes_advogados` | estrutura categorizada das partes por pólo (ativo/passivo) e advogados |
| `analisar_linha_do_tempo_processo` | análise da linha do tempo, tempo de tramitação e períodos de inatividade (>30 dias) |
| `recomendar_estrategia_processual` | recomendações estratégicas jurídicas automatizadas (impulso, embargos, execução) |
| `ultimas_movimentacoes` | N últimas movimentações |
| `buscar_movimentacoes_por_termo` | filtra o histórico de movimentações por palavra-chave |
| `relatorio_processo` | relatório completo: dados, movimentações e documentos |
| `resumo_executivo_processo` | panorama consolidado (dados, movimentações, última decisão e pendências) |
| `buscar_por_nome_parte` / `buscar_por_nome_advogado` | busca por nome |
| `buscar_por_cpf` / `buscar_por_cnpj` / `buscar_por_oab` | busca por identificador |

**Documentos, autos e armazenamento**

| Ferramenta | O que faz |
|---|---|
| `listar_documentos` | todos os documentos do processo |
| `gerar_indice_remissivo_autos` | gera índice remissivo categorizado (Iniciais, Defesas, Decisões, Provas) |
| `filtrar_documentos_processo` | filtra documentos do processo por palavra-chave ou tipo |
| `pesquisar_autos_texto` | realiza busca textual profunda no TEOR dos documentos e extrai trechos/snippets |
| `ler_documento` | texto integral de um documento (HTML ou PDF) |
| `ultima_decisao` | teor da última decisão/sentença/despacho/ato ordinatório |
| `classificar_teor_decisao` | classifica o resultado jurídico (Procedente, Improcedente, Extinção, Tutela Deferida...) |
| `ultimo_despacho` | teor do último despacho (só despacho) |

| `baixar_documento` | baixa UM documento e salva na pasta do processo |
| `baixar_processo` | baixa os autos COMPLETOS (em background por padrão) |
| `status_download` | acompanha um download em andamento |
| `preparar_processo` | baixa o processo e decide a estratégia de análise |
| `limpar_cache_processos` | calcula uso de disco por grau/processo e limpa PDFs/HTMLs baixados |

**Produção de peças (grava só no SEU disco, nunca no PJe)**

| Ferramenta | O que faz |
|---|---|
| `listar_modelos_peticao` / `ler_modelo_peticao` | modelos em `Modelos TJPA/` no iCloud |
| `pesquisar_modelos_peticao` | pesquisa palavra-chave no nome ou conteúdo dos modelos de petição |
| `duplicar_e_preencher_modelo` | preenche modelo de petição com metadados do processo ({NUMERO_CNJ}, {CLASSE}, {AUTOR}, {REU}...) |
| `salvar_peticao_processo` | salva petição (.docx) na pasta do processo |
| `salvar_relatorio_processo` | salva relatório de análise na pasta do processo |
| `gerar_relatorio_markdown_processo` | gera e salva um relatório estruturado em Markdown (`Relatorio_{cnj}.md`) na pasta do processo |
| `gerar_relatorio_html_processo` | gera e salva um relatório visual estilizado em HTML (`Relatorio_{cnj}.html`) na pasta do processo |
| `exportar_relatorio_pdf_processo` | gera e exporta um relatório em PDF impresso de alta qualidade (`Relatorio_{cnj}.pdf`) via Playwright Chromium |
| `gerar_folha_de_rosto_processual` | gera folha de rosto oficial impresso (.html/.pdf) para a capa do processo |
| `exportar_todos_relatorios_processos` | gera relatórios sintéticos em lote (.md ou .html) para múltiplos processos |
| `gerar_dossie_executivo_zip` | empacota minutas, relatórios e documentos num único arquivo `.zip` |
| `listar_minutas_processo` | lista todas as peças e relatórios (.docx/.md/.txt/.html/.pdf) gerados no processo |







-->

Parâmetros comuns: `grau` (`"1"`/`"2"`) e `persona` (`"advogado"` padrão ou `"procurador"`).

## Auditor processual

A superferramenta `auditar_fluxo_processual_pje` implementa as ações
`planejar_auditoria`, `iniciar_auditoria_caixa`, `status_auditoria`,
`listar_resultados`, `explicar_resultado`, `exportar_relatorio` e
`registrar_revisao_humana`.

O piloto é fail-closed, restrito ao 1º grau e à caixa `Providências a adotar`.
Abrir autos exige autorização de leitura explícita, snapshot integral, lotação
compatível e playbook vigente/aprovado. Configure o bundle com
`PJE_TASK_POLICY_PATH`; o conteúdo confidencial é criptografado com chave do
keyring ou `PJE_AUDIT_MASTER_KEY`.

Consulte [docs/AUDITORIA_PROCESSUAL.md](docs/AUDITORIA_PROCESSUAL.md) antes de
habilitar o piloto. A fundação técnica não substitui classificação de risco,
RIPD, avaliação de impacto algorítmico, cadastro aplicável no Sinapses ou
aprovação institucional.

A análise integral de processo único está documentada em
[docs/ANALISE_PROCESSUAL_COMPLETA.md](docs/ANALISE_PROCESSUAL_COMPLETA.md).

## 📂 Onde os arquivos são salvos

```
~/Library/Mobile Documents/com~apple~CloudDocs/
├── Processos TJPA 1 Grau/{cnj}/    # autos, documentos e peças do 1º grau
├── Processos TJPA 2 Grau/{cnj}/    # idem, 2º grau (mesmo CNJ ≠ mesma pasta!)
└── Modelos TJPA/                   # modelos .docx/.md de petição/relatório
```

## 🧰 Requisitos

- macOS (credenciais no Keychain; em Linux/Windows funciona com `keyring` equivalente)
- Python 3.10+
- Claude Desktop instalado
- Conta ativa no PDPJ com 2FA configurado via app autenticador

## 📦 Instalação

### 1) Ambiente virtual + dependências

```bash
cd PJePA-Pedreira/mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2) Credenciais

Se você **já usa o MCP PJe-TJPI** neste Mac, pule esta etapa — o servidor reaproveita as credenciais PDPJ do service `mcp-pje-tjpi` do Keychain (fallback automático).

Senão:

```bash
python3 setup_credenciais.py
```

O script pergunta CPF, senha PDPJ e seed TOTP e grava tudo no Keychain (service `mcp-pje-tjpa`) — nunca em arquivo.

### 3) Registre o MCP no Claude Desktop

No arquivo de configuração do cliente MCP, use caminhos absolutos do seu
checkout. Exemplo com placeholders:

```json
{
  "mcpServers": {
    "pje-tjpa": {
      "command": "<CHECKOUT>/mcp-server/.venv/bin/python",
      "args": ["<CHECKOUT>/mcp-server/src/server.py"]
    }
  }
}
```

### 4) Reinicie o Claude Desktop

`Cmd+Q` e abra de novo — as ferramentas devem aparecer.

## 💬 Exemplos de uso

```
Tenho expedientes pendentes no TJPA?

Quais meus prazos urgentes no 2º grau do TJPA?

Consulte o processo 0000000-00.0000.8.10.0001

Consulte a apelação 0000000-00.0000.8.10.0001 no 2º grau

Busca processos pela minha OAB no TJPA

Liste os documentos do processo e lê a última decisão

Baixa os autos completos e prepara o processo para análise
```

## 🏗️ Estrutura do projeto

```
PJePA-Pedreira/mcp-server/
├── README.md
├── requirements.txt
├── requirements.lock
├── resources/              # calendários e catálogo TPU estáticos
├── setup_credenciais.py     # setup inicial (opcional se já usa o MCP TJPI)
└── src/
    ├── server.py            # fachada do servidor MCP (17 ferramentas)
    ├── pje_client.py        # cliente Playwright (URL_BASES por grau)
    ├── cliente_singleton.py # 1 sessão viva, chaveada por (persona, grau)
    ├── pje_downloader.py    # downloads (pastas por grau, jobs em background)
    ├── minutas.py           # salvar petições/relatórios (.docx/.md/.txt)
    └── modelos.py           # leitura de modelos em Modelos TJPA/
```

## Acervo de tarefas

Na super-ferramenta `painel_e_prazos_pje`:

- `acao="consultar_acervo_estruturado", formato="compacto"` retorna até
  5.000 ocorrências por página com dicionários e bitmask de indicadores.
  Para agentes e páginas pequenas, use `envelope="minimo"` e
  `incluir_facetas=false`: os dicionários ficam limitados à página e a
  consulta sem filtros usa paginação SQL indexada. O retorno inclui
  `snapshot.revision` e `paginacao.next_cursor`; envie `if_revision` para
  evitar retransmitir um snapshot inalterado.
- As respostas de `painel_e_prazos_pje` também são publicadas em
  `structuredContent`, mantendo o JSON textual para clientes MCP antigos.
- `acao="metricas_desempenho_acervo"` devolve p50/p95/p99 locais sem CNJ,
  partes ou termos de pesquisa.
- `acao="exportar_acervo"` produz NDJSON, CSV, JSON ou SQLite comprimido e
  cifrado, com SHA-256 e TTL de sete dias. A leitura exige
  `acao="ler_export_acervo"`, `export_id` e a mesma `autorizacao_ref`.
- `acao="estatisticas_acervo"` calcula mediana, P90, máximo e indicadores
  por tarefa, classe, assunto, órgão ou mês.
- `acao="sincronizar_caixas", modo="incremental"` reutiliza caixas somente
  após confirmar o hash; `modo="integral"` força a coleta completa.

O job diário pode ser executado manualmente:

```bash
python3 src/jobs_acervo.py --modo incremental
```

Os templates `deploy/systemd/pjepa-acervo-diario.service` e
`deploy/systemd/pjepa-acervo-diario.timer` agendam a execução às 06h30 de Belém em
dias úteis. Revise usuário e caminhos antes de instalar e ativar o timer.

Para validar a massa real de regressão sem alterar o banco original:

```bash
python3 scripts/verificar_snapshot_626ba164.py
```

Para medir consultas com dados exclusivamente sintéticos, sem tocar no PJe:

```bash
python3 scripts/benchmark_acervo.py --ocorrencias 100000 --repeticoes 10
```

Detalhes do contrato e do benchmark estão em
`docs/PERFORMANCE_ACERVO.md`.

## 🔒 Segurança

- **Credenciais** ficam no Keychain do macOS, nunca em arquivo
- **Seed TOTP** tratada como secret — não commitar nunca
- **Nenhuma ação de escrita no PJe**: este MCP só **lê** informação do tribunal — nunca protocola, peticiona ou altera nada (as ferramentas de "salvar" gravam apenas no seu disco local)
- **Resolução CNJ 121/2010**: consulta a processo de terceiro é registrada pelo próprio PJe e o retorno inclui o aviso

## ⚠️ Avisos importantes

**Validade das credenciais** — a senha do PDPJ expira periodicamente; ao trocar no site, rode `setup_credenciais.py` de novo (ou atualize o service que estiver em uso no Keychain).

**Fragilidade de scraping** — o projeto depende do HTML/JavaScript atual do PJe-TJPA. Se o tribunal mudar o layout: rode com `PJE_HEADLESS=0` para ver onde trava, pegue os novos seletores no DevTools e atualize o `pje_client.py`.

**Uso responsável** — respeite o termo de uso do PJe; nada de scraping massivo; consultas a processos de terceiros ficam registradas — use com responsabilidade profissional.

## 📝 Licença e créditos

Uso pessoal e profissional, sem garantias — use por sua conta e risco, respeitando as regras do tribunal e do seu cliente. Construído por [Fábio Ximenes Barros](https://github.com/fxbarros) com ajuda do [Claude](https://www.anthropic.com/claude), usando [Playwright](https://playwright.dev), [PyOTP](https://pyauth.github.io/pyotp/), [Scrapling](https://github.com/D4Vinci/Scrapling) e [pdfplumber](https://github.com/jsvine/pdfplumber).

<p align="center"><sub>Arte do banner: original — marca dos projetos MCP do autor.</sub></p>
# Consultas sem confirmação duplicada

As ações consultivas das onze ferramentas executam diretamente quando a sessão,
o perfil e os parâmetros são válidos. Os parâmetros legados
`confirmar_consulta` e `confirmation_token` permanecem aceitos apenas para
compatibilidade com clientes antigos e são ignorados.

Confirmações específicas de operações destrutivas locais, como excluir arquivos
do cache, continuam vinculadas à própria ação destrutiva. O servidor não publica
ações de assinatura, protocolo ou movimentação no PJe.

O warm-up autenticado fica desabilitado por padrão (`PJE_WARMUP=0`), evitando
acesso preventivo ao PJe durante o startup. Ativá-lo reduz essa garantia e não
é recomendado neste perfil de operação.
