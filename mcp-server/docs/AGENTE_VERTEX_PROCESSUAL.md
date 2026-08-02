# Agente processual Gemini no Vertex AI

O agente recebe exclusivamente o dossiê e as peças já extraídas pelo pipeline
local. Ele não abre o navegador, não acessa o PJe e não executa ações externas.
O modelo fixado neste primeiro corte é `gemini-3.5-flash`, disponível em GA no
Vertex AI.

## Configuração

```bash
gcloud auth application-default login
gcloud config set project your-gcp-project-id
gcloud auth application-default set-quota-project your-gcp-project-id
gcloud services enable aiplatform.googleapis.com --project=your-gcp-project-id
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
export GOOGLE_CLOUD_LOCATION="global"
```

O SDK usado é `google-genai`. A aplicação constrói o cliente com
`vertexai=True` e API `v1`, portanto não usa `GEMINI_API_KEY`. Em produção, as
credenciais devem vir de ADC, identidade da carga de trabalho ou conta de
serviço, nunca do repositório.

O ambiente local validado usa o projeto `your-gcp-project-id`, região `global`, ADC com
quota atribuída ao mesmo projeto e a API Vertex AI habilitada.

`PJE_VERTEX_CONTEXT_CHARS` controla o teto de caracteres enviados por execução.
O padrão é `600000`, com limite defensivo de `2000000`.

## Fluxo

Primeiro produza um dossiê rápido ou integral e aguarde sua conclusão. Depois:

```text
analisar_processo_completo_pje(
    acao="iniciar_agente",
    job_id="<job do dossiê>",
    modelo_agente="gemini-3.5-flash",
    autorizacao_leitura=true,
    autorizacao_ref="<referência da autorização>"
)
```

A resposta contém `run_id`. Consulte com `status_agente`, leia com
`resultado_agente` e recupere uma conclusão específica com
`explicar_agente`, passando `agent_run_id` e `finding_id`.

## Prova dos trechos

O modelo devolve JSON estruturado. Cada finding precisa conter:

- `document_id`;
- página;
- trecho literal curto;
- SHA-256 da peça.

Depois da geração, um verificador local procura a página na peça cifrada,
confere o hash e verifica se o trecho está literalmente presente após apenas
normalizar espaços e caixa. A conclusão inteira é descartada quando qualquer
prova diverge. Itens descartados aparecem somente em
`verification.rejections`, sem publicar a conclusão não comprovada.

O resultado validado também é persistido cifrado. O banco mantém apenas hashes,
estado da execução e mensagem de erro segura em texto aberto.

## Limites

- inventários não podem alimentar o agente porque não contêm teor;
- páginas ausentes, OCR indisponível e cortes de contexto aparecem como lacunas;
- a análise auxilia revisão humana e não autoriza protocolo, assinatura ou
  movimentação automática;
- uma resposta conforme o schema não é suficiente: a prova local é obrigatória.
