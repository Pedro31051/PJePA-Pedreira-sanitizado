'use strict';

module.exports = Object.freeze({
  consultaUrl: 'https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam',
  inputNumeroProcesso: [
    "input[id*='numeroProcesso']",
    "input[name*='numeroProcesso']",
    "input[id*='numProcesso']",
    "input[name*='numProcesso']",
  ].join(', '),
  inputNomeParte: "input[id*='nomeParte'], input[name*='nomeParte']",
  inputOutrosNomes: [
    "input[id*='outrosNomesAlcunha']",
    "input[name*='outrosNomesAlcunha']",
  ].join(', '),
  inputNomeAdvogado: [
    "input[id*='nomeRepresentante']",
    "input[name*='nomeRepresentante']",
    "input[id*='nomeAdvogado']",
    "input[id*='representante']",
  ].join(', '),
  inputDocumentoParte: [
    "input[id='fPP:dpDec:documentoParte']",
    "input[id*='documentoParte']",
    "input[name*='documentoParte']",
  ].join(', '),
  radioCpf: "input#cpf, input[type='radio'][id$='cpf']",
  radioCnpj: "input#cnpj, input[type='radio'][id$='cnpj']",
  inputNumeroOab: [
    "input[id*='numeroOAB']",
    "input[name*='numeroOAB']",
    "input[id*='oab'][type='text']",
  ].join(', '),
  inputLetraOab: [
    "input[id*='letraOAB']",
    "input[name*='letraOAB']",
  ].join(', '),
  selectUfOab: [
    "select[id*='ufOAB']",
    "select[name*='ufOAB']",
    "select[id*='estadoOAB']",
  ].join(', '),
  inputNumeroDocumento: [
    "input[id*='numeroDocumento']",
    "input[name*='numeroDocumento']",
  ].join(', '),
  inputAssunto: "input[id*=':assunto'], input[name*=':assunto']",
  inputClasseJudicial: [
    "input[id*='classeJudicial']",
    "input[name*='classeJudicial']",
  ].join(', '),
  selectJurisdicao: [
    "select[id*='jurisdicaoCombo']",
    "select[name*='jurisdicaoCombo']",
  ].join(', '),
  selectOrgaoJulgador: [
    "select[id*='orgaoJulgadorCombo']",
    "select[name*='orgaoJulgadorCombo']",
  ].join(', '),
  selectPrioridadeProcessual: [
    "select[id*='prioridadeProcessualCombo']",
    "select[name*='prioridadeProcessualCombo']",
  ].join(', '),
  inputDataAutuacaoInicio: "input[id*='dataAutuacaoInicioInputDate']",
  inputDataAutuacaoFim: "input[id*='dataAutuacaoFimInputDate']",
  inputValorCausaInicio: "input[id*='valorCausaInicial']",
  inputValorCausaFim: "input[id*='valorCausaFinal']",
  inputMovimentoProcessual: [
    "input[id*='movimentacaoProcessualSuggest']",
    "input[name*='movimentacaoProcessualSuggest']",
  ].join(', '),
  sugestoesMovimentoProcessual: [
    "[id*='movimentacaoProcessualSuggest'] + div tr.rich-sb-int",
    "div[id*='j_id418'] tr.rich-sb-int",
    "table[id*='j_id418'][id$='suggest'] tr.rich-sb-int",
  ].join(', '),
  selectOrgaoOrigemCriminal: [
    "select[id*='orgaoOrigemCriminal']",
    "select[name*='orgaoOrigemCriminal']",
  ].join(', '),
  cabecalhoFiltrosCriminais: [
    "div.rich-stglpanel:has-text('Filtros Criminais') .rich-stglpanel-header",
    "div[id*='j_id424'] .rich-stglpanel-header",
  ].join(', '),
  inputNumeroProcedimentoCriminal: "input[id$=':numeroProcedCriminal']",
  inputAnoProcedimentoCriminal: "input[id$=':anoProcedCriminal']",
  inputProtocoloPolicia: "input[id*='numeroProtocoloPolicia']",
  botaoPesquisar: "button[id*='btnPesquisar'], input[id*='btnPesquisar'], #fPP\\:searchProcessos",
  tabelaResultados: [
    "table[id*='processosTable']",
    "table[id*='processoTable']",
    "[id*='processoList']",
  ].join(', '),
  avisoSemResultados: [
    '.rich-messages .warn',
    '.alert-warning',
    '.mensagem-erro',
    "[class*='no-results']",
    "[class*='sem-resultado']",
  ].join(', '),
  marcadoresSessao: [
    'li.menu-usuario',
    'a[href*="logout"]',
    'a.dropdown-toggle',
    '[id*="usuarioLogado"]',
    '[class*="usuario-logado"]',
    '[data-testid="user-menu"]',
  ].join(', '),
  marcadoresLogin: [
    '#kc-form-login',
    'input[type="password"]',
    'input[autocomplete="one-time-code"]',
  ].join(', '),
  documentoContainers: [
    '.timeline-item',
    '.detalhe-documento',
    '.documento-conteudo',
    '#divConteudoProcesso',
    '#conteudoProcesso',
    '.tarefa-conteudo',
  ],
});
