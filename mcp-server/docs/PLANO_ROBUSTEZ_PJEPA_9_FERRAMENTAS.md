# Plano de robustez das nove ferramentas do PJePA

## 1. Objetivo e princípio operacional

Este plano orienta a robustez, a homologação e a liberação das nove
ferramentas públicas do MCP PJePA sem impedir que o próprio desenvolvimento
encontre falhas por meio de execução real, controlada e auditável:

1. `analisar_processo_completo_pje`;
2. `status_e_auditoria_pje`;
3. `painel_e_prazos_pje`;
4. `buscar_processos_pje`;
5. `analisar_processo_pje`;
6. `gerir_documentos_pje`;
7. `download_e_cache_pje`;
8. `producao_minutas_e_relatorios`;
9. `auditar_fluxo_processual_pje`.

O projeto está em desenvolvimento. Pendências de homologação, canários reais
ainda não designados e um gate acumulado em 19/20 são pendências para a
liberação de produção, não impedimentos para iniciar, usar ou testar o MCP com
`PJE_ENV=development`.

Robustez em desenvolvimento significa observar, diagnosticar e isolar falhas.
Alertas e dados incompletos devem ampliar a visibilidade do problema, sem
indisponibilizar globalmente operações independentes e seguras.

## 2. Separação obrigatória de ambientes

### 2.1. Desenvolvimento — `PJE_ENV=development`

No modo de desenvolvimento:

- as nove ferramentas permanecem publicadas e disponíveis;
- o MCP pode ser iniciado e testado mesmo que o gate institucional de produção
  esteja incompleto;
- o resultado 19/20 é registrado como pendência de homologação de produção,
  sem exigir 20/20 para desenvolvimento;
- erros são registrados com contexto diagnóstico e ficam restritos ao menor
  escopo operacional afetado;
- a falha é isolada, conforme sua origem, na ação, documento, processo ou
  sessão afetada;
- uma falha localizada não torna indisponíveis outras ferramentas, processos,
  sessões ou ações que continuem seguras e funcionais;
- alertas de robustez são sinais para investigação, não bloqueios gerais do
  ambiente;
- testes e canários autorizados podem ser repetidos sem homologação final;
- diagnósticos detalhados devem permanecer visíveis para revelar erros
  intermitentes, silenciosos ou escondidos por caminhos de fallback.

Nenhuma pendência de produção deve bloquear, em desenvolvimento:

- inicialização do MCP;
- uso das nove ferramentas;
- consultas processuais e abertura de processos acessíveis ao perfil
  autenticado e autorizado;
- leitura direta dos documentos pela sessão autenticada;
- análise integral de inventário e usucapião;
- testes unitários, integrados e manuais;
- execução dos seis golden cases;
- canários somente leitura;
- testes com processos reais institucionalmente autorizados;
- geração de diagnósticos, relatórios e dados estruturados;
- desenvolvimento e teste do painel;
- cadastro e consultas já permitidos ao perfil utilizado;
- auditoria, extração estruturada e produção de rascunhos;
- entrega de resultados parciais claramente identificados.

As permissões efetivas do perfil, a segregação de sessões e a autenticação
continuam sendo respeitadas. A política de desenvolvimento não amplia acesso:
ela apenas evita que um gate de produção retire capacidades que o perfil já
possui e que são necessárias para testar o MCP.

### 2.2. Produção — `PJE_ENV=production`

Somente o modo de produção exige, como condição de liberação:

- conclusão do gate institucional;
- canários reais aprovados;
- critérios formais de liberação;
- artefato de homologação verificado;
- evidência de rollback, rastreabilidade e aprovação institucional.

Falhas que violem segurança, isolamento, autorização ou integridade podem
bloquear somente o caminho inseguro e a identidade ou sessão afetada. Não
existe chave geral de indisponibilidade do MCP em nenhum ambiente.

## 3. Tratamento localizado de falhas

Cada erro deve ser atribuído à menor unidade operacional conhecida:

| Escopo da falha | Comportamento em desenvolvimento |
| --- | --- |
| Ação ou seletor | Falhar a ação afetada, registrar o diagnóstico e preservar as demais ações. |
| Documento | Marcar o documento como não processado e continuar com os demais documentos. |
| Processo | Interromper apenas o processo afetado, preservando outros processos e consultas. |
| Sessão | Invalidar ou recuperar somente a sessão afetada, sem derrubar sessões independentes. |
| Dependência comum indisponível | Informar indisponibilidade específica e manter capacidades que não dependam dela. |
| Violação comprovada de isolamento ou autorização | Bloquear o caminho inseguro, registrar o incidente e aplicar a resposta de segurança cabível. |

A indisponibilidade global é excepcional e deve corresponder a uma causa
realmente global. Um timeout, documento malformado, seletor divergente,
processo incompleto ou sessão expirada não basta, isoladamente, para declarar
todo o MCP indisponível.

## 4. Resultados parciais e cobertura

Quando houver dados úteis, a ferramenta deve retornar resultado parcial em vez
de descartar toda a execução. O retorno deve identificar, de forma legível e
estruturada:

- o que foi encontrado;
- o que não foi encontrado;
- campos ausentes;
- documentos não processados;
- nível de cobertura obtido;
- motivo da incompletude.

O resultado parcial deve distinguir ausência confirmada de dado não observado.
Sempre que aplicável, o diagnóstico também registra a etapa afetada, a unidade
isolada, tentativas realizadas e a possibilidade segura de repetição. O
consumidor não deve interpretar resultado parcial como análise integral.

Para inventário e usucapião, a cobertura deve considerar os documentos
acessíveis à sessão, os documentos efetivamente processados e as lacunas que
impediram análise integral. Conflitos encontrados continuam sendo entregues
como achados, mesmo que outro documento ou campo não tenha sido processado.

## 5. Confirmação humana e limites de operação

Em desenvolvimento, a confirmação humana permanece obrigatória somente para
operações judiciais irreversíveis ou que alterem efetivamente o processo,
especialmente:

- assinatura;
- protocolo;
- movimentação processual;
- expedição definitiva;
- exclusão ou alteração destrutiva de dados.

Consulta, abertura e leitura autorizadas, análise, extração estruturada,
auditoria e produção de rascunhos não são bloqueadas por gates de produção nem
por confirmação destinada a atos irreversíveis. A geração de minuta não
autoriza sua assinatura, protocolo ou expedição.

Este plano não habilita escrita judicial que não exista no catálogo público.
Caso uma capacidade de alteração seja introduzida no futuro, ela deverá ter
controle explícito, confirmação humana e trilha de auditoria próprios.

## 6. Estratégia de testes

O desenvolvimento deve permitir repetição e combinação de:

- testes unitários;
- testes integrados com fixtures;
- testes manuais;
- testes de contrato das nove ferramentas;
- testes do painel;
- testes de resultados estruturados e parciais;
- testes de isolamento de ação, processo e sessão;
- canários somente leitura;
- testes reais institucionalmente autorizados.

Uma falha de teste produz evidência para correção e não deve indisponibilizar
globalmente o projeto. O teste deve preservar diagnóstico suficiente para
reprodução, sem registrar credenciais, cookies, tokens, CNJs reais ou conteúdo
sigiloso em artefatos versionados.

### 6.1. Massa sintética

Há seis cenários sintéticos de domínio disponíveis:

- `inventory-basic`;
- `inventory-minor`;
- `inventory-conflict`;
- `adverse-basic`;
- `adverse-neighbors`;
- `adverse-area-conflict`.

Esses golden cases podem ser usados durante todo o desenvolvimento,
independentemente da conclusão do gate de produção ou da designação de
processos reais. Revisões humanas pendentes devem ser registradas como
pendência do aceite formal, sem impedir execução automatizada, investigação de
falhas ou evolução dos diagnósticos.

### 6.2. Canários reais

Ainda faltam processos reais institucionalmente designados para canário. Essa
ausência não impede:

- uso dos seis casos sintéticos;
- testes offline e integrados;
- testes manuais autorizados;
- canários somente leitura que já tenham autorização institucional;
- testes com outros processos reais que tenham sido institucionalmente
  autorizados para desenvolvimento.

CNJs reais não devem ser commitados. No momento de um canário autorizado, eles
devem ser fornecidos por canal seguro e configuração externa ao repositório,
com acesso limitado, rastreabilidade e descarte conforme a política
institucional.

## 7. Diagnóstico de desenvolvimento

O diagnóstico deve ser suficientemente detalhado para descobrir falhas
escondidas, incluindo, quando seguro e aplicável:

- ferramenta e ação executadas;
- etapa em que ocorreu a falha;
- escopo isolado da falha;
- classe do erro e mensagem saneada;
- estado de cobertura e contagens de documentos;
- caminhos de fallback utilizados;
- resultado de tentativas e repetição;
- indicação de resultado completo, parcial ou indisponível;
- correlação técnica sem exposição de segredo ou dado processual protegido.

Erros devem permanecer observáveis. O saneamento de dados sensíveis não pode
transformar uma falha em sucesso aparente nem remover a informação técnica
necessária para reproduzi-la.

## 8. Critérios de homologação e liberação

O gate 20/20, os canários reais designados e as aprovações formais compõem a
decisão de liberar `PJE_ENV=production`. Enquanto o estado for 19/20:

- a pendência deve constar nos relatórios de homologação;
- produção permanece sujeita aos critérios de liberação;
- desenvolvimento permanece utilizável;
- as nove ferramentas continuam testáveis;
- resultados parciais e diagnósticos continuam disponíveis;
- os seis golden cases e os canários autorizados continuam executáveis.

O gate mede prontidão para produção. Ele não substitui as permissões do perfil,
não concede acesso a processos e não funciona como chave geral para testes de
leitura em desenvolvimento.

## 9. Critérios de aceite desta revisão

Antes da publicação deste plano, deve ser confirmado que:

- não existe mecanismo de bloqueio global do MCP;
- homologação de produção não é exigida para testar as ferramentas;
- execução real autorizada pode ser usada para encontrar erros;
- falha localizada está separada de indisponibilidade global;
- desenvolvimento e produção têm políticas claramente diferentes;
- confirmação humana fica reservada às ações irreversíveis ou que alterem
  efetivamente o processo;
- nenhuma ferramenta consultiva fica bloqueada em
  `PJE_ENV=development`;
- pendências de canário real não impedem os golden cases nem os demais testes
  de desenvolvimento;
- o retorno parcial comunica achados, lacunas, cobertura e motivo da
  incompletude.

O aceite deste documento define a política de trabalho e homologação. Mudanças
de runtime necessárias para cumprir o plano devem ser implementadas e
validadas em alterações próprias, sem misturar documentação com código.

## 10. Estado de implementação local

Implementado e coberto por fixtures:

- completude nas cinco dimensões e taxonomia estruturada de lacunas;
- distinção entre expediente vazio confirmado e fonte indisponível;
- manifesto `pje.document-manifest/v2` com ordem, pai/anexo e `source_fields`;
- hash dos bytes originais separado do hash do texto;
- leitura integral sem teto oculto de 200 páginas;
- segunda leitura leve do manifesto e delta de documentos;
- entrega parcial por escopo, sem indisponibilidade global.

Ainda pendente:

- cruzamento do manifesto DOM com endpoint estruturado do PJe;
- visão de diagramas/plantas e interpretação especializada de layout;
- homologação do OCR local com Tesseract e massa documental autorizada;
- extratores especializados de inventário e usucapião;
- lease transacional entre perfis;
- contratos estritos por ação e integração do painel visual separado;
- canários reais institucionalmente designados.
