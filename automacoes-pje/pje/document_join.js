'use strict';

const { PJeBridgeError } = require('./contracts');

const SELECTORS = Object.freeze({
  joinTab: 'a[title="Juntar documentos"]',
  documentType: 'select[id$="cbTD"]',
  description: 'input[id$="ipDesc"]',
  number: 'input[id$="ipNro"]',
  model: 'select[id$="modTD"]',
  confidential: 'input[id$="sigDPCB"]',
  movement: '#descEv',
  pdfMode: 'input[type="radio"][id^="raTipoDocPrincipal:"]',
  upload: 'input[type="file"][id$="uploadDocumentoPrincipal:file"]',
});

function normalizeLabel(value) {
  return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
}

function classifyCatalog(documentTypes = [], models = []) {
  const cleanTypes = documentTypes.map(value => String(value || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean).filter(value => normalizeLabel(value) !== 'selecione');
  const cleanModels = models.map(value => String(value || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean).filter(value => normalizeLabel(value) !== 'selecione um modelo');
  return {
    document_types: cleanTypes,
    certificate_types: cleanTypes.filter(value => /\bcertidao\b/.test(normalizeLabel(value))),
    family_models: cleanModels.filter(value => /^fam\s*-/i.test(normalizeLabel(value))),
    models: cleanModels,
  };
}

function createDocumentJoinOperations({ getPage }) {
  if (typeof getPage !== 'function') throw new TypeError('getPage é obrigatório.');

  function findAutosPage(context) {
    return [...context.pages()].reverse().find((page) => {
      try {
        const url = new URL(page.url());
        return url.hostname === 'pje.tjpa.jus.br' &&
          url.pathname.endsWith('/Processo/ConsultaProcesso/Detalhe/listAutosDigitais.seam');
      } catch (_) {
        return false;
      }
    }) || null;
  }

  async function waitForPost(page, timeoutMs) {
    return page.waitForResponse(response =>
      response.request().method() === 'POST' && response.url().includes('/pje/'),
    { timeout: Math.min(timeoutMs, 10_000) }).catch(() => null);
  }

  async function chooseType(page, field, requested, timeoutMs) {
    const options = await field.locator('option').evaluateAll(elements => elements.map(option => ({
      label: String(option.textContent || '').replace(/\s+/g, ' ').trim(),
      value: option.value,
      disabled: option.disabled,
    })));
    const wanted = normalizeLabel(requested);
    const matches = options.filter(option => !option.disabled && normalizeLabel(option.label) === wanted);
    if (matches.length !== 1) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Tipo de documento inexistente ou ambíguo.');
    }
    const target = matches[0];
    const current = await field.inputValue();
    const dependentReady = await page.locator(`${SELECTORS.movement}, ${SELECTORS.upload}, ${SELECTORS.pdfMode}`).count() > 0;
    if (current === target.value && dependentReady) return target.label;

    if (current === target.value) {
      const placeholder = options.find(option => normalizeLabel(option.label) === 'selecione');
      if (placeholder) {
        const resetResponse = waitForPost(page, timeoutMs);
        await field.selectOption(placeholder.value);
        await resetResponse;
      }
    }
    const response = waitForPost(page, timeoutMs);
    await field.selectOption(target.value);
    await response;
    await page.waitForFunction(({ selector, value }) =>
      document.querySelector(selector)?.value === value,
    { selector: SELECTORS.documentType, value: target.value }, { timeout: timeoutMs });
    return target.label;
  }

  async function inspect(payload = {}, timeoutMs = 15_000) {
    const page = await getPage();
    const autos = findAutosPage(page.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const expected = String(payload.process_number || '').trim();
    if (expected) {
      const text = await autos.locator('body').textContent().catch(() => '');
      if (!String(text || '').includes(expected)) {
        throw new PJeBridgeError('PROCESS_MISMATCH', 'Os autos abertos não correspondem ao processo solicitado.');
      }
    }

    const tab = autos.locator(SELECTORS.joinTab).first();
    await tab.waitFor({ state: 'visible', timeout: timeoutMs });
    let typeField = autos.locator(SELECTORS.documentType).first();
    if (!await typeField.isVisible().catch(() => false)) {
      await tab.click();
      await typeField.waitFor({ state: 'visible', timeout: timeoutMs });
    }

    const typeLabels = await typeField.locator('option').evaluateAll(elements =>
      elements.map(option => String(option.textContent || '').replace(/\s+/g, ' ').trim()));
    const requested = String(payload.document_type || '').trim();
    let selectedType = '';
    if (requested) selectedType = await chooseType(autos, typeField, requested, timeoutMs);

    typeField = autos.locator(SELECTORS.documentType).first();
    const models = await autos.locator(SELECTORS.model).first().locator('option')
      .evaluateAll(elements => elements.map(option =>
        String(option.textContent || '').replace(/\s+/g, ' ').trim()))
      .catch(() => []);
    const catalog = classifyCatalog(typeLabels, models);
    const modes = await autos.locator(SELECTORS.pdfMode).evaluateAll(elements => elements.map(element => {
      const explicit = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
      return {
        id: element.id,
        label: String(explicit?.textContent || '').replace(/\s+/g, ' ').trim(),
        checked: element.checked,
      };
    }));
    const upload = autos.locator(SELECTORS.upload).first();
    let movement = autos.locator(SELECTORS.movement).first();
    const editorMode = modes.find(mode => normalizeLabel(mode.label) === 'editor de texto');
    const previousMode = modes.find(mode => mode.checked);
    let editorMapping = {
      available: Boolean(editorMode),
      save_control_visible: false,
      editor_surfaces: [],
    };
    if (payload.inspect_native_editor !== false && editorMode) {
      const editorRadio = autos.locator(`[id="${editorMode.id}"]`).first();
      await editorRadio.check();
      const save = autos.getByRole('button', { name: 'Salvar', exact: true }).first();
      await save.waitFor({ state: 'visible', timeout: timeoutMs });
      editorMapping = {
        available: true,
        save_control_visible: true,
        editor_surfaces: await autos.locator(
          '#dvEditorHTML, #editorAnexar, #docPrincipalEditor, #docPrincipalEditorTextArea, #docPrincipalEditorTextArea_ifr',
        ).evaluateAll(elements => elements.map(element => ({
          tag: element.tagName.toLowerCase(),
          id: element.id || null,
          title: element.getAttribute('title'),
          visible: Boolean(element.getClientRects().length),
        }))),
        toolbar_controls: await autos.locator(
          '[id^="docPrincipalEditorTextArea_"][title]',
        ).evaluateAll(elements => elements.map(element => ({
          id: element.id,
          title: element.getAttribute('title'),
        })).filter(item => item.title)),
      };
      movement = autos.locator(SELECTORS.movement).first();
      if (previousMode && previousMode.id !== editorMode.id) {
        await autos.locator(`[id="${previousMode.id}"]`).first().check();
        if (normalizeLabel(previousMode.label) === 'arquivo pdf') {
          await upload.waitFor({ state: 'attached', timeout: timeoutMs });
        }
      }
    }
    await movement.waitFor({ state: 'attached', timeout: 3_000 }).catch(() => {});
    return {
      status: 'mapped',
      process_number: expected || null,
      task_box: payload.task_box || null,
      selected_document_type: selectedType || null,
      document_types_total: catalog.document_types.length,
      certificate_types: catalog.certificate_types,
      models_total: catalog.models.length,
      family_models_total: catalog.family_models.length,
      family_models: catalog.family_models,
      models: catalog.models,
      modes,
      external_document: {
        available: await upload.count() > 0,
        input_id: await upload.getAttribute('id').catch(() => null),
        accept: await upload.getAttribute('accept').catch(() => null),
        multiple: await upload.getAttribute('multiple').then(value => value !== null).catch(() => false),
      },
      native_editor: editorMapping,
      movement: {
        available: await movement.count() > 0,
        input_id: await movement.getAttribute('id').catch(() => null),
        title: await movement.getAttribute('title').catch(() => null),
      },
      final_action_executed: false,
    };
  }

  return { inspect };
}

module.exports = {
  SELECTORS,
  classifyCatalog,
  createDocumentJoinOperations,
  normalizeLabel,
};
