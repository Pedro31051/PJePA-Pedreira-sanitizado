# PJePA Pedreira

Servidor MCP para consultas ao PJe-TJPA, acompanhado por automações auxiliares
e uma extensão Chrome companion. A operação é somente leitura por padrão; a
Retificação da Autuação é a única mutação publicada e exige prévia confirmada.

## Componentes

```text
PJePA-Pedreira/
├── mcp-server/        servidor Python/FastMCP e suíte offline
├── automacoes-pje/    automações JavaScript opcionais
├── chrome-extension/  cliente companion do endpoint local
└── docs/              arquitetura, operação, relatórios e histórico
```

O servidor publica atualmente **17 ferramentas MCP**: 13 ferramentas principais
e 4 adaptadores compatíveis. O inventário canônico e a topologia operacional
estão em [docs/architecture/current.md](docs/architecture/current.md).

## Produção

O serviço atual executa esta working tree por `systemd`, em
`127.0.0.1:8001`. Não mova o checkout, altere a unidade ou reinicie produção
sem seguir [docs/operations/deployment.md](docs/operations/deployment.md).

Para operação em nuvem (Cloud Run com IP fixo de saída, Secret Manager e
estado em GCS), use o kit de `deploy/cloudrun/` descrito em
[docs/operations/cloudrun.md](docs/operations/cloudrun.md).

Estado e credenciais vivem fora do Git:

- `/var/lib/pjepa-mcp/` — sessão, armazenamento e jobs;
- `/var/log/pjepa-mcp/` — auditoria operacional;
- `/etc/credstore.encrypted/pjepa-mcp/` — credenciais systemd cifradas.

## Desenvolvimento offline

```bash
cd mcp-server
python3 -m pytest tests/ -q
python3 -m compileall -q src
python3 -m ruff check .
```

Testes e scripts reais exigem `PJE_ENABLE_LIVE_TESTS=1` e nunca participam da
suíte padrão ou da CI. Não use dados de processos reais em fixtures, exemplos,
logs ou commits.

As automações JavaScript usam `PJE_BROWSER_CONFIG` ou um arquivo local ignorado;
consulte `automacoes-pje/browser_config.local.example.js`. A extensão possui
instruções próprias em [chrome-extension/README.md](chrome-extension/README.md).

## Segurança

O MCP observa o PJe: autentica, navega, pesquisa, lê e baixa para armazenamento
externo controlado. Não assina, protocola nem movimenta processos. Segredos são
entregues por `systemd LoadCredential`; o código falha de modo fechado quando a
chave de auditoria está ausente.

Antes de contribuir, consulte [SECURITY.md](SECURITY.md) e
[docs/architecture/naming.md](docs/architecture/naming.md).
