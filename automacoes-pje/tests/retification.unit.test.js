'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { INSTITUTIONS, normalizeChanges } = require('../pje/retification');

test('normaliza uma edição de características', () => {
  assert.deepEqual(normalizeChanges({
    caracteristicas: {
      tutela_liminar: true,
      valor_causa: '1250,00',
      prioridade: 'Idoso',
    },
  }), {
    caracteristicas: {
      tutela_liminar: true,
      valor_causa: '1250,00',
      prioridade: 'Idoso',
    },
  });
});

test('normaliza edição de uma parte sem transportar campos desconhecidos', () => {
  assert.deepEqual(normalizeChanges({
    partes: [{ polo: 'ATIVO', indice: 0, dados: { nome_social: 'Nome Social' } }],
  }), {
    partes: [{ polo: 'ativo', indice: 0, dados: { nome_social: 'Nome Social' } }],
  });
  assert.throws(() => normalizeChanges({
    partes: [{ polo: 'ativo', indice: 0, dados: { campo_inventado: 'x' } }],
  }), /campos não permitidos/);
});

test('rejeita mais de uma seção por confirmação', () => {
  assert.throws(() => normalizeChanges({
    dados_iniciais: { classe_judicial: 'Procedimento Comum' },
    caracteristicas: { justica_gratuita: true },
  }), /uma seção por confirmação/);
});

test('rejeita inclusão ambígua de vários assuntos', () => {
  assert.throws(() => normalizeChanges({
    assuntos: { adicionar: [{ codigo: '1' }, { codigo: '2' }] },
  }), /um assunto por confirmação/i);
});

test('normaliza edição controlada de endereço existente', () => {
  assert.deepEqual(normalizeChanges({
    partes: [{
      polo: 'ATIVO',
      indice: 0,
      endereco: {
        acao: 'editar',
        indice: 1,
        dados: { logradouro: 'Rua Nova', numero: '10', correspondencia: true },
      },
    }],
  }), {
    partes: [{
      polo: 'ativo',
      indice: 0,
      endereco: {
        acao: 'editar',
        indice: 1,
        dados: { logradouro: 'Rua Nova', numero: '10', correspondencia: true },
      },
    }],
  });
});

test('normaliza inclusão e remoção de contato', () => {
  assert.deepEqual(normalizeChanges({
    partes: [{
      polo: 'passivo', indice: 2,
      contato: { acao: 'adicionar', tipo: 'Telefone Celular', valor: '(91) 99999-9999' },
    }],
  }), {
    partes: [{
      polo: 'passivo', indice: 2,
      contato: { acao: 'adicionar', tipo: 'Telefone Celular', valor: '(91) 99999-9999' },
    }],
  });
  assert.deepEqual(normalizeChanges({
    partes: [{ polo: 'outros', indice: 0, contato: { acao: 'remover', indice: 1 } }],
  }), {
    partes: [{ polo: 'outros', indice: 0, contato: { acao: 'remover', indice: 1 } }],
  });
});

test('fixa a identidade institucional do MPPA e exige fundamento', () => {
  assert.equal(INSTITUTIONS.mppa.cnpj, '05.054.960/0001-58');
  assert.deepEqual(normalizeChanges({
    ministerio_publico: { acao: 'remover', fundamento: 'Determinação judicial expressa.' },
  }), {
    ministerio_publico: {
      acao: 'remover',
      fundamento: 'Determinação judicial expressa.',
      instituicao: {
        nome: 'MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ',
        cnpj: '05.054.960/0001-58',
      },
    },
  });
  assert.throws(() => normalizeChanges({
    ministerio_publico: { acao: 'adicionar', fundamento: '' },
  }), /fundamento/i);
});
