# Imagem do servidor MCP para Cloud Run (gen2).
# Contexto de build: a raiz do repositório. Ver docs/operations/cloudrun.md.
#
# A imagem contém apenas o servidor Python/FastMCP e o Chromium do Playwright.
# A ponte Node (automacoes-pje) é uma rota de desktop local e fica fora do
# container de propósito; automacao_navegador_pje devolve erro estruturado.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY mcp-server/requirements.lock mcp-server/requirements.lock
RUN pip install --no-cache-dir -r mcp-server/requirements.lock

# Chromium e bibliotecas de sistema em camada própria: só invalida quando o
# pino do Playwright muda no requirements.lock.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY mcp-server/ /app/mcp-server/
COPY deploy/cloudrun/entrypoint.sh /app/entrypoint.sh

# O sandbox do Chromium exige processo não-root; /var/lib/pjepa-mcp é o ponto
# de montagem do volume de estado (GCS FUSE) e precisa existir sem o volume.
RUN chmod 0755 /app/entrypoint.sh \
    && useradd --create-home --uid 10001 pjepa \
    && mkdir -p /var/lib/pjepa-mcp \
    && chown -R pjepa:pjepa /var/lib/pjepa-mcp /ms-playwright

USER pjepa

ENV MCP_HOST=0.0.0.0 \
    PJE_ENV=production

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
