# Deploy no Cloud Run

Rota de produção em nuvem do servidor MCP, alternativa à topologia systemd de
[deployment.md](deployment.md). O kit vive em `deploy/cloudrun/` e dimensiona o
serviço para cerca de 12 usuários simultâneos consumindo as ferramentas via
Gemini/Vertex AI ou qualquer cliente MCP autenticado.

## Topologia

```text
[ Cliente MCP / agente Gemini ]
        │  HTTPS + token de identidade (IAM, run.invoker)
        ▼
[ Cloud Run gen2: pjepa-mcp ]───(env→arquivos)──▶ $CREDENTIALS_DIRECTORY
        │        │                                 (Secret Manager)
        │        └──(GCS FUSE)──▶ gs://<projeto>-pjepa-mcp-estado
        │                          montado em /var/lib/pjepa-mcp
        ▼  Direct VPC egress
[ Sub-rede pjepa-egress-sub ]──▶ [ Cloud NAT + IP estático ]──▶ PJe TJPA
```

## Decisões que divergem do plano de referência

O desenho partiu de um plano gerado externamente (Vertex/Gemini) e foi ajustado
ao código real:

- **Segredos sem mudança de código.** O servidor já lê credenciais de
  `$CREDENTIALS_DIRECTORY/{cpf,senha,totp_seed,audit_master_key}`
  (`cliente_singleton.py`, `session_manager.py`, `auditoria_processual.py`).
  O entrypoint materializa esses arquivos em tmpfs a partir das variáveis
  `PJE_CRED_*` injetadas pelo Secret Manager e depois as remove do ambiente.
  Nada de novo caminho de configuração, nada de segredo em variável viva.
- **Direct VPC egress em vez de Serverless VPC Access connector.** Mesmo
  resultado (egress pelo IP fixo do Cloud NAT) sem o custo por hora do
  connector.
- **Perfil do navegador é efêmero.** `PJE_PROFILE_DIR` fica em `/tmp`;
  Chromium sobre GCS FUSE não é confiável (locks, mmap). A sessão autenticada
  persiste como `storage_state` dentro de `PJE_STORAGE_DIR`, que fica no
  bucket, então instâncias novas reaproveitam login sem refazer TOTP.
- **A ponte Node fica fora da imagem.** `pje_browser_bridge.js` é uma rota de
  desktop local (Chrome/CDP do operador). No Cloud Run a rota canônica é o
  Playwright Python interno; `automacao_navegador_pje` devolve o erro
  estruturado já documentado quando a ponte está ausente.
- **`stateless_http=True` já está no código**, então qualquer instância
  atende qualquer POST. `--session-affinity` fica ligado só como otimização
  de cache de sessão por instância.

## Passo a passo

```bash
cd deploy/cloudrun
cp config.env.example config.env   # revise projeto, região e nomes
./setup_infra.sh                   # uma vez por projeto; pede os segredos
./deploy.sh                        # build via Cloud Build + deploy
```

`setup_infra.sh` é idempotente e imprime ao final o IP estático de saída — é
esse IP que se cadastra em eventual liberação junto ao TJPA caso o tribunal
bloqueie faixas de nuvem pública. `deploy.sh` imprime o comando de smoke test
(`initialize` via token de identidade).

Os quatro segredos seguem o contrato do systemd: `pjepa-mcp-cpf`,
`pjepa-mcp-senha`, `pjepa-mcp-totp-seed` e `pjepa-mcp-audit-master-key`.
A chave de auditoria é gerada automaticamente na primeira execução; as demais
são digitadas sem eco. Rotação = nova versão no Secret Manager + novo deploy.

## Dimensionamento

Cada requisição pode segurar um Chromium (~1–1,5 GiB). A configuração padrão em
`config.env.example`:

| Parâmetro       | Valor | Racional                                   |
| --------------- | ----- | ------------------------------------------ |
| CPU / memória   | 2 vCPU / 4 GiB | 2 navegadores simultâneos com folga |
| `--concurrency` | 2     | limita a 2 Chromium por instância          |
| `--max-instances` | 6   | 6 × 2 = 12 requisições simultâneas         |
| `--min-instances` | 0   | escala a zero; suba para 1 se o cold start incomodar |
| `--timeout`     | 600 s | análises longas e leitura de autos em lote |

Custo típico nessa escala: Cloud Run cobra por uso e tende a ficar baixo com
`min-instances=0`; o custo fixo relevante é o par Cloud NAT + IP estático
(~US$15–20/mês).

## Segurança e conformidade

- Serviço **sem acesso não autenticado**: somente `roles/run.invoker` na conta
  `pjepa-mcp-invoker` (ou membros adicionais que você conceder).
- Runtime roda como `pjepa-mcp-runtime` com o mínimo: `secretAccessor` nos
  quatro segredos, `objectAdmin` no bucket de estado e `aiplatform.user` para
  o especialista Vertex já usado pelo servidor (ADC, sem `GEMINI_API_KEY`).
- Região padrão `southamerica-east1` mantém dados processuais no Brasil; os
  termos do Vertex AI não usam prompts/saídas para treinar modelos globais.
- Bucket com acesso uniforme e `public-access-prevention`; auditoria
  (`PJE_AUDIT_LOG`) persiste no bucket em `logs/audit.log`.
- Para perímetro adicional (VPC Service Controls, CMEK), trate como evolução;
  nada no kit impede.

## Limitações conhecidas

- **IP de nuvem pode ser bloqueado pelo PJe.** O IP do NAT é estático
  justamente para permitir liberação institucional; sem ela, a alternativa é
  proxy corporativo — fora do escopo deste kit.
- **GCS FUSE não é um disco local.** Bom para `storage_state`, downloads e
  logs; ruim para SQLite com lock agressivo e para perfis de navegador (por
  isso o perfil é efêmero).
- **Sandbox do Chromium.** A imagem roda como usuário não-root, que é a
  combinação que funciona no gen2. Se o launch falhar por sandbox em alguma
  atualização de runtime, a saída é o modo CDP (`PJE_CDP_URL`) ou ajustar os
  argumentos de launch — decisão de código, não de infra.
- **Integração com Vertex/Gemini.** O caminho suportado é um agente (por
  exemplo o orquestrador ADK de `pje-process-agents/`) consumindo o endpoint
  MCP com token de identidade. Exportar as ferramentas como OpenAPI para
  "Extensions" não é um recurso pronto do FastMCP — se essa rota virar
  requisito, é trabalho novo no servidor.
