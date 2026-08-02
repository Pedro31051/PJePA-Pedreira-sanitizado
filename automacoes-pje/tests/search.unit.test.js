'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

process.env.PJE_BROWSER_CONFIG = path.join(
  __dirname,
  'fixtures',
  'browser_config_stub.js',
);

const {
  filterResultsByPartyRole,
  normalizeSearchPayload,
} = require('../pje/operations');

test('normaliza aliases de autor e réu para filtros de polo', () => {
  assert.equal(
    normalizeSearchPayload({ criterion: 'autor', value: 'Maria da Silva' }).criterion,
    'nome_requerente',
  );
  assert.equal(
    normalizeSearchPayload({ criterion: 'réu', value: 'João de Souza' }).criterion,
    'nome_requerido',
  );
});

test('preserva número, letra complementar e UF da OAB', () => {
  assert.deepEqual(
    normalizeSearchPayload({ criterion: 'oab', value: '12345-A/PA' }),
    {
      criterion: 'oab',
      value: '12345',
      letra_oab: 'A',
      uf_oab: 'PA',
      limit: 20,
    },
  );
});

test('normaliza intervalos de data e valor sem mapear processo de referência', () => {
  assert.deepEqual(
    normalizeSearchPayload({
      criterion: 'data_autuacao',
      value: '01/01/2024..31/12/2024',
    }),
    {
      criterion: 'data_autuacao',
      value: '01/01/2024',
      value_end: '31/12/2024',
      limit: 20,
    },
  );
  assert.deepEqual(
    normalizeSearchPayload({
      criterion: 'valor_causa',
      value: 'R$ 1.000,00',
      value_end: '5.000,00',
    }),
    {
      criterion: 'valor_causa',
      value: 'R$ 1.000,00',
      value_end: '5.000,00',
      limit: 20,
    },
  );
  assert.throws(
    () => normalizeSearchPayload({ criterion: 'processo_referencia', value: '123' }),
    /Critério de busca inválido/,
  );
});

test('normaliza aliases e número/ano do procedimento criminal', () => {
  assert.equal(
    normalizeSearchPayload({ criterion: 'movimentação processual', value: 'Audiência' })
      .criterion,
    'movimento_processual',
  );
  assert.deepEqual(
    normalizeSearchPayload({ criterion: 'procedimento_criminal', value: '12345/2024' }),
    {
      criterion: 'procedimento_criminal',
      value: '12345',
      value_end: '2024',
      limit: 20,
    },
  );
});

test('requerido confere somente o polo passivo sem confundir o autor', () => {
  const rows = [
    { numero_cnj: '1', polo_ativo: 'Maria da Silva', polo_passivo: 'João de Souza' },
    { numero_cnj: '2', polo_ativo: 'João de Souza', polo_passivo: 'Empresa Sintética' },
  ];

  const filtered = filterResultsByPartyRole(rows, {
    criterion: 'nome_requerido',
    value: 'João de Souza',
  });

  assert.equal(filtered.roleField, 'polo_passivo');
  assert.deepEqual(filtered.results.map(item => item.numero_cnj), ['1']);
});
