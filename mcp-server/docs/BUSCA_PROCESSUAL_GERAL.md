# Busca processual geral

## Motivo

Um identificador formado por 11 dígitos não é necessariamente uma solicitação
de busca por CPF. O PJe também expõe a área `Consulta processual` no menu
lateral do painel Angular. Classificar previamente o valor pode levar à tela
errada e produzir um falso “nenhum processo encontrado”.

## Contrato

Use:

```text
buscar_processos_pje(
    acao="busca_geral",
    valor="<identificador>",
    grau="1"
)
```

O fluxo preferencial:

1. abre a casca do painel interno;
2. localiza `iframe#ngFrame`;
3. encontra o `menubar` chamado `Menu lateral do PJe`;
4. aciona o link `Consulta processual`;
5. confirma a rota `#/consulta-processual`;
6. preenche o campo `Número do processo` com o valor original;
7. executa somente a pesquisa e extrai resultados sem abrir ou alterar autos.

Na versão do frontend em que a rota Angular não está disponível ou é
redirecionada para `#/`, a ferramenta usa o item real
`Menu geral → Processo → Processo`. Essa é a consulta processual geral legada
do PJe. O fallback:

1. volta ao início;
2. localiza no DOM o link oficial
   `/Processo/ConsultaProcesso/listView.seam`;
3. aciona esse item mesmo quando o menu está recolhido;
4. escolhe o campo do formulário pelo formato disponível;
5. informa em `criterio_aplicado` qual campo foi usado;
6. devolve somente linhas que contenham número CNJ.

Um valor de 11 dígitos é aplicado ao campo CPF nesse fallback; 14 dígitos, ao
campo CNPJ; 20 dígitos, aos campos do número CNJ. A resposta torna essa escolha
explícita, evitando o falso total causado por linhas auxiliares da página.

No modo `auto`, sequências numéricas ambíguas de 11 ou 14 dígitos seguem para
`busca_geral`; no formulário legado, o critério efetivamente aplicado sempre
aparece na resposta.

## Escopo

A rota e os locators possuem evidência positiva somente para o primeiro grau.
O segundo grau falha fechado até ser mapeado em sessão autorizada. A ferramenta
é estritamente somente leitura.

## Critérios enriquecidos da consulta nativa

Além de CNJ, CPF e CNPJ, `buscar_processos_pje` aceita:

- `acao="oab"`: número, letra complementar opcional e UF, por exemplo
  `12345-A/PA`;
- `acao="nome_parte"`: qualquer polo;
- `acao="nome_requerente"` (aliases `autor` e `requerente`): preenche
  `Nome da Parte` e conserva somente coincidências no `Polo ativo`;
- `acao="nome_requerido"` (aliases `reu` e `requerido`): preenche
  `Nome da Parte` e conserva somente coincidências no `Polo passivo`;
- `acao="nome_advogado"` (alias `representante`);
- `acao="outros_nomes"` (alias `alcunha`);
- `acao="numero_documento"`.

Também foram mapeados os demais campos da consulta avançada:

- `assunto` e `classe_judicial`: texto informado nos campos nativos;
- `jurisdicao`, `orgao_julgador` e `prioridade_processual`: seleção por
  rótulo completo, trecho inequívoco ou código interno da opção;
- `data_autuacao`: uma data (`01/01/2024`) ou faixa
  (`01/01/2024..31/12/2024`); o fim também pode ser passado em
  `valor_final`;
- `valor_causa`: um valor ou faixa (`1.000,00..5.000,00`), com validação de
  ordem e formato antes da navegação;
- `movimento_processual` (aliases `movimento`, `movimentacao` e
  `movimentacao_processual`): exige que o autocompletar do PJe reconheça e
  selecione uma opção; texto sem correspondência falha fechado;
- `orgao_origem_criminal`: abre primeiro a seção recolhida `Filtros Criminais`
  e seleciona pela lista disponibilizada pelo PJe. Na sessão real de
  01/08/2026 essa lista veio sem opções para o perfil ativo; nesse estado a
  automação falha explicitamente, em vez de simular uma seleção;
- `procedimento_criminal`: número com ano opcional, por exemplo
  `12345/2024`; o ano também pode ser passado em `valor_final`;
- `ano_procedimento_criminal` e `protocolo_policia`.

O bridge Node expõe os mesmos nomes em
`automacao_navegador_pje(acao="buscar", criterio=...)`. Assim, o dicionário do
MCP Python e o da automação Playwright permanecem alinhados. O alias
`documento` significa `numero_documento` nas duas rotas.

### Exclusão deliberada

`processo_referencia` não integra este mapeamento. Os controles “Numeração
única” e “Livre” pertencem a esse mesmo bloco e também ficaram fora. A ação é
rejeitada explicitamente até que um processo de referência real exija esse
fluxo, conforme a orientação operacional atual.

O filtro por polo percorre a paginação necessária até atingir o limite pedido
ou esgotar os resultados disponíveis. Um vazio só é confirmado quando todas as
linhas relevantes foram lidas; caso contrário, a resposta permanece
inconclusiva.
