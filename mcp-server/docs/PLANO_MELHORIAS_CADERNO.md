# Plano de melhorias orientado pelo caderno MCP PJe

Data da revisão: 2026-07-28.

## Premissa soberana

Este repositório implementa um MCP observador do PJe. O n8n não integra este
escopo. São permitidos autenticação, navegação, pesquisa, leitura, download e
persistência local; são vedados cadastro, preenchimento preparatório, upload
para protocolo, assinatura, protocolo e movimentação de tarefas.

## Dúvidas enviadas ao caderno e decisões aplicadas

1. **Qual é a fronteira read-only?** O caderno determinou retirar a ferramenta
   de protocolo e todas as ações preparatórias da superfície MCP. Navegação,
   pesquisa, download e persistência local permanecem permitidos.
2. **Como entregar exports confidenciais?** O caderno recomendou ação explícita
   de leitura com revalidação de autorização, AES-256-GCM em repouso, TTL,
   vínculo ao solicitante/job e testes de adulteração, expiração e autorização.
3. **Qual o MVP contra prompt injection em peças?** O caderno recomendou
   segregar conteúdo suspeito, preservar proveniência, detectar sinais visuais,
   aplicar saída fail-closed e exigir revisão humana diante de anomalia.

## Estado executado

- [x] exatamente nove superferramentas publicadas, todas com annotations MCP;
- [x] protocolo ausente do inventário e bloqueado no servidor e no cliente;
- [x] recursos MCP confidenciais removidos;
- [x] exports de acervo e auditoria cifrados e lidos por ação autorizada;
- [x] TTL verificado na leitura e limpeza de arquivo expirado;
- [x] resposta de download nativo Playwright descartada em `finally`;
- [x] envio externo de PDF removido; análise grande usa ingestão local;
- [x] segregação determinística de instruções hostis e sinais visuais;
- [x] anomalia documental reduz cobertura e exige revisão humana;
- [x] dependências diretas congeladas em `requirements.lock`;
- [x] documentação alinhada ao contrato observador;
- [x] suíte sintética integral executada.

## Pendências que exigem decisão ou ambiente externo

- remover fisicamente o código histórico de cadastro após preservar a história
  em um commit limpo ou extrair um simulador totalmente offline;
- decidir se exports plaintext antigos devem ser eliminados; a exclusão é
  destrutiva e não deve ocorrer implicitamente;
- validar em canário com conta e massa não sigilosa do PJe;
- aprovar janela de implantação, rollback e reinício do serviço;
- fazer revisão humana das regras jurídicas e do playbook institucional.

Nenhuma dessas pendências autoriza escrita no PJe.
