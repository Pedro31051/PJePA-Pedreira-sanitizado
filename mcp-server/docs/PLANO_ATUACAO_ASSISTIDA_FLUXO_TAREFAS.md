# Plano — Atuação assistida do Playwright no fluxo de tarefas/caixas

**Escopo decidido:** preparação assistida (nunca commit automático) · persona
servidor/secretaria · área fluxo de tarefas e caixas.

**Princípio inegociável:** o MCP monta o ato completo, validado e conferido; o
clique que produz efeito processual é sempre humano. A ampliação é da
*preparação*, não da *autoridade*.

---

## 1. Ponto de partida real

### 1.1 O que já existe e serve de fundação

| Ativo | Onde | Por que importa |
|---|---|---|
| Endpoint do painel | `pje-legacy/painelUsuario/recuperarProcessosTarefaPendenteComCriterios/{caixa}/0` | Fonte única das ocorrências; já paralelizado com retry e conferência de contagem |
| `id_task_instance` / `id_task_instance_proximo` | colunas em `ocorrencias` | **É o handle que uma transição de fluxo consome.** Já persistido para as 8.474 ocorrências |
| 6 flags `pode*EmLote` | `metadados_origem_json` → `capacidades_operacao_em_lote` | **O próprio PJe declara, por ocorrência, o que aquele perfil pode fazer.** Autorização não precisa ser inferida |
| Sessão fixada validada | `sessao_contexto_fixado`, `revalidar_contexto_fixado` | Identidade de perfil já é fail-closed, imutável e revalidada a cada reuso |
| Acervo estruturado | `caixas_tarefas.py`, SQLite versionado por snapshot | Seleção de lote pode ser feita offline, sem tocar no PJe |
| Convenção de page object | `mapa_pje.py` (`ProcessRegistrationPage`, `NativeProcessSearchPage`) | Padrão pronto para isolar seletor instável fora do cliente |

### 1.2 O que **não** serve

A trilha `_ACOES_PROTOCOLO` ([server.py:6396](../src/server.py#L6396), 11 ações,
desabilitada em [server.py:6497](../src/server.py#L6497)) é **cadastro de
processo na persona advogado**. Não é o alvo deste plano e permanece
desabilitada. Reaproveita-se dela apenas o *padrão de contenção*:
`FINAL_ACTION = re.compile(r"\b(?:protocolar|assinar|enviar\s+processo)\b")` —
a ideia de uma denylist de commit compilada dentro do page object.

### 1.3 Lacunas concretas

1. Não existe page object do painel Angular de tarefas — a lógica de DOM está
   dispersa em `pje_client.py` (`_abrir_painel_interno_tarefas`,
   `_coletar_indice_caixas_dom`).
2. **Não se conhece o grafo de fluxo da unidade.** `id_task_instance_proximo`
   existe, mas nunca foi lido o conjunto de destinos que o PJe oferece por
   tarefa. Esta é a maior lacuna de conhecimento.
3. Os flags `pode*EmLote` vivem em JSON não indexado — filtrar elegibilidade
   hoje exige varrer 8.474 blobs.
4. A política é binária (`read_only: True`). Não há estado intermediário para
   representar "preparado, aguardando humano".

---

## 2. Arquitetura em cinco camadas

```
C1 DESCOBERTA      read-only    grafo de transições da unidade
C2 ELEGIBILIDADE   read-only    lote validado, offline, sem navegador
C3 PREPARAÇÃO      escrita UI   seleciona e escolhe destino; PARA antes do commit
C4 HANDOFF         inerte       congela, evidencia, entrega ao humano
C5 RECONCILIAÇÃO   read-only    confirma o que de fato mudou
```

A fronteira dura fica entre **C3 e C4**. Todo o esforço de engenharia de
segurança concentra-se em garantir que C3 jamais atravesse sozinha.

---

## 3. Fases

### Fase 0 — Fundação de contenção
*Sem esta fase, nenhuma outra pode ser mesclada.*

- **Política triestado** substituindo o booleano `read_only`:
  `OBSERVACAO` · `PREPARACAO_ASSISTIDA` · `AGUARDANDO_COMMIT_HUMANO`.
  Nenhum caminho de código transiciona para um quarto estado "executado".
- **`PoliticaAtuacao`** — guard central com:
  - *allowlist* de rotas e seletores clicáveis na preparação;
  - *denylist de commit* compilada (`encaminhar`, `confirmar`, `mover`,
    `assinar`, `concluir`, `enviar`, `finalizar`, `lançar`), verificada contra
    o texto, `title`, `aria-label` e `value` do elemento **antes** de cada
    clique. Casou → aborta a operação inteira, não apenas o clique.
- **Kill-switch duplo:** env var (`PJE_ATUACAO_ASSISTIDA=0` como padrão de
  fábrica) + gate por perfil validado. Ausência de qualquer um → `OBSERVACAO`.
- **Trilha append-only** (`atuacao_log`): toda tentativa de preparação grava
  perfil, lote, hash, seletores tocados e desfecho — inclusive as abortadas.

**Entrega:** guard + testes; nenhuma ação nova publicada.

---

### Fase 1 — `TaskFlowPanelPage`
Extrai para `mapa_pje.py` a navegação do painel interno, hoje dispersa. Só
métodos de leitura nesta fase: `open`, `list_boxes`, `open_box`,
`snapshot_controls`. Refatoração pura, comportamento idêntico — os testes de
`test_caixas_tarefas.py` são a rede de segurança.

**Entrega:** page object + paridade de comportamento comprovada.

---

### Fase 2 — Descoberta do grafo de transições `[read-only]`
**A fase mais importante do plano.** Nova ação
`painel_e_prazos_pje(acao="mapear_transicoes_tarefa")`.

Para cada tarefa da unidade: abre a caixa, seleciona **um** item, lê o conjunto
de destinos que o PJe oferece, e fecha sem confirmar. Persiste em nova tabela
`transicoes` (por perfil + tarefa + snapshot).

Cada transição descoberta recebe uma **classificação de reversibilidade**:

| Classe | Significado | Libera C3? |
|---|---|---|
| `REVERSIVEL_ANTES_COMMIT` | escolher o destino não move nada; há botão de confirmação distinto | sim |
| `COMMIT_NA_SELECAO` | o próprio select/link já movimenta | **não — bloqueada permanentemente** |
| `INDETERMINADA` | não foi possível provar | **não — fail-closed** |

> **Risco central do projeto:** em parte dos fluxos do PJe, escolher o destino
> *já é* o ato. A Fase 4 não pode ser habilitada para nenhuma tarefa cuja
> transição não esteja provada `REVERSIVEL_ANTES_COMMIT`. A prova é empírica,
> feita nesta fase, em um item de baixo impacto, com reconciliação imediata.

**Entrega:** o grafo de fluxo da vara — que hoje ninguém tem — mesmo que a
atuação nunca avance.

---

### Fase 3 — Elegibilidade e simulação de lote `[read-only, sem navegador]`
Nova ação `preparar_movimentacao_lote(modo="simular")`, operando só sobre o
SQLite.

- **Migração de schema v2 → v3:** promover os 6 flags `pode*EmLote` de
  `metadados_origem_json` para colunas indexadas. Elegibilidade vira índice, não
  varredura.
- Monta o lote a partir dos filtros do acervo e devolve **decisão item a item
  com motivo**: incluído; excluído por `podeMovimentarEmLote=false`; excluído
  por sigiloso; excluído por `id_task_instance` ausente; excluído por prioridade
  que exige conferência individual.
- **Gate de frescor:** lote só é preparável sobre snapshot com idade abaixo do
  TTL. `idTaskInstance` envelhece — snapshot velho exige ressincronização antes
  de qualquer preparação.
- Devolve `lote_hash` determinístico (ordenado, sobre `id_task_instance`).

**Entrega:** triagem em lote auditável com risco zero. Valor operacional
imediato mesmo sem C3.

---

### Fase 4 — Preparação na UI `[escrita de UI, sem commit]`
`preparar_movimentacao_lote(modo="preparar")`. Habilitada **apenas** para
tarefas classificadas `REVERSIVEL_ANTES_COMMIT` na Fase 2.

1. Abre sessão fixada validada (reusa o gate existente).
2. Navega à caixa via `TaskFlowPanelPage`.
3. Aplica a seleção do lote; escolhe o destino.
4. **Reconferência obrigatória:** relê a seleção no DOM e compara com
   `lote_hash`. Divergência de um único item → desfaz a seleção, fecha o
   navegador, falha fechado.
5. Para. Captura screenshot de evidência e a contagem exibida pelo próprio PJe.

Todo clique atravessa `PoliticaAtuacao`. A denylist de commit é avaliada contra
o elemento antes de cada interação.

**Entrega:** lote montado na tela, conferido, parado no botão final.

---

### Fase 5 — Handoff humano
A sessão passa a `AGUARDANDO_COMMIT_HUMANO`, com TTL. Entrega ao operador:
`lote_hash`, contagem confirmada na tela, screenshot, resumo do destino e o
acesso à sessão viva (VNC) para o clique final.

- Nenhum caminho automático conclui o ato. TTL estourado → sessão destruída e
  seleção abandonada (o PJe não persiste seleção não confirmada).
- Fallback auditável quando não houver sessão compartilhável: entrega o lote
  como artefato assinado para execução manual, sem navegador.

---

### Fase 6 — Reconciliação `[read-only]`
`conferir_movimentacao_lote(lote_hash=...)`: ressincroniza e compara
antes/depois, confirmando quais processos de fato mudaram de tarefa. Fecha o
ciclo de auditoria e alimenta a classificação de reversibilidade da Fase 2 com
evidência real.

---

### Fase 7 — Demais capacidades
Mesmo padrão, na ordem de risco crescente:

1. **`minutar` em lote** — segundo maior ganho para secretaria e o **menor
   risco**: minuta não é ato final. Candidato natural ao primeiro piloto real.
2. `intimar` · `designar_audiencia` · `designar_pericia` — atos com efeito
   externo; exigem o ciclo completo C1→C6 maduro.
3. `renajud` — atinge terceiros; último da fila, se algum dia.

---

## 4. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| **Escolher destino já movimenta** | Fase 2 classifica; `INDETERMINADA` e `COMMIT_NA_SELECAO` nunca chegam à Fase 4 |
| Lote errado em massa, irreversível | Simulação obrigatória → reconferência no DOM contra `lote_hash` → commit humano |
| Snapshot velho, `idTaskInstance` obsoleto | Gate de frescor com TTL; ressincronização forçada |
| Seletor instável do Angular | Page object isolado; fail-closed, jamais "melhor esforço" |
| Perfil errado atuando | Reusa o gate de sessão fixada já validado em produção |
| Preparação virar commit por regressão de código | `test_atuacao_assistida.py` como teste adversarial permanente |

---

## 5. Validação

Segue o rito de `EVOLUCAO.md`: edição no disco da VM, validação **de fora** por
`curl` na URL pública (stateless, `tools/list` e `tools/call` diretos).

- **Unitário:** fixtures por fase. Precedente em `test_protocolo.py`, que hoje
  testa o *bloqueio* — o novo `test_atuacao_assistida.py` deve provar que o
  commit **nunca** ocorre, incluindo sob DOM adversarial (botão de confirmação
  renomeado, seleção divergente, elemento sobreposto).
- **Real:** Fases 0–3 validam contra o acervo existente. Fase 4 valida
  primeiro com **lote de 1 item**, em tarefa de baixo impacto, com
  reconciliação imediata pela Fase 6 — nunca direto em lote grande.

---

## 6. Sequenciamento

```
Fases 0 → 1 → 2 → 3    risco zero, valor entregue
                        (grafo de fluxo + triagem em lote auditável)
        ─── portão ───  só atravessa com transição provada REVERSÍVEL
Fases 4 → 5 → 6         preparação assistida real
Fase  7                 extensão, começando por `minutar`
```

As Fases 0–3 valem por si: entregam o grafo de fluxo da unidade e a triagem em
lote com decisão justificada item a item, sem nunca escrever no PJe. Se o
portão da Fase 2 provar que as transições da vara são `COMMIT_NA_SELECAO`, o
plano para ali — e ainda assim terá entregue valor.

---

## 7. Questões abertas

1. **Reversibilidade real das transições da Vara de Família de Marabá** —
   resolvida empiricamente na Fase 2; determina se o plano passa do portão.
2. **Superfície de publicação** — as ações novas entram em
   `painel_e_prazos_pje` (mantém as 11 superferramentas) ou justificam uma
   décima segunda, `atuar_fluxo_tarefas_pje`? Recomendação: superferramenta
   nova, porque o eixo de política é diferente e misturá-la com a ferramenta
   read-only de painel enfraquece a leitura do limite de autoridade.
3. **Responsabilidade funcional** — o commit é humano por desenho, mas a
   preparação em lote por automação em nome de Diretor de Secretaria merece
   decisão consciente e, idealmente, registro administrativo prévio.
