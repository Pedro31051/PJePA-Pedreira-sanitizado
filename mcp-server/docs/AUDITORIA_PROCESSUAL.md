# Auditoria processual consultiva

## Estado da implementação

O MCP possui uma fundação somente leitura para auditar ocorrências de processos
em caixas. Ela separa:

- o `TaskPolicyBundle` sanitizado e versionado, produzido pelo `mapa-pjepa`;
- o `ProcessDossier` confidencial, montado localmente a partir de snapshot e
  autos autorizados;
- o `ProcessAuditFinding`, que registra conclusão, abstenção, evidências,
  cobertura e versões.

O piloto está limitado ao 1º grau e às tarefas da allowlist. O bundle pode
declarar aliases — por exemplo, `Providências a adotar` e `Verificar
providência a adotar` — para a mesma identidade de tarefa. Nenhum caminho do
auditor movimenta tarefa, altera marcação, cria minuta, assina ou escreve no PJe.

## Travas antes do uso real

A existência do código não autoriza o uso em produção. A ativação depende de:

1. classificação formal segundo a Resolução CNJ nº 615/2025;
2. RIPD e avaliação de impacto algorítmico aprovadas;
3. cadastro no Sinapses, quando aplicável;
4. finalidade, retenção, equipe revisora e playbook aprovados;
5. endpoint institucional de IA aprovado, caso análise semântica seja usada;
6. conjunto-ouro e piloto cego com os critérios definidos no projeto.

O adaptador semântico e o OCR permanecem desabilitados. Documento sem texto,
árvore incompleta, truncamento ou mudança posterior ao snapshot reduzem a
cobertura e impedem uma conclusão confirmada.

## Configuração

Variáveis:

- `PJE_TASK_POLICY_PATH`: caminho absoluto do bundle
  `pje.task-policy/v1`;
- `PJE_AUDIT_ALLOWED_TASKS`: allowlist separada por `|`; o padrão contém
  `Providências a adotar` e cada rótulo é resolvido pela identidade/aliases
  declarados no bundle;
- `PJE_STORAGE_DIR`: raiz local do armazenamento;
- credencial systemd `audit_master_key`: forma recomendada no serviço Linux,
  contendo uma chave AES-256 em base64 URL-safe;
- `PJE_AUDIT_MASTER_KEY`: alternativa opcional para execução local. Sem
  credencial nem variável, a chave é obtida/criada no keyring do sistema.

Não registre a chave em `.env` versionado, logs ou parâmetros MCP.

## Ações da superferramenta

| Ação | Comportamento |
|---|---|
| `planejar_auditoria` | Conta ocorrências e informa cobertura e bloqueios sem abrir autos. |
| `iniciar_auditoria_caixa` | Abre um job retomável, sob autorização explícita. |
| `status_auditoria` | Mostra progresso e contadores sem teor processual. |
| `listar_resultados` | Filtra por status, destino, classe, urgência e confiança; não expõe CNJ. |
| `explicar_resultado` | Devolve cadeia completa e citações mediante autorização. |
| `exportar_relatorio` | Gera JSON gzip cifrado, permissão `0600`, hash e TTL. |
| `ler_export_relatorio` | Revalida a autorização e decifra um export ainda vigente. |
| `registrar_revisao_humana` | Aceita, rejeita ou corrige localmente; não altera PJe nem playbook. |

`iniciar_auditoria_caixa` exige `snapshot_id`, `nome_tarefa`,
`playbook_version`, `autorizacao_leitura=true` e uma referência de autorização.
O servidor recusa snapshot incompleto, cobertura menor que 100%, lotação
divergente, regra não aprovada/vencida ou tarefa fora da allowlist.

## Ordem da decisão

1. validar snapshot, grau, lotação, perfil e playbook;
2. preservar cada ocorrência e as tarefas simultâneas;
3. construir dossiê incremental, reaproveitando peças por fingerprint;
4. extrair apenas fatos citáveis;
5. detectar tarefas mutuamente exclusivas;
6. aplicar regras aprovadas e vigentes por escopo e prioridade;
7. confirmar divergência apenas com destino único, citações e cobertura
   integral;
8. marcar como provável quando a evidência aponta um destino, mas a cobertura
   é parcial;
9. abster-se diante de lacuna, empate conflitante ou regra ausente.

Tarefas simultâneas legítimas são preservadas. Apenas relações declaradas como
mutuamente exclusivas no playbook produzem `invalid_parallel_task`.

## Persistência e minimização

O SQLite usa WAL e guarda jobs e metadados operacionais sem teor. Dossiês,
findings, revisões, exports e texto derivado são protegidos com AES-256-GCM. Número CNJ,
chave de ocorrência e revisor aparecem nos índices apenas como HMAC.

O adaptador atual não persiste os bytes originais das peças. O texto derivado e
os dossiês expiram em sete dias; exports aceitam TTL entre uma hora e sete dias
e só são decifrados após revalidar a referência de autorização do job.
Relatórios de execução não devem registrar teor processual. Uma política
institucional mais restritiva deve prevalecer.

## Verificação

Os testes usam apenas dados sintéticos:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m compileall -q src
```

O conjunto cobre divergência confirmada e provável, abstenção, tarefas
mutuamente exclusivas, validação de snapshot/playbook, persistência
criptografada, revisão humana, export, planejamento sem abrir autos e bloqueio
sem autorização.

## Vara de Família de Marabá: implantação técnica

`policies/familia_maraba_draft_0.json` é um modelo **fail-closed**. Ele
registra a caixa observada no snapshot como `Verificar providência a adotar`,
aceita `Providências a adotar` como alias e fixa a identidade da unidade no
órgão julgador `916`. A única regra está em estado `draft`, não tem destino e
não pode produzir finding de caixa errada.

Para uma versão aprovada, substitua o modelo por um novo bundle versionado
que contenha fonte vigente, aprovador, data de aprovação, destinos permitidos
e regras determinísticas revisadas pela unidade. Não altere o status do
modelo para `approved` como atalho.

A comparação de lotação é fail-closed:

- o snapshot precisa provar um único órgão julgador, com ID, nome e contagem
  compatíveis com as ocorrências coletadas;
- uma regra aprovada do TJPA deve declarar `orgao_julgador_id` e
  `unit_canonical_segment`;
- a chamada pode usar a lotação completa, o nome canônico do órgão ou um
  `unit_aliases` explicitamente declarado pela regra; secretaria e papel
  isolados não são aceitos.

Os arquivos `deploy/systemd/pjepa-mcp-auditoria-familia-maraba.env.example` e
`deploy/systemd/pjepa-mcp-auditoria-familia-maraba.conf.example` documentam a
configuração systemd. Instale uma cópia revisada do ambiente em
`/etc/pjepa-mcp/` e entregue a chave somente pela credencial systemd
`audit_master_key`; não coloque segredo no arquivo de ambiente.

### Sinais determinísticos disponíveis

Além das referências gerais, o extrator expõe menções da última movimentação:
`latest_movement_mentions_redistribution`,
`latest_movement_mentions_suspension`,
`latest_movement_mentions_hearing` e
`latest_movement_mentions_appeal`. São apenas menções textuais citáveis; uma
regra aprovada ainda precisa provar vigência e destino antes de concluir que a
caixa está errada.
