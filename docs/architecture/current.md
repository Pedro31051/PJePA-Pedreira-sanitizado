# Arquitetura corrente

## Catálogo executável

A fonte de verdade é cada função decorada com `@mcp.tool` em
`mcp-server/src/server.py`. A revisão de 01/08/2026 registra 17 ferramentas:

1. `analisar_processo_completo_pje`
2. `status_e_auditoria_pje`
3. `atuar_fluxo_tarefas_pje`
4. `painel_e_prazos_pje`
5. `buscar_processos_pje`
6. `analisar_processo_pje`
7. `gerir_documentos_pje`
8. `download_e_cache_pje`
9. `producao_minutas_e_relatorios`
10. `auditar_fluxo_processual_pje`
11. `pje_ler_autos_digitais`
12. `pje_rastrear_ar_correios`
13. `pje_capturar_evidencia`
14. `pje_consultar_processo`
15. `pje_analisar_autos_lote`
16. `automacao_navegador_pje`
17. `retificar_autuacao_pje`

As primeiras 12, a automação do navegador e a retificação formam a interface
principal. As quatro anteriores à automação do navegador são adaptadores
estreitos preservados por compatibilidade.

`automacao_navegador_pje` é a integração local explícita entre o MCP Python e
o processo Node/Playwright. A comunicação usa socket UNIX protegido; essa rota
não inicializa um segundo navegador no servidor MCP.

`retificar_autuacao_pje` é a única rota mutante publicada. Ela aceita uma seção
por vez, gera uma prévia vinculada ao processo e ao hash do formulário e só
grava com o token efêmero correspondente. Respostas a expedientes, remoção de
partes, protocolo, assinatura e movimentação de tarefas permanecem fora da
autoridade da automação.

## Fronteiras

- `src/api`: políticas e handlers HTTP auxiliares;
- `src/contracts`: contratos de resposta e evidência;
- `src/domain_*`: análise jurídica versionada;
- `src`: fachadas compatíveis e módulos ainda em decomposição incremental;
- `resources`: recursos estáticos, nunca dados operacionais;
- `tests`: testes offline padrão; integrações usam apenas dublês e temporários;
- `scripts/live`: acesso real somente com opt-in explícito;
- `scripts/benchmarks`: cargas exclusivamente sintéticas;
- `deploy/systemd`: templates sanitizados da topologia canônica.

Dados processuais, SQLite, cache, downloads e exports permanecem fora do
repositório em `PJE_STORAGE_DIR`.

## Processamento em volume

O caminho de escala é incremental e não transforma o Git em acervo:

- entradas reais são parâmetros operacionais; fixtures versionadas são sintéticas;
- produtores particionam o trabalho em lotes e consumidores mantêm concorrência
  limitada, evitando filas ilimitadas em memória (backpressure);
- `job_engine.py`, `durable_jobs.py` e `batch_engine.py` mantêm estado durável,
  retomada, heartbeats, coalescência/idempotência e limite de tentativas;
- temporários e resultados ficam sob `PJE_STORAGE_DIR`, em armazenamento externo
  cifrado e auditável, com retenção definida pela operação e descarte seguro;
- métricas devem separar fila, processamento, I/O, browser e OCR; o benchmark
  sintético em `scripts/benchmarks/benchmark_pipeline_sintetico.py` mede somente
  a infraestrutura de batching e não representa vazão contra o PJe.
