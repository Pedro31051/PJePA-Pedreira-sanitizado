# Deploy e rollback

## Topologia canônica

Novas instalações usam releases imutáveis em
`/opt/pjepa-mcp/releases/<commit>` e o symlink atômico
`/opt/pjepa-mcp/current`. Ambiente virtual, credenciais e dados ficam fora da
release.

O serviço atualmente em execução ainda aponta para a working tree histórica.
Esta reorganização **não altera nem reinicia esse serviço**. A migração para a
unidade versionada exige janela operacional explícita.

`scripts/publicar_mcp_local.sh` apenas prepara por padrão. `--activate` troca o
symlink e reinicia; falha de restart restaura o symlink anterior. O rollback
recebe obrigatoriamente o commit de uma release já preparada.

Antes da primeira ativação:

1. provisionar `/opt/pjepa-mcp/{releases,venv}` com owner `pjepa-mcp`;
2. instalar e revisar `deploy/systemd/pjepa-mcp.service`;
3. fornecer `PJE_EXTENSION_TOKEN` e `PJE_EXTENSION_ALLOWED_ORIGINS` por
   `/etc/pjepa-mcp/runtime.env` se a extensão for habilitada;
4. executar `systemd-analyze verify` nos templates;
5. manter cópia da unidade anterior e testar rollback.

## Ponte Playwright local

A integração `automacao_navegador_pje` depende do sidecar
`pjepa-browser-bridge.service`. Os dois serviços compartilham exclusivamente o
socket `/run/pjepa-mcp/browser.sock`; não use `/tmp`, pois `PrivateTmp` pode
isolar os processos em namespaces diferentes.

Antes de habilitar o sidecar:

1. instalar Node.js compatível com `automacoes-pje/package.json`;
2. definir `PJE_BROWSER_CONFIG` em `/etc/pjepa-mcp/browser.env`, apontando para
   uma configuração revisada do Chrome/CDP;
3. garantir que o perfil e o navegador definidos nessa configuração pertençam
   ao usuário `pjepa-mcp`;
4. verificar as duas unidades com `systemd-analyze verify`;
5. iniciar a ponte em janela de desenvolvimento e chamar
   `automacao_navegador_pje(acao='estado')` antes de qualquer busca.

Ausência da ponte, sessão expirada, seletor divergente e timeout são devolvidos
como erros estruturados. A ferramenta não recorre silenciosamente ao
Playwright Python legado.
