'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { classifyModels, validateCommunicationPlan } = require('../pje/communications');

test('classifica catálogo contextual sem inventar modelo ausente', () => {
  const result = classifyModels([
    { value: '92', label: 'FAMILIA_Conciliacao-Int-Partes_Hibr_Ec' },
    { value: '95', label: 'FAMILIA_Conciliacao-Int-Partes_Pres_Oj' },
    { value: '1', label: 'Modelo padrão de intimação' },
  ]);
  assert.equal(result.total, 3);
  assert.equal(result.family.length, 2);
  assert.equal(result.hybrid.length, 1);
  assert.equal(result.in_person.length, 1);
  assert.equal(result.citation.length, 0);
  assert.equal(result.suffix_interpretation_verified, false);
});

test('bloqueia citação de requerente e falta de evidência', () => {
  const result = validateCommunicationPlan({
    process_analysis: {},
    communications: [{
      recipient_role: 'Requerente', type: 'Citação', medium: 'Sistema', deadline_days: 15,
    }],
  });
  assert.equal(result.status, 'blocked');
  assert.ok(result.blockers.some((item) => item.code === 'REQUESTER_CITATION_FORBIDDEN'));
  assert.ok(result.blockers.some((item) => item.code === 'CITATION_ORDER_NOT_EVIDENCED'));
  assert.equal(result.final_action_available, false);
});

test('exige MP quando há interesse de menor e prazo operacional de 30 dias', () => {
  const missing = validateCommunicationPlan({
    process_analysis: { minor_interest: true },
    communications: [{
      recipient_role: 'Requerido', type: 'Intimação', medium: 'Sistema', deadline_days: 15,
    }],
  });
  assert.ok(missing.blockers.some((item) => item.code === 'MP_INTIMATION_REQUIRED'));

  const valid = validateCommunicationPlan({
    process_analysis: { minor_interest: true, legal_review_completed: true },
    communications: [
      { recipient_role: 'Requerido', type: 'Intimação', medium: 'Sistema', deadline_days: 15 },
      { recipient_role: 'Ministério Público', type: 'Intimação', medium: 'Sistema', deadline_days: 30 },
    ],
  });
  assert.equal(valid.status, 'ready_for_human_review');
});

test('impede lote com centrais de mandados diferentes', () => {
  const result = validateCommunicationPlan({
    process_analysis: { legal_review_completed: true },
    communications: [
      { recipient_role: 'Requerido', type: 'Intimação', medium: 'Central de Mandados', central: 'Belém', deadline_days: 15, batch_key: 'lote-1' },
      { recipient_role: 'Requerente', type: 'Intimação', medium: 'Central de Mandados', central: 'Ananindeua', deadline_days: 15, batch_key: 'lote-1' },
    ],
  });
  assert.ok(result.blockers.some((item) => item.code === 'UNSAFE_BATCH'));
});

test('reconhece escritório universitário como destinatário de prazo operacional dobrado', () => {
  const result = validateCommunicationPlan({
    process_analysis: { legal_review_completed: true },
    communications: [{
      recipient_role: 'Escritório junto às universidades',
      type: 'Intimação', medium: 'Sistema', deadline_days: 30,
    }],
  });
  assert.equal(result.status, 'ready_for_human_review');
});

test('modelo híbrido exige decisão revisada e modo confirmado', () => {
  const result = validateCommunicationPlan({
    process_analysis: { hearing_mode: 'presencial' },
    communications: [{
      recipient_role: 'Requerente', type: 'Intimação', medium: 'Sistema', deadline_days: 15,
      model: 'FAMILIA_Conciliacao-Int-Partes_Hibr_Ec',
    }],
  });
  assert.ok(result.blockers.some((item) => item.code === 'DECISION_REVIEW_REQUIRED'));
  assert.ok(result.blockers.some((item) => item.code === 'HEARING_MODE_MISMATCH'));
});
