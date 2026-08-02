'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { COMMANDS } = require('../pje/contracts');
const { classifyCatalog, normalizeLabel } = require('../pje/document_join');

test('o bridge publica o comando somente preparatório de juntada', () => {
  assert.ok(COMMANDS.includes('inspect_document_join'));
});

test('normaliza rótulos com acentos sem perder a semântica', () => {
  assert.equal(normalizeLabel('  Certidão de Trânsito em Julgado '),
    'certidao de transito em julgado');
});

test('separa tipos de certidão e modelos específicos da Vara de Família', () => {
  const result = classifyCatalog([
    'Selecione',
    'Ato Ordinatório',
    'Certidão',
    'Certidão da Contadoria',
    'Ofício',
  ], [
    'Selecione um modelo',
    'CIVEL_Certidao-Ausencia-Contestacao',
    'FAM - JUNTADA - DOCUMENTOS DIVERSOS',
    'FAM - PRAZO - RECURSO - TEMPESTIVO',
  ]);
  assert.deepEqual(result.certificate_types, ['Certidão', 'Certidão da Contadoria']);
  assert.deepEqual(result.family_models, [
    'FAM - JUNTADA - DOCUMENTOS DIVERSOS',
    'FAM - PRAZO - RECURSO - TEMPESTIVO',
  ]);
  assert.equal(result.document_types.length, 4);
  assert.equal(result.models.length, 3);
});
