# Saneamento do histórico Git

A chave de auditoria e dados processuais que apareceram em commits anteriores
devem ser considerados comprometidos mesmo após a correção da árvore atual.

O saneamento remoto exige uma reescrita coordenada e, por isso, não é executado
automaticamente por scripts de desenvolvimento. Sequência obrigatória:

1. rotacionar externamente a chave de auditoria e recriptografar ou invalidar
   dados cifrados com a chave anterior;
2. congelar pushes e criar um mirror de contingência com acesso restrito;
3. executar `git filter-repo` em clone descartável, removendo os arquivos e
   literais identificados pelo relatório redigido;
4. executar `scripts/audit_git_history.py` no clone saneado;
5. validar testes, tags e branches;
6. publicar com `--force-with-lease` após comunicação aos colaboradores;
7. exigir reclone; clones antigos continuam contendo o material removido.

Não guardar o mirror em backup comum. Ele contém o material comprometido e
deve seguir retenção curta, cifrada e acesso mínimo.
