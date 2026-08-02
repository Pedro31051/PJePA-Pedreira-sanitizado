# PJe Process Agents

Orquestrador ADK de análise processual somente leitura. A primeira especialidade
é inventário e sucessões. O sistema recebe apenas lotes textuais completos já
extraídos localmente do PDF consolidado do PJe, pesquisa fontes oficiais no
Agent Platform Search e produz relatório estruturado ato a ato.

O modelo não controla o navegador e não executa atos no PJe. Depois da resposta,
`app.verifier.validate_inventory_report` confirma localmente cada documento,
página, hash e trecho literal. Saída sem prova exata é rejeitada.

## Desenvolvimento local

```bash
agents-cli install
INTEGRATION_TEST=TRUE uv run pytest tests/unit
PJE_RUN_LIVE_AGENT_TESTS=1 INTEGRATION_TEST=TRUE agents-cli run '<pedido com dossiê JSON textual>'
```

Sem `PJE_OFFICIAL_SEARCH_DATA_STORE_ID`, a pesquisa usa um catálogo local de
fontes oficiais apenas para desenvolvimento. Em ambiente real, configure o ID
completo do data store do Agent Platform Search.

## Segurança e custo

- Baseline fixado em `gemini-3.5-flash`, temperatura zero. Candidatos por função
  só são habilitados com `PJE_MODEL_ROUTING_ENABLED=1` depois de avaliação.
- Nenhuma imagem, print ou PDF é enviado ao modelo; somente texto extraído.
- `PJE_ZERO_RETENTION=1` é o padrão: sessões e artefatos ficam em memória e a
  telemetria em nuvem é desabilitada para a execução processual.
- Testes faturáveis exigem `PJE_RUN_LIVE_AGENT_TESTS=1`; a suíte normal não faz
  chamadas ao Vertex AI.
- Não use Memory Bank para conteúdo processual.
- Não habilite conteúdo de processos em logs ou traces.
- Nenhum deploy deve ser realizado antes da avaliação e aprovação humana.

Simple ReAct agent
Agent generated with `agents-cli` version `1.2.1`

## Project Structure

```
pje-process-agents/
├── app/         # Core agent code
│   ├── agent.py               # Main agent logic
│   ├── fast_api_app.py        # FastAPI Backend server
│   └── app_utils/             # App utilities and helpers
├── tests/                     # Unit, integration, and load tests
├── GEMINI.md                  # AI-assisted development guide
└── pyproject.toml             # Project dependencies
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `GEMINI.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)


## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv                                                         |
| `agents-cli playground` | Launch local development environment                                                  |
| `agents-cli lint`    | Run code quality checks                                                               |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests                                                        || [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector                                                        |

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Edit your agent logic in `app/agent.py` and test with `agents-cli playground` - it auto-reloads on save.

## Deployment

```bash
gcloud config set project <your-project-id>
agents-cli deploy
```

To add CI/CD and Terraform, run `agents-cli scaffold enhance`.
To set up your production infrastructure, run `agents-cli infra cicd`.

## Observability

A telemetria do scaffold permanece desabilitada no modo padrão de zero retenção.
Somente métricas opacas, sem prompt, resposta, nomes, documentos ou número de
processo, podem ser habilitadas numa implantação institucional aprovada.

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
See the [A2A Inspector docs](https://github.com/a2aproject/a2a-inspector) for details.
