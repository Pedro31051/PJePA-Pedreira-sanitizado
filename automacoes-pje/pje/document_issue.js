'use strict';

const { PJeBridgeError } = require('./contracts');

function cleanText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function fold(value) {
  return cleanText(value).normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
}

function classifyIssueCatalog(documentTypes = [], models = []) {
  const normalize = (item) => item && typeof item === 'object'
    ? { value: cleanText(item.value), label: cleanText(item.label) }
    : { value: '', label: cleanText(item) };
  const types = documentTypes.map(normalize)
    .filter((item) => item.label && fold(item.label) !== 'selecione');
  const normalizedModels = models.map(normalize)
    .filter((item) => item.label && fold(item.label) !== 'selecione o modelo');
  return {
    document_types: types,
    document_types_total: types.length,
    models: normalizedModels,
    models_total: normalizedModels.length,
    family_models: normalizedModels.filter((item) => /^fam\s*-/i.test(item.label)),
    food_support_models: normalizedModels.filter((item) => /alimento/i.test(item.label)),
  };
}

function validateDocumentIssuePlan(payload = {}) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'O plano de expedição deve ser um objeto.');
  }
  const blockers = [];
  const warnings = [];
  const origin = fold(payload.document_origin || 'internal');
  const documentType = cleanText(payload.document_type);
  const draftText = cleanText(payload.draft_text);
  const movementCode = Number(payload.movement_code ?? 60);
  const movementComplement = cleanText(payload.movement_complement);
  if (!['internal', 'interno', 'nativo'].includes(origin)) {
    blockers.push({
      code: 'WRONG_WORKFLOW_EXTERNAL_DOCUMENT',
      message: 'Documento produzido fora do PJe deve seguir Juntar documentos, não Expedir documento.',
    });
  }
  if (!documentType) blockers.push({ code: 'DOCUMENT_TYPE_REQUIRED', message: 'Informe o tipo documental.' });
  if (!draftText) blockers.push({ code: 'DRAFT_TEXT_REQUIRED', message: 'A minuta interna não pode estar vazia.' });
  if (movementCode !== 60) {
    blockers.push({ code: 'MOVEMENT_MISMATCH', message: 'O fluxo mapeado exige o movimento 60, Expedição de documento.' });
  }
  if (!movementComplement) {
    blockers.push({ code: 'MOVEMENT_COMPLEMENT_REQUIRED', message: 'Preencha o complemento tipo de documento do movimento 60.' });
  } else if (documentType && fold(movementComplement) !== fold(documentType)) {
    blockers.push({ code: 'MOVEMENT_COMPLEMENT_MISMATCH', message: 'Tipo da minuta e complemento do movimento devem coincidir.' });
  }
  if (payload.prepare_communication === true) {
    warnings.push({
      code: 'COMMUNICATION_IS_SEPARATE_STEP',
      message: 'Preparar ato de comunicação é etapa autônoma; expedir o documento não comunica automaticamente um destinatário.',
    });
  }
  return {
    status: blockers.length ? 'blocked' : 'ready_for_draft_review',
    workflow: 'expedir_documento_interno',
    document_origin: origin,
    document_type: documentType,
    movement: { code: movementCode, complement: movementComplement },
    blockers,
    warnings,
    signing_available: false,
    final_action_available: false,
    final_action_executed: false,
  };
}

function createDocumentIssueOperations({ getPage }) {
  if (typeof getPage !== 'function') throw new TypeError('getPage é obrigatório.');

  async function inspect(payload = {}) {
    const source = await getPage();
    const page = [...source.context().pages()].reverse().find((candidate) => {
      try {
        const url = new URL(candidate.url());
        return url.hostname === 'pje.tjpa.jus.br' && url.pathname.endsWith('/Processo/movimentar.seam');
      } catch (_) { return false; }
    });
    if (!page) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra a tarefa Expedir documento primeiro.');
    const expected = cleanText(payload.process_number);
    const body = cleanText(await page.locator('body').textContent().catch(() => ''));
    if (expected && !body.includes(expected)) {
      throw new PJeBridgeError('PROCESS_MISMATCH', 'A tarefa aberta não corresponde ao processo solicitado.');
    }
    const raw = await page.evaluate(() => {
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = (element) => Boolean(element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden');
      const selects = Array.from(document.querySelectorAll('select')).filter(visible);
      const typeSelect = selects.find((select) => /selectMenuTipoDocumento$/.test(select.id));
      const modelSelect = selects.find((select) => /selectModeloDocumento$/.test(select.id));
      const options = (select) => select ? Array.from(select.options).map((option) => ({ value: option.value, label: clean(option.textContent) })) : [];
      const editorFrame = document.querySelector('iframe[id$="EditorTextArea_ifr"]');
      const controls = Array.from(document.querySelectorAll('input,button,a')).filter(visible).map((element) => ({
        tag: element.tagName.toLowerCase(), id: element.id || null,
        label: clean(element.value || element.textContent || element.title),
        disabled: Boolean(element.disabled),
      })).filter((item) => item.label);
      return {
        body: clean(document.body.innerText),
        types: options(typeSelect),
        selected_type: clean(typeSelect?.selectedOptions[0]?.textContent),
        models: options(modelSelect),
        selected_model: clean(modelSelect?.selectedOptions[0]?.textContent),
        editor_present: Boolean(editorFrame),
        movement_60_present: /Expedição de (?:documento|[^.]+)\.?\s*\(60\)/i.test(document.body.innerText),
        movement_complement_control: controls.some((item) => item.label === 'Preencher complementos'),
        communication_section_present: /Preparar Ato de Comunicação/i.test(document.body.innerText),
        controls,
      };
    });
    const url = new URL(page.url());
    const stage = /Confirmar minuta de documento/i.test(raw.body)
      ? 'confirmar_minuta'
      : /Minutar documento/i.test(raw.body) ? 'minutar_documento' : 'unknown';
    const catalog = classifyIssueCatalog(raw.types, raw.models);
    return {
      status: 'mapped',
      workflow: 'expedir_documento_interno',
      process_number: expected || raw.body.match(/\d{7}-\d{2}\.\d{4}\.8\.14\.\d{4}/)?.[0] || null,
      task_id: url.searchParams.get('newTaskId'),
      stage,
      selected_document_type: raw.selected_type || null,
      selected_model: raw.selected_model || null,
      catalog,
      editor_present: raw.editor_present,
      movement_60_present: raw.movement_60_present,
      movement_complement_control: raw.movement_complement_control,
      communication_section_separate: raw.communication_section_present,
      controls: raw.controls,
      discard_route: 'Retornar para minutar documento e Devolver para secretaria antes da assinatura.',
      signing_available: false,
      final_action_available: false,
      final_action_executed: false,
    };
  }

  return { inspect, analyze: validateDocumentIssuePlan };
}

module.exports = {
  classifyIssueCatalog,
  createDocumentIssueOperations,
  validateDocumentIssuePlan,
};
