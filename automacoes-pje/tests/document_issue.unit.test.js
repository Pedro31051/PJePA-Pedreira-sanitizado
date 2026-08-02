'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { classifyIssueCatalog, validateDocumentIssuePlan } = require('../pje/document_issue');

test('classifica tipos e modelos contextuais da expedição', () => {
  const result = classifyIssueCatalog(
    [{ value: '19', label: 'Ofício' }, { value: '6', label: 'Certidão' }],
    [{ value: '42', label: 'FAM - Ofício DESCONTO EM FOLHA - Alimentos' }],
  );
  assert.equal(result.document_types_total, 2);
  assert.equal(result.models_total, 1);
  assert.equal(result.family_models.length, 1);
  assert.equal(result.food_support_models.length, 1);
});

test('rejeita documento externo no fluxo Expedir documento', () => {
  const result = validateDocumentIssuePlan({
    document_origin: 'externo', document_type: 'Ofício', draft_text: 'teste',
    movement_code: 60, movement_complement: 'Ofício',
  });
  assert.ok(result.blockers.some((item) => item.code === 'WRONG_WORKFLOW_EXTERNAL_DOCUMENT'));
});

test('exige correspondência entre tipo e complemento do movimento 60', () => {
  const result = validateDocumentIssuePlan({
    document_origin: 'interno', document_type: 'Ofício', draft_text: 'conteúdo',
    movement_code: 60, movement_complement: 'Certidão',
  });
  assert.ok(result.blockers.some((item) => item.code === 'MOVEMENT_COMPLEMENT_MISMATCH'));
  assert.equal(result.signing_available, false);
});

test('mantém comunicação como etapa separada', () => {
  const result = validateDocumentIssuePlan({
    document_origin: 'interno', document_type: 'Ofício', draft_text: 'conteúdo',
    movement_code: 60, movement_complement: 'Ofício', prepare_communication: true,
  });
  assert.equal(result.status, 'ready_for_draft_review');
  assert.ok(result.warnings.some((item) => item.code === 'COMMUNICATION_IS_SEPARATE_STEP'));
  assert.equal(result.final_action_available, false);
});
