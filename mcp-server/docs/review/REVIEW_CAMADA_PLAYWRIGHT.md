# Revisão incremental — camada de automação Playwright

Data: 2026-07-28
Estado: implementado e validado offline
Escopo: `src/pje_client.py` e `src/cliente_singleton.py`, com verificação de
runtime somente leitura no endpoint produtivo.

## Método

O caderno `mcp pje` foi usado como fonte de contexto no contrato
`pje-agent-context/v2`. Cada afirmação recuperada foi tratada como hipótese e
confirmada — ou refutada — contra o código atual e o runtime. Nenhuma conclusão
deste relatório repousa apenas na evidência do caderno.

## Hipóteses do caderno que NÃO se confirmaram

O caderno classificou como crítico o vazamento de descritores por ausência de
`APIResponse.dispose()` (incidente EMFILE de 27/07, 337 filhos Playwright).

Isso **já estava corrigido**. Os seis pontos que consomem `APIResponse` chamam
`dispose()`, cinco deles dentro de `finally`:

| Arquivo | Linha | Situação |
|---|---:|---|
| `pje_client.py` | 1428 | `dispose()` presente |
| `pje_client.py` | 2017 | `dispose()` presente |
| `pje_client.py` | 2046 | `dispose()` presente |
| `pje_client.py` | 3378 | `dispose()` em `finally` |
| `pje_client.py` | 3442 | `dispose()` em `finally` |
| `pje_client.py` | 4141 | `dispose()` presente |
| `pje_downloader.py` | 113 | `dispose()` presente |

Auditoria de runtime no endpoint produtivo, com sessão ativa há 101 s:

```json
"descritores_abertos": 12,
"limite_descritores_soft": 65535,
"uso_descritores_percentual": 0.02,
"subprocessos_filhos": 1
```

O achado histórico foi rebaixado a **resolvido**. Registrar isso importa: repetir
um achado já sanado como se fosse atual contamina a próxima revisão.

Também foi verificada por AST a hipótese de deadlock do lock não-reentrante
(`_serializa`): nenhum método decorado chama outro método decorado, e os dois
usos diretos de `_op_lock` em `server.py` (5522 e 5734) só executam
`new_page`/`goto`/`pdf`. **Sem caminho de deadlock.**

## Achados confirmados

### P1 — Handlers de diálogo acumulavam por aba, retendo `Page` fechadas

`_setup_dialog_handler` era registrado no evento `page` do contexto e anexava
uma entrada a `self._dialog_handlers` a cada aba aberta. A lista só era limpa em
`_fechar`. Como o singleton mantém a sessão quente por até 1800 s e reusa o mesmo
cliente entre tool calls, cada processo aberto deixava uma entrada permanente
com referência forte à `Page` já fechada.

Reprodução com 200 processos abertos e fechados:

```text
antes:  handlers=201   Pages fechadas ainda vivas=200/200
depois: handlers=1     Pages fechadas ainda vivas=0/200
```

Severidade: média. Não consome descritores — as abas são de fato fechadas — mas
o crescimento é linear no número de processos por sessão e proporcional a
operações em lote (`ler_documentos_em_lote`, `coletar_processo_integral`,
`buscar_processos_pje(acao='lote')` com até 25 itens).

### P2 — Em modo CDP, a limpeza de abas fechava abas do usuário

`_fechar_aba_autos` fechava toda página do contexto que não fosse `self._page`.
Em `launch` isso é correto: o contexto pertence ao cliente. Em CDP,
`self._context = self._browser.contexts[0]` é o contexto **do usuário**, e a
limpeza derrubava as abas que ele já tinha abertas — contradizendo o comentário
de `_iniciar` ("não sequestra nem fecha a aba do usuário").

Severidade: **latente, não ativa em produção**. Nenhum arquivo de deploy define
`PJE_CDP_URL`; o serviço usa `launch`. O caminho existe, é exercitado por teste
e é o modo usado em automação local, então foi corrigido.

### P3 — Fallback de credenciais abortava no primeiro erro de backend

`_creds_keyring` percorria `mcp-pje-tjpa`, `mcp-pje-tjma` e `mcp-pje-tjpi`, mas
`return None` dentro do `except` descartava os dois fallbacks quando o primeiro
service levantasse erro de backend. Como a conta do SSO nacional PDPJ é a mesma
para os três, o efeito era exigir reconfiguração desnecessária.

Severidade: baixa. Em produção as credenciais vêm de `systemd LoadCredential`,
caminho que nem chega ao keyring.

### P4 — Contrato divergente em `download_e_cache_pje`

`verificar_integridade` e `listar_cache` são documentadas como "só disco, não
abre o PJe", e `listar_cache` como "nada obrigatório". O dispatcher aplica
`_ativar_perfil_ferramenta` antes de qualquer ação (linha 7233), então ambas
retornam `PERFIL_OBRIGATORIO` para usuário interno sem perfil selecionado.

Confirmado em runtime:

```json
{"erro": "perfil funcional obrigatório para usuário interno",
 "codigo": "PERFIL_OBRIGATORIO", "persona": "servidor"}
```

**O gate não foi removido.** Ele é defensável: `verificar_integridade` devolve
nomes de arquivo que são CNJs, ou seja, expõe quais processos existem no cache.
Exigir declaração da lotação antes disso é rastreabilidade institucional, não
excesso. O defeito é a **documentação**, que promete uma ação sem pré-condição.
A correção pertence ao texto do contrato, não ao controle — enfraquecer a
proteção para o teste passar seria a troca errada.

Severidade: baixa (contrato), nenhuma quanto à segurança.

## Mudanças implementadas

- `_descartar_handlers_de_abas_fechadas()` poda entradas cujas abas já fecharam,
  chamada no registro de cada handler e ao fim de `_fechar_aba_autos`.
- `_esquecer_aba()` ligado a `page.once("close", ...)` solta a referência assim
  que a aba morre, sem esperar a varredura seguinte.
- `_abas_preexistentes` fotografa as abas do usuário em `_iniciar` (modo CDP,
  antes de abrir a aba própria); `_fechar_aba_autos` nunca as fecha quando o
  contexto não pertence ao cliente. Em `launch` o comportamento é inalterado.
- `_fechar` limpa `_abas_preexistentes` junto com os handlers.
- `_creds_keyring` usa `continue` e chega aos services de fallback.
- Comentário de `cliente_singleton.get_cliente` corrigido: afirmava que o cliente
  "sempre conecta via CDP", o que contradiz `_iniciar`, onde `launch` é o padrão.

## Testes adicionados

Em `tests/test_resource_lifecycle.py`:

1. 50 abas abertas e fechadas deixam exatamente 1 handler registrado.
2. `Page` fechada é coletada pelo GC — verificado por `weakref`.
3. Modo CDP: aba dos autos fecha, aba preexistente do usuário sobrevive.
4. Modo `launch`: abas residuais continuam sendo limpas (sem regressão).
5. Erro de backend no primeiro keyring service não impede o fallback.

Os três primeiros foram submetidos a mutação — com a correção neutralizada em
runtime, todos falham. Não são testes vazios.

## Resultado da suíte

```text
146 passed
2 skipped
39 subtests passed
4 warnings de depreciação do lxml
```

Baseline antes das mudanças: 126 passed, 2 skipped, 39 subtests.

Do saldo de +20, doze são desta revisão (5 de ciclo de vida de abas e
credenciais, 7 de reautenticação). Os demais vieram de uma sessão paralela que
editou `src/server.py` durante esta revisão — mtime 14:28, posterior ao início
deste ciclo. Os arquivos tocados aqui (`pje_client.py`, `cliente_singleton.py`,
`tests/test_resource_lifecycle.py`) não colidiram, e o teste de catálogo segue
verde com nove ferramentas.

## Segunda onda — reautenticação na navegação e contrato

### P5 — Sessão expirada durante a navegação não era detectada

Só a rota de API de caixas tratava expiração (HTTP 401/403 → `_login()`). Na
navegação o PJe não devolve 401: ele **redireciona** a aba para o SSO do PDPJ
ou de volta para `login.seam`. O scraping seguia adiante raspando a tela de
autenticação e devolvia "processo não encontrado" — falha silenciosa na direção
perigosa, porque o agente conclui ausência de processo onde há sessão morta.

Implementado em `_abrir_autos_processo`, que é o funil único de 11 call sites
(`buscar_processo`, `relatorio_processo`, `listar_documentos`,
`ler_documentos_em_lote`, `coletar_processo_integral`, `baixar_processo_nativo`
e demais):

- `_sessao_expirou(url)` reconhece `sso.cloud.pje.jus.br`, `/auth/realms/`
  (Keycloak) e `/login.seam`.
- `_reautenticar()` refaz `_login()`, `_trocar_perfil()` e **reseleciona a
  lotação**, abortando com `PERFIL_DIVERGENTE` se o PJe confirmar outro perfil.
  Sem isso, reautenticar poderia devolver a sessão lendo a unidade errada.
- Retentativa única: a chamada recursiva recebe `permitir_relogin=False`, e uma
  segunda tela de login vira `SESSAO_EXPIRADA` em vez de laço.
- Carência de 60 s após relogin malsucedido: a senha do PDPJ expira
  periodicamente e insistir a cada tool call bloquearia a conta no SSO nacional.
  A falha é marcada antes de propagar, para valer mesmo sob cancelamento.
- Modo CDP não tenta relogin: a sessão pertence ao Chrome do usuário (A3), e o
  erro instrui o login manual.

### Onde o caderno teve de ser filtrado

A resposta do caderno recomendou como **P0 crítico** criar um `asyncio.Lock`
para serializar o relogin e evitar "tempestade de login". Isso **já está
resolvido**: `_serializa` põe todo método público sob `_op_lock` e o singleton
mantém um único cliente, logo duas reautenticações simultâneas são impossíveis.
Adicionar outro lock seria redundância sem efeito. O risco real não era
concorrência, e sim **repetição em série** contra credencial inválida — daí a
carência ter sido a proteção escolhida.

O mesmo trecho citou URLs do TRF1 (`pje1g.trf1.jus.br`) e handshake mTLS com
certificado A1 via `client_certificates`. Nada disso se aplica: o TJPA usa
`pje.tjpa.jus.br` e autenticação CPF + senha + TOTP. Fontes comparativas de
outros tribunais estavam sendo recuperadas como se descrevessem este projeto.

### P4 — Contrato de `download_e_cache_pje` corrigido no texto

O docstring dizia "listar_cache: nada obrigatório" e "só disco, não abre o PJe",
mas o dispatcher aplica `_ativar_perfil_ferramenta` a todas as ações. O texto
agora declara a pré-condição e explica o motivo: os retornos expõem nomes de
arquivo que são CNJs, revelando quais processos existem no cache.

**O controle não foi tocado.** Corrigir a documentação era a única mudança
legítima; afrouxar o gate para a ação "passar" trocaria uma proteção de dados
processuais por conveniência.

## Testes da segunda onda

Sete testes em `ReautenticacaoNavegacaoTests`:

1. Detecção de tela de autenticação (SSO, Keycloak, `login.seam`) e negativos.
2. Redirect ao SSO dispara exatamente um relogin e repete a navegação uma vez.
3. Sessão ainda expirada após relogin falha com `SESSAO_EXPIRADA`, sem laço.
4. Modo CDP não chama `_login()`.
5. Carência bloqueia a segunda tentativa sem tocar no SSO.
6. Relogin bem-sucedido limpa a carência.
7. Perfil divergente após relogin interrompe e também entra em carência.

Submetidos a mutação: com `_sessao_expirou` forçado a `False` e com a carência
zerada, os quatro testes correspondentes falham.

## Limites desta etapa

- Nenhum canário real foi executado. A validação usa dublês de `Page`/`Context`
  e dados sintéticos.
- A auditoria de runtime foi somente leitura e não abriu autos. O serviço em
  produção roda o código de `/opt/pjepa-mcp`, anterior a estas correções: os
  números de descritores acima descrevem o estado **pré-correção**, e são
  evidência de que P1 não causava exaustão de descritores.
- `_dialog_handlers` agora é podado, mas o limite superior dentro de uma única
  operação em lote não foi medido contra o PJe real.
- A reautenticação foi validada apenas com URLs sintéticas. **Os marcadores de
  expiração são a hipótese mais frágil desta entrega**: `sso.cloud.pje.jus.br`,
  `/auth/realms/` e `/login.seam` derivam do código de login existente e da
  documentação, não de uma sessão realmente expirada no TJPA. Se o PJe usar
  outro caminho de redirect, a detecção não dispara e o comportamento volta a
  ser o anterior — falha silenciosa, não regressão. Confirmar exige deixar uma
  sessão expirar e capturar a URL resultante.
- O TTL de sessão do Keycloak do TJPA continua desconhecido. A carência de 60 s
  é uma escolha conservadora, não um valor medido.
- A reautenticação só cobre o funil `_abrir_autos_processo`. Rotas que navegam
  fora dele — painel de expedientes, sincronização de caixas — mantêm o
  comportamento anterior.

## Rollback

Primeira onda: reverter `_descartar_handlers_de_abas_fechadas`, `_esquecer_aba`,
`_abas_preexistentes`, o `continue` em `_creds_keyring` e os cinco testes.

Segunda onda: reverter `MARCADORES_SESSAO_EXPIRADA`, `COOLDOWN_RELOGIN_S`,
`_sessao_expirou`, `_reautenticar`, o parâmetro `permitir_relogin` de
`_abrir_autos_processo`, o campo `_relogin_falhou_em` e os sete testes. O texto
do contrato de `download_e_cache_pje` é independente e pode ficar.

As duas ondas são reversíveis em separado. Não há migração de banco, alteração
de cache, mudança de assinatura pública nem efeito externo a desfazer. O
catálogo permanece com nove ferramentas e nenhuma capacidade de assinatura,
protocolo ou movimentação foi criada.
