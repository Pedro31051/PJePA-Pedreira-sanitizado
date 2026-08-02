'use strict';

const crypto = require('crypto');
const { PJeBridgeError } = require('./contracts');

const AUTOS_PATH = '/pje/Processo/ConsultaProcesso/Detalhe/listAutosDigitais.seam';
const PANEL_URL = 'https://pje.tjpa.jus.br/pje/ng2/dev.seam#/painel-usuario-interno';
const DEFAULT_TASK = 'Verificar providência a adotar';
const PREVIEW_TTL_MS = 5 * 60 * 1000;

function cleanText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function normalizeCnj(value) {
  const digits = String(value || '').replace(/\D/g, '');
  if (digits.length !== 20 || digits[13] !== '8' || digits.slice(14, 16) !== '14') {
    throw new PJeBridgeError('INVALID_REQUEST', 'Número CNJ inválido ou fora do TJPA.');
  }
  return `${digits.slice(0, 7)}-${digits.slice(7, 9)}.${digits.slice(9, 13)}.` +
    `${digits[13]}.${digits.slice(14, 16)}.${digits.slice(16)}`;
}

function normalizeLabelChange(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'alteracao deve ser um objeto.');
  }
  const unknown = Object.keys(raw).filter(key => !['acao', 'etiqueta', 'caixa_tarefa'].includes(key));
  if (unknown.length) throw new PJeBridgeError('INVALID_REQUEST', 'alteracao contém campos não permitidos.');
  const action = cleanText(raw.acao).toLowerCase();
  const label = cleanText(raw.etiqueta);
  const taskBox = cleanText(raw.caixa_tarefa || DEFAULT_TASK);
  if (!['adicionar', 'remover'].includes(action)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'A ação da etiqueta deve ser adicionar ou remover.');
  }
  if (!label || label.length > 240 || /[\u0000-\u001f]/.test(label)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Nome de etiqueta inválido.');
  }
  if (!taskBox || taskBox.length > 240) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Caixa de tarefa inválida.');
  }
  return { action, label, taskBox };
}

function stateHash(state) {
  const stable = JSON.stringify({
    process_number: state.process_number,
    labels: [...state.labels].map(item => item.name).sort(),
    situations: [...state.situations].map(item => item.name).sort(),
    document_id: state.reminders.current_document_id,
    reminders: state.reminders.items.map(item => item.text).sort(),
  });
  return crypto.createHash('sha256').update(stable).digest('hex');
}

function createProcessMetadataOperations({ getPage }) {
  if (typeof getPage !== 'function') throw new TypeError('getPage é obrigatório.');
  const previews = new Map();

  function findAutosPage(context) {
    return [...context.pages()].reverse().find(page => {
      try {
        const url = new URL(page.url());
        return url.hostname === 'pje.tjpa.jus.br' && url.pathname === AUTOS_PATH;
      } catch (_) { return false; }
    }) || null;
  }

  async function readState(payload = {}) {
    const sourcePage = await getPage();
    const autos = findAutosPage(sourcePage.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const expected = payload.process_number ? normalizeCnj(payload.process_number) : null;
    const bodyText = cleanText(await autos.locator('body').textContent().catch(() => ''));
    const detected = bodyText.match(/\d{7}-\d{2}\.\d{4}\.8\.14\.\d{4}/)?.[0] || null;
    if (expected && !bodyText.includes(expected)) {
      throw new PJeBridgeError('PROCESS_MISMATCH', 'Os autos abertos não correspondem ao processo solicitado.');
    }
    const raw = await autos.evaluate(() => {
      const tidy = value => String(value || '').replace(/\s+/g, ' ').trim();
      const menuParent = title => document.querySelector(`a[title="${title}"]`)?.parentElement;
      const labelParent = menuParent('Etiquetas do processo');
      const situationParent = menuParent('Situações do processo');
      const reminderParent = menuParent('Lembretes');
      const labels = Array.from(labelParent?.querySelectorAll('li[id^="etiqueta"]') || []).map(item => ({
        id: item.id.replace(/^etiqueta/, '') || null,
        name: tidy(item.querySelector('i[title]')?.getAttribute('title') || item.textContent),
      })).filter(item => item.name);
      const situations = Array.from(situationParent?.querySelectorAll('.menu-conteudo li:not(.divider)') || []).map(item => ({
        id: item.id || null,
        name: tidy(item.querySelector('[title]')?.getAttribute('title') || item.textContent),
      })).filter(item => item.name);
      const rows = Array.from(reminderParent?.querySelectorAll('table[id$="gridLembretes"] tbody tr') || []).map(row => ({
        text: tidy(row.textContent),
        cells: Array.from(row.querySelectorAll('td')).map(cell => tidy(cell.textContent)),
      })).filter(item => item.text);
      const create = reminderParent?.querySelector('a[onclick*="lembretes.seam"]');
      const onclick = create?.getAttribute('onclick') || '';
      return {
        labels,
        situations,
        reminders: {
          current_document_id: onclick.match(/idProcessoDocumento=(\d+)/)?.[1] || null,
          create_url: onclick.match(/['"]([^'"]*lembretes\.seam[^'"]*)['"]/)?.[1] || null,
          items: rows,
        },
      };
    });
    const state = {
      status: 'inspected',
      process_number: expected || detected,
      labels: raw.labels,
      situations: raw.situations,
      reminders: {
        ...raw.reminders,
        terminology: 'No PJe, os alertas deste contexto são chamados de lembretes.',
      },
      capabilities: {
        labels: { read: true, add_existing: true, remove: true, create_catalog_entry: false },
        situations: { read: true, direct_write: false, reason: 'Estado derivado do fluxo processual.' },
        reminders: { read_current_document: true, mapped_form: true, write: false },
      },
    };
    state.state_fingerprint = stateHash(state);
    return state;
  }

  async function openTaskCard(context, processNumber, taskBox, timeoutMs) {
    let page = [...context.pages()].find(candidate => {
      try {
        const url = new URL(candidate.url());
        return url.hostname === 'pje.tjpa.jus.br' && url.pathname !== AUTOS_PATH;
      } catch (_) { return false; }
    });
    if (!page) page = await context.newPage();
    await page.goto(PANEL_URL, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
    await page.locator('#ngFrame').waitFor({ state: 'attached', timeout: timeoutMs });
    const frame = page.frameLocator('#ngFrame');
    const task = frame.locator('a[href*="lista-processos-tarefa"]').filter({ hasText: taskBox }).first();
    await task.waitFor({ state: 'attached', timeout: timeoutMs });
    await task.evaluate(el => el.click());
    const order = frame.locator('select#inputOrdem');
    await order.waitFor({ state: 'attached', timeout: timeoutMs });
    await order.evaluate(el => {
      el.value = 'ASC';
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await frame.locator('.btnPesquisar').first().evaluate(el => el.click());
    const link = frame.locator('a.selecionarProcesso').filter({ hasText: processNumber }).first();
    await link.waitFor({ state: 'attached', timeout: timeoutMs });
    return { page, frame, card: link.locator('xpath=ancestor::div[contains(@class,"datalist-content")][1]') };
  }

  async function resolveCatalogLabel(frame, card, requested, timeoutMs) {
    const select = card.locator('button.botao-selecionar').first();
    if (!await select.locator('i.fa-check-square').count()) await select.evaluate(el => el.click());
    const labelButton = frame.locator('button[title="Vincular etiqueta"]:visible').first();
    await labelButton.waitFor({ state: 'visible', timeout: timeoutMs });
    await labelButton.evaluate(el => el.click());
    const modal = frame.locator('#modalEtiquetarLote .modal-dialog');
    await modal.waitFor({ state: 'visible', timeout: timeoutMs });
    const input = modal.locator('#itPesquisarEtiquetas');
    await input.fill(requested);
    const rows = modal.locator('table.table-etiquetas tbody tr');
    await rows.first().waitFor({ state: 'attached', timeout: timeoutMs }).catch(() => {});
    const matches = await rows.evaluateAll((elements, wanted) => elements.map((row, index) => ({
      index,
      name: String(row.querySelector('td:last-child')?.textContent || '').replace(/\s+/g, ' ').trim(),
    })).filter(item => item.name.localeCompare(wanted, 'pt-BR', { sensitivity: 'accent' }) === 0), requested);
    if (matches.length !== 1) {
      await modal.locator('button[data-dismiss="modal"]').last().evaluate(el => el.click());
      if (await select.locator('i.fa-check-square').count()) await select.evaluate(el => el.click());
      throw new PJeBridgeError('LABEL_NOT_FOUND', 'A etiqueta não existe no catálogo ou é ambígua.');
    }
    return { modal, rows, match: matches[0], select };
  }

  async function preview(payload = {}, timeoutMs = 30_000) {
    const processNumber = normalizeCnj(payload.process_number);
    const change = normalizeLabelChange(payload.change);
    const state = await readState({ process_number: processNumber });
    const existing = state.labels.filter(item => item.name === change.label);
    if (change.action === 'remover') {
      if (existing.length !== 1) throw new PJeBridgeError('LABEL_NOT_FOUND', 'A etiqueta não está vinculada ao processo.');
    } else {
      if (existing.length) throw new PJeBridgeError('NO_CHANGE', 'A etiqueta já está vinculada ao processo.');
      const sourcePage = await getPage();
      const { frame, card } = await openTaskCard(sourcePage.context(), processNumber, change.taskBox, timeoutMs);
      const resolved = await resolveCatalogLabel(frame, card, change.label, timeoutMs);
      change.label = resolved.match.name;
      await resolved.modal.locator('button[data-dismiss="modal"]').last().evaluate(el => el.click());
      if (await resolved.select.locator('i.fa-check-square').count()) await resolved.select.evaluate(el => el.click());
    }
    const token = crypto.randomBytes(24).toString('base64url');
    const expiresAt = Date.now() + PREVIEW_TTL_MS;
    previews.set(token, { processNumber, change, fingerprint: state.state_fingerprint, expiresAt });
    return {
      status: 'preview_ready',
      process_number: processNumber,
      change: { action: change.action, label: change.label, task_box: change.taskBox },
      current_labels: state.labels,
      confirmation_token: token,
      expires_at: new Date(expiresAt).toISOString(),
      applied: false,
    };
  }

  async function apply(payload = {}, timeoutMs = 30_000) {
    const processNumber = normalizeCnj(payload.process_number);
    const change = normalizeLabelChange(payload.change);
    const token = cleanText(payload.confirmation_token);
    const previewData = previews.get(token);
    if (!previewData) throw new PJeBridgeError('CONFIRMATION_REQUIRED', 'Token de confirmação ausente ou desconhecido.');
    previews.delete(token);
    if (Date.now() > previewData.expiresAt) throw new PJeBridgeError('CONFIRMATION_EXPIRED', 'A confirmação expirou.');
    if (previewData.processNumber !== processNumber || previewData.change.action !== change.action ||
        previewData.change.label !== change.label || previewData.change.taskBox !== change.taskBox) {
      throw new PJeBridgeError('CONFIRMATION_MISMATCH', 'A confirmação não corresponde à alteração solicitada.');
    }
    const before = await readState({ process_number: processNumber });
    if (before.state_fingerprint !== previewData.fingerprint) {
      throw new PJeBridgeError('STATE_CHANGED', 'Etiquetas, situações ou lembretes mudaram após a prévia.');
    }
    const sourcePage = await getPage();
    const { page: panelPage, frame, card } = await openTaskCard(sourcePage.context(), processNumber, change.taskBox, timeoutMs);
    if (change.action === 'remover') {
      const removeTitle = `Excluir etiqueta ${change.label}`;
      const remove = card.locator(`[title=${JSON.stringify(removeTitle)}]`).first();
      await remove.waitFor({ state: 'attached', timeout: timeoutMs });
      const response = panelPage.waitForResponse(candidate => candidate.request().method() !== 'GET' && candidate.url().includes('/pje/'), { timeout: timeoutMs }).catch(() => null);
      await remove.evaluate(el => el.click());
      await response;
    } else {
      const resolved = await resolveCatalogLabel(frame, card, change.label, timeoutMs);
      const row = resolved.rows.nth(resolved.match.index);
      await row.locator('button.check-etiqueta').evaluate(el => el.click());
      const response = panelPage.waitForResponse(candidate => candidate.request().method() !== 'GET' && candidate.url().includes('/pje/'), { timeout: timeoutMs }).catch(() => null);
      await resolved.modal.getByRole('button', { name: 'Vincular etiqueta', exact: true }).evaluate(el => el.click());
      await response;
      if (await resolved.select.locator('i.fa-check-square').count()) await resolved.select.evaluate(el => el.click());
    }
    return {
      status: 'applied', process_number: processNumber,
      change: { action: change.action, label: change.label, task_box: change.taskBox }, applied: true,
    };
  }

  return { inspect: readState, preview, apply };
}

module.exports = {
  AUTOS_PATH,
  DEFAULT_TASK,
  PREVIEW_TTL_MS,
  createProcessMetadataOperations,
  normalizeLabelChange,
  stateHash,
};
