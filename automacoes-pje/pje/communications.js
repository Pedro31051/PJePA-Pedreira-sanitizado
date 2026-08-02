'use strict';

const { PJeBridgeError } = require('./contracts');

const POLICY_SOURCE = 'instrucoes_operacionais_do_usuario';
const FINAL_ACTIONS = Object.freeze([
  'expedir', 'finalizar', 'assinar', 'enviar', 'intimar', 'citar',
]);

function cleanText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function fold(value) {
  return cleanText(value).normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
}

function classifyModels(models = []) {
  const normalized = models.map((model) => {
    if (model && typeof model === 'object') {
      return { value: cleanText(model.value), label: cleanText(model.label) };
    }
    return { value: '', label: cleanText(model) };
  }).filter((model) => model.label && fold(model.label) !== 'selecione o modelo');
  const matching = (expressions) => normalized.filter((model) => {
    const label = fold(model.label);
    return expressions.some((expression) => label.includes(expression));
  });
  return {
    total: normalized.length,
    family: matching(['familia_']),
    citation: matching(['citacao', 'citar']),
    food_support: matching(['alimento']),
    hybrid: matching(['hibr']),
    in_person: matching(['_pres_', 'presencial']),
    electronic_mail: matching(['_ec']),
    court_officer: matching(['_oj']),
    system: matching(['_sist']),
    models: normalized,
    suffix_interpretation_verified: false,
  };
}

function validateCommunicationPlan(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'O plano deve ser um objeto.');
  }
  const analysis = payload.process_analysis || {};
  const communications = payload.communications;
  if (!Array.isArray(communications) || communications.length === 0) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Informe ao menos uma comunicação.');
  }
  const blockers = [];
  const warnings = [];
  const addBlocker = (code, message, index = null) => blockers.push({ code, message, index });
  const normalized = communications.map((raw, index) => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new PJeBridgeError('INVALID_REQUEST', `Comunicação ${index + 1} inválida.`);
    }
    const item = {
      recipient_role: fold(raw.recipient_role),
      type: fold(raw.type),
      medium: fold(raw.medium),
      central: cleanText(raw.central),
      deadline_days: Number(raw.deadline_days),
      model: cleanText(raw.model),
      batch_key: cleanText(raw.batch_key),
      interest_key: cleanText(raw.interest_key),
      documents: Array.isArray(raw.documents) ? raw.documents.map(cleanText).filter(Boolean) : [],
    };
    if (!item.recipient_role || !item.type || !item.medium ||
        !Number.isInteger(item.deadline_days) || item.deadline_days < 0) {
      addBlocker('INCOMPLETE_COMMUNICATION', 'Destinatário, tipo, meio e prazo válido são obrigatórios.', index);
    }
    if (item.recipient_role.includes('requerente') && item.type.includes('citacao')) {
      addBlocker('REQUESTER_CITATION_FORBIDDEN', 'A parte requerente deve ser intimada, não citada.', index);
    }
    if (item.type.includes('citacao')) {
      if (analysis.judicial_order_to_cite !== true) {
        addBlocker('CITATION_ORDER_NOT_EVIDENCED', 'A ordem judicial de citação precisa estar evidenciada.', index);
      }
      if (analysis.respondent_already_cited !== false) {
        addBlocker('CITATION_STATUS_NOT_CLEARED', 'É preciso comprovar que a parte ainda não foi citada.', index);
      }
    }
    const doubleDeadline = item.recipient_role === 'mp' ||
      item.recipient_role.includes('ministerio publico') ||
      item.recipient_role.includes('defensoria') ||
      item.recipient_role.includes('pratica juridica') ||
      (item.recipient_role.includes('universidad') &&
        (item.recipient_role.includes('escritorio') || item.recipient_role.includes('nucleo')));
    const expectedDeadline = doubleDeadline ? 30 : 15;
    if (item.deadline_days !== expectedDeadline) {
      addBlocker('DEADLINE_POLICY_MISMATCH', `O prazo operacional esperado é ${expectedDeadline} dias.`, index);
    }
    if (item.medium.includes('central de mandados') && !item.central) {
      addBlocker('CENTRAL_REQUIRED', 'Informe a Central de Mandados para comunicação por central.', index);
    }
    const model = fold(item.model);
    if ((model.includes('hibr') || model.includes('_pres_') || model.includes('presencial')) &&
        analysis.decision_reviewed !== true) {
      addBlocker('DECISION_REVIEW_REQUIRED', 'Revise a decisão antes de escolher modelo de audiência.', index);
    }
    if (model.includes('hibr') && fold(analysis.hearing_mode) !== 'hibrida') {
      addBlocker('HEARING_MODE_MISMATCH', 'Modelo híbrido sem confirmação de audiência híbrida.', index);
    }
    if ((model.includes('_pres_') || model.includes('presencial')) &&
        fold(analysis.hearing_mode) !== 'presencial') {
      addBlocker('HEARING_MODE_MISMATCH', 'Modelo presencial sem confirmação de audiência presencial.', index);
    }
    if ((fold(analysis.decision_kind).includes('alimento') || model.includes('alimento')) &&
        analysis.decision_reviewed !== true) {
      addBlocker('FOOD_SUPPORT_DECISION_REVIEW_REQUIRED', 'A decisão inicial de alimentos precisa ser analisada.', index);
    }
    return item;
  });

  if (analysis.minor_interest === true) {
    const mpIncluded = normalized.some((item) =>
      (item.recipient_role.includes('ministerio publico') || item.recipient_role === 'mp') &&
      item.type.includes('intimacao'));
    if (!mpIncluded) {
      addBlocker('MP_INTIMATION_REQUIRED', 'O plano sinalizado com interesse de menor deve incluir intimação do MP.');
    }
  }

  const batches = new Map();
  normalized.forEach((item, index) => {
    if (!item.batch_key) return;
    if (!batches.has(item.batch_key)) batches.set(item.batch_key, []);
    batches.get(item.batch_key).push({ ...item, index });
  });
  for (const [batchKey, items] of batches) {
    const signatures = new Set(items.map((item) => JSON.stringify({
      type: item.type,
      medium: item.medium,
      central: item.medium.includes('central de mandados') ? fold(item.central) : '',
      deadline_days: item.deadline_days,
      model: fold(item.model),
      documents: [...item.documents].sort(),
      interest_key: item.interest_key,
    })));
    if (signatures.size > 1) {
      addBlocker('UNSAFE_BATCH', `O lote ${batchKey} mistura central, ato, prazo, modelo, documentos ou interesse.`);
    }
  }

  if (analysis.legal_review_completed !== true) {
    warnings.push({
      code: 'LEGAL_REVIEW_PENDING',
      message: 'As regras são operacionais e o ato real ainda exige conferência jurídica humana.',
    });
  }
  return {
    status: blockers.length ? 'blocked' : 'ready_for_human_review',
    policy_source: POLICY_SOURCE,
    process_analysis: analysis,
    communications: normalized,
    blockers,
    warnings,
    final_action_available: false,
    final_action_executed: false,
  };
}

function createCommunicationOperations({ getPage }) {
  if (typeof getPage !== 'function') throw new TypeError('getPage é obrigatório.');

  async function inspect(payload = {}) {
    const source = await getPage();
    const page = [...source.context().pages()].reverse().find((candidate) => {
      try {
        const url = new URL(candidate.url());
        return url.hostname === 'pje.tjpa.jus.br' &&
          url.pathname.endsWith('/Processo/movimentar.seam');
      } catch (_) { return false; }
    });
    if (!page) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra a tarefa de comunicação primeiro.');
    const expected = cleanText(payload.process_number);
    const bodyText = cleanText(await page.locator('body').textContent().catch(() => ''));
    if (expected && !bodyText.includes(expected)) {
      throw new PJeBridgeError('PROCESS_MISMATCH', 'A tarefa aberta não corresponde ao processo solicitado.');
    }
    const raw = await page.evaluate(({ finalActions }) => {
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const foldLocal = (value) => clean(value).normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '').toLowerCase();
      const visible = (element) => Boolean(element && getComputedStyle(element).display !== 'none' &&
        getComputedStyle(element).visibility !== 'hidden' && element.getClientRects().length);
      const modelSelect = Array.from(document.querySelectorAll('select')).filter(visible).find((select) =>
        Array.from(select.options).some((option) => /familia_/i.test(option.textContent || '')));
      const controls = Array.from(document.querySelectorAll('input,button,a,select')).filter(visible)
        .map((element) => ({
          tag: element.tagName.toLowerCase(),
          type: element.type || null,
          id: element.id || null,
          label: clean(element.value || element.textContent || element.title),
          disabled: Boolean(element.disabled),
        })).filter((item) => item.label);
      const instruments = Array.from(document.querySelectorAll('input[type="radio"]')).filter(visible)
        .map((radio) => ({ value: radio.value, checked: radio.checked, id: radio.id || null }));
      const models = modelSelect ? Array.from(modelSelect.options).map((option) => ({
        value: option.value,
        label: clean(option.textContent),
      })) : [];
      return {
        title: document.title,
        body_excerpt: clean(document.body.innerText).slice(0, 1500),
        controls,
        instruments,
        models,
        dangerous_controls: controls.filter((item) =>
          finalActions.some((action) => foldLocal(item.label).includes(action))),
      };
    }, { finalActions: FINAL_ACTIONS });
    const url = new URL(page.url());
    const catalog = classifyModels(raw.models);
    return {
      status: 'mapped',
      process_number: expected || bodyText.match(/\d{7}-\d{2}\.\d{4}\.8\.14\.\d{4}/)?.[0] || null,
      task_id: url.searchParams.get('newTaskId'),
      route: url.pathname,
      title: raw.title,
      stage_excerpt: raw.body_excerpt,
      instruments: raw.instruments,
      catalog,
      visible_controls: raw.controls,
      final_controls_detected: raw.dangerous_controls,
      policy_source: POLICY_SOURCE,
      inspection_only: true,
      final_action_available: false,
      final_action_executed: false,
    };
  }

  return { inspect, analyze: validateCommunicationPlan };
}

module.exports = {
  FINAL_ACTIONS,
  POLICY_SOURCE,
  classifyModels,
  createCommunicationOperations,
  validateCommunicationPlan,
};
