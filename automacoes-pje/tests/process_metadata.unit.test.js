'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { COMMANDS } = require('../pje/contracts');
const { normalizeLabelChange, stateHash } = require('../pje/process_metadata');

test('publica leitura e alteração confirmada de etiquetas', () => {
  assert.ok(COMMANDS.includes('inspect_process_metadata'));
  assert.ok(COMMANDS.includes('preview_process_label_change'));
  assert.ok(COMMANDS.includes('apply_process_label_change'));
});

test('normaliza uma única alteração de etiqueta', () => {
  assert.deepEqual(normalizeLabelChange({ acao: ' Adicionar ', etiqueta: '  PRAZO  ' }), {
    action: 'adicionar',
    label: 'PRAZO',
    taskBox: 'Verificar providência a adotar',
  });
  assert.throws(() => normalizeLabelChange({ acao: 'criar', etiqueta: 'PRAZO' }),
    error => error.code === 'INVALID_REQUEST');
});

test('fingerprint muda quando etiqueta, situação ou lembrete muda', () => {
  const base = {
    process_number: '0000000-00.0000.0.00.0000',
    labels: [{ name: 'AGUARDAR PRAZO' }],
    situations: [],
    reminders: { current_document_id: '185032203', items: [] },
  };
  assert.notEqual(stateHash(base), stateHash({
    ...base,
    reminders: { ...base.reminders, items: [{ text: 'Conferir documento' }] },
  }));
});
