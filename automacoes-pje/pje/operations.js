'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { connect, liteMode } = require('../browser_config');
const { PJeBridgeError } = require('./contracts');
const selectors = require('./selectors');
const { createDocumentJoinOperations } = require('./document_join');
const { createCommunicationOperations } = require('./communications');
const { createDocumentIssueOperations } = require('./document_issue');
const { createProcessMetadataOperations } = require('./process_metadata');
const { createRetificationOperations } = require('./retification');
const {
  assertAuthenticatedSession,
  findAuthenticatedPJePage,
  findOfficialPJePage,
} = require('./session');

const CNJ_TJPA_RE = /\d{7}-\d{2}\.\d{4}\.8\.14\.\d{4}/;
const SEARCH_CRITERIA = new Set([
  'numero_cnj',
  'cpf',
  'cnpj',
  'nome_parte',
  'nome_requerente',
  'nome_requerido',
  'nome_advogado',
  'outros_nomes',
  'numero_documento',
  'oab',
  'assunto',
  'classe_judicial',
  'jurisdicao',
  'orgao_julgador',
  'prioridade_processual',
  'data_autuacao',
  'valor_causa',
  'movimento_processual',
  'orgao_origem_criminal',
  'procedimento_criminal',
  'ano_procedimento_criminal',
  'protocolo_policia',
]);
const SEARCH_CRITERION_ALIASES = Object.freeze({
  autor: 'nome_requerente',
  requerente: 'nome_requerente',
  reu: 'nome_requerido',
  requerido: 'nome_requerido',
  representante: 'nome_advogado',
  advogado: 'nome_advogado',
  alcunha: 'outros_nomes',
  documento: 'numero_documento',
  classe: 'classe_judicial',
  'classe judicial': 'classe_judicial',
  prioridade: 'prioridade_processual',
  movimento: 'movimento_processual',
  'movimentacao processual': 'movimento_processual',
  movimentacao_processual: 'movimento_processual',
  'orgao julgador': 'orgao_julgador',
  'prioridade processual': 'prioridade_processual',
  'data autuacao': 'data_autuacao',
  'valor causa': 'valor_causa',
  numero_procedimento_criminal: 'procedimento_criminal',
  protocolo_policial: 'protocolo_policia',
});
const ALLOWED_EVENTS = new Set([
  'pje-ai-draft-response',
  'pje-ai-analysis-response',
  'pje-ai-deadline-response',
  'pje-ai-status-response',
]);
const DOCUMENT_FRAME = 'iframe[src*="/seam/resource/rest/pje-legacy/documento/download/"]';
const DOCUMENT_TITLE = 'a[title="Abrir documento em outra página"]';
const DOCUMENT_NAVIGATION = Object.freeze({
  first: 'detalheDocumento:primeiroDocumento',
  previous: 'detalheDocumento:documentoAnterior',
  next: 'detalheDocumento:proximoDocumento',
  last: 'detalheDocumento:ultimoDocumento',
});

function normalizeCnj(value) {
  const digits = String(value || '').replace(/\D/g, '');
  if (digits.length !== 20 || digits[13] !== '8' || digits.slice(14, 16) !== '14') {
    throw new PJeBridgeError('INVALID_REQUEST', 'Número CNJ inválido ou fora do TJPA.');
  }
  return `${digits.slice(0, 7)}-${digits.slice(7, 9)}.${digits.slice(9, 13)}.` +
    `${digits[13]}.${digits.slice(14, 16)}.${digits.slice(16)}`;
}

function normalizeSearchPayload(payload = {}) {
  const requestedCriterion = String(
    payload.criterion || (payload.process_number ? 'numero_cnj' : ''),
  ).normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().toLowerCase();
  const criterion = SEARCH_CRITERION_ALIASES[requestedCriterion] || requestedCriterion;
  if (!SEARCH_CRITERIA.has(criterion)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Critério de busca inválido.');
  }
  const raw = String(payload.value ?? payload.process_number ?? '')
    .replace(/\s+/g, ' ').trim();
  if (!raw || raw.length > 200 || /[\u0000-\u001f]/.test(raw)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Valor de busca inválido.');
  }
  const limit = Number(payload.limit ?? 20);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Limite deve estar entre 1 e 100.');
  }
  if (criterion === 'numero_cnj') {
    return { criterion, value: normalizeCnj(raw), limit };
  }
  if (criterion === 'cpf' || criterion === 'cnpj') {
    const value = raw.replace(/\D/g, '');
    const expected = criterion === 'cpf' ? 11 : 14;
    if (value.length !== expected || new Set(value).size === 1) {
      throw new PJeBridgeError('INVALID_REQUEST', `${criterion.toUpperCase()} inválido.`);
    }
    return { criterion, value, limit };
  }
  if (criterion === 'oab') {
    const upper = raw.toUpperCase();
    const ufFromValue = upper.match(/(?:\/|\s)([A-Z]{2})\s*$/)?.[1] || '';
    const uf = String(payload.uf_oab || ufFromValue).trim().toUpperCase();
    const withoutUf = ufFromValue
      ? upper.replace(new RegExp(`(?:\\/|\\s)${ufFromValue}\\s*$`), '')
      : upper;
    const compact = withoutUf.replace(/[^0-9A-Z]/g, '');
    const match = compact.match(/^(\d{1,10})([A-Z]?)$/);
    const value = match?.[1] || '';
    const letter = String(payload.letra_oab || match?.[2] || '').trim().toUpperCase();
    if (!value || !/^[A-Z]{2}$/.test(uf) || (letter && !/^[A-Z]$/.test(letter))) {
      throw new PJeBridgeError('INVALID_REQUEST', 'OAB exige número e UF.');
    }
    return { criterion, value, letra_oab: letter, uf_oab: uf, limit };
  }
  if (criterion === 'data_autuacao') {
    const pieces = raw.split(/\s*(?:\.\.| a )\s*/i);
    const start = pieces[0];
    const end = String(payload.value_end ?? pieces[1] ?? start).trim();
    const parseDate = (value) => {
      const match = value.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
      if (!match) return null;
      const date = new Date(Date.UTC(Number(match[3]), Number(match[2]) - 1, Number(match[1])));
      return date.getUTCFullYear() === Number(match[3]) &&
        date.getUTCMonth() === Number(match[2]) - 1 &&
        date.getUTCDate() === Number(match[1]) ? date : null;
    };
    const startDate = parseDate(start);
    const endDate = parseDate(end);
    if (!startDate || !endDate || startDate > endDate) {
      throw new PJeBridgeError(
        'INVALID_REQUEST',
        'Data de autuação exige DD/MM/AAAA ou intervalo início..fim em ordem crescente.',
      );
    }
    return { criterion, value: start, value_end: end, limit };
  }
  if (criterion === 'valor_causa') {
    const pieces = raw.split(/\s*(?:\.\.| a )\s*/i);
    const start = pieces[0];
    const end = String(payload.value_end ?? pieces[1] ?? start).trim();
    const parseMoney = (value) => {
      const normalized = value.replace(/^R\$\s*/i, '').replace(/\s/g, '');
      if (!/^\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?$|^\d+(?:[.,]\d{1,2})?$/.test(normalized)) {
        return null;
      }
      const decimal = normalized.includes(',')
        ? normalized.replace(/\./g, '').replace(',', '.')
        : normalized;
      const number = Number(decimal);
      return Number.isFinite(number) && number >= 0 ? number : null;
    };
    const startNumber = parseMoney(start);
    const endNumber = parseMoney(end);
    if (startNumber === null || endNumber === null || startNumber > endNumber) {
      throw new PJeBridgeError(
        'INVALID_REQUEST',
        'Valor da causa exige número ou intervalo início..fim em ordem crescente.',
      );
    }
    return { criterion, value: start, value_end: end, limit };
  }
  if (criterion === 'procedimento_criminal') {
    const match = raw.match(/^(\d{1,30})(?:\s*[-/]\s*(\d{4}))?$/);
    const year = String(payload.value_end ?? match?.[2] ?? '').trim();
    if (!match || (year && !/^\d{4}$/.test(year))) {
      throw new PJeBridgeError(
        'INVALID_REQUEST',
        'Procedimento criminal exige número e ano opcional (ex.: 12345/2024).',
      );
    }
    return { criterion, value: match[1], value_end: year, limit };
  }
  if (criterion === 'ano_procedimento_criminal' && !/^\d{4}$/.test(raw)) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Ano do procedimento deve ter quatro dígitos.');
  }
  if ([
    'jurisdicao',
    'orgao_julgador',
    'prioridade_processual',
    'orgao_origem_criminal',
  ].includes(criterion)) {
    return { criterion, value: raw, limit };
  }
  if (raw.length < 3) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Nome deve ter ao menos 3 caracteres.');
  }
  return { criterion, value: raw, limit };
}

function maskSearchValue(search) {
  const digits = String(search.value || '').replace(/\D/g, '');
  if (search.criterion === 'cpf') return `***.***.***-${digits.slice(-2)}`;
  if (search.criterion === 'cnpj') return `**.***.***/****-${digits.slice(-2)}`;
  if (search.criterion === 'oab') {
    return `***${digits.slice(-2)}${search.letra_oab || ''}/${search.uf_oab}`;
  }
  if (search.criterion === 'numero_cnj') return search.value;
  return String(search.value).split(/\s+/).slice(0, 8)
    .map(word => `${word[0].toUpperCase()}***`).join(' ');
}

function mapSearchRows(rawRows, limit = 20) {
  const aliases = new Map([
    ['caracteristicas', 'caracteristicas'],
    ['orgao julgador', 'orgao_julgador'],
    ['autuado em', 'autuado_em'],
    ['classe judicial', 'classe_judicial'],
    ['polo ativo', 'polo_ativo'],
    ['polo passivo', 'polo_passivo'],
    ['no(s) atual(is)', 'nos_atuais'],
    ['nos atuais', 'nos_atuais'],
    ['ultima moviment.', 'ultima_movimentacao'],
    ['ultima movimentacao', 'ultima_movimentacao'],
  ]);
  const fallback = [
    'processo',
    'caracteristicas',
    'orgao_julgador',
    'autuado_em',
    'classe_judicial',
    'polo_ativo',
    'polo_passivo',
    'nos_atuais',
    'ultima_movimentacao',
  ];
  const normalize = value => String(value || '').normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ')
    .trim().toLowerCase();
  const seen = new Set();
  const results = [];
  for (const raw of rawRows || []) {
    const columns = (raw.columns || []).map(value =>
      String(value || '').replace(/\s+/g, ' ').trim());
    const match = columns.join(' ').match(
      /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/,
    );
    if (!match || seen.has(match[0])) continue;
    seen.add(match[0]);
    const headers = (raw.headers || []).map(normalize);
    const item = { numero_cnj: match[0] };
    columns.forEach((value, index) => {
      const key = aliases.get(headers[index]) || fallback[index];
      if (key && key !== 'processo') item[key] = value;
    });
    if (item.classe_judicial) item.classe = item.classe_judicial;
    if (String(raw.href_path || '').startsWith('/')) {
      item.rota_resultado = String(raw.href_path).slice(0, 500);
    }
    results.push(item);
    if (results.length >= limit) break;
  }
  return results;
}

function normalizeComparable(value) {
  return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
}

function filterResultsByPartyRole(results, search) {
  const roleField = search.criterion === 'nome_requerente'
    ? 'polo_ativo'
    : search.criterion === 'nome_requerido'
      ? 'polo_passivo'
      : null;
  if (!roleField) return { results, roleField: null };
  const needle = normalizeComparable(search.value);
  return {
    results: results.filter(item => normalizeComparable(item[roleField]).includes(needle)),
    roleField,
  };
}

function createPJeOperations({
  connectFn = connect,
  liteModeFn = liteMode,
} = {}) {
  const configuredContexts = new WeakSet();
  let operationQueue = Promise.resolve();

  async function getPage({ requireSession = true } = {}) {
    let connection;
    try {
      connection = await connectFn();
    } catch (error) {
      throw new PJeBridgeError('CDP_UNAVAILABLE', error.message, { retryable: true });
    }
    if (!configuredContexts.has(connection.context)) {
      await liteModeFn(connection.context, { keepImages: true });
      configuredContexts.add(connection.context);
    }
    const page = requireSession
      ? await findAuthenticatedPJePage(connection.context)
      : await findOfficialPJePage(connection.context);
    return page;
  }

  const retification = createRetificationOperations({ getPage });
  const documentJoin = createDocumentJoinOperations({ getPage });
  const communications = createCommunicationOperations({ getPage });
  const documentIssue = createDocumentIssueOperations({ getPage });
  const processMetadata = createProcessMetadataOperations({ getPage });

  async function getState() {
    const page = await getPage();
    const url = page.url();
    const title = await page.title();
    const visibleText = await page.locator('main, [role="main"], #pageBody')
      .first().textContent().catch(() => '');
    const cnj = `${url}\n${visibleText || ''}`.match(CNJ_TJPA_RE)?.[0] || null;
    return {
      status: 'connected',
      url,
      title,
      process_number: cnj,
      authenticated: true,
    };
  }

  async function extractDocument(payload) {
    const page = await getPage();
    const maxChars = Math.min(Math.max(Number(payload.max_chars) || 20_000, 1), 20_000);
    const result = await page.evaluate(
      ({ containers, limit }) => {
        const selection = window.getSelection()?.toString().trim() || '';
        if (selection.length > 20) {
          return {
            source: 'selection',
            selector: null,
            text: selection.slice(0, limit),
            truncated: selection.length > limit,
          };
        }
        for (const selector of containers) {
          const element = document.querySelector(selector);
          const text = element?.textContent?.trim() || '';
          if (text.length > 30) {
            return {
              source: 'recognized_container',
              selector,
              text: text.slice(0, limit),
              truncated: text.length > limit,
            };
          }
        }
        return null;
      },
      { containers: selectors.documentoContainers, limit: maxChars },
    );
    if (!result) {
      throw new PJeBridgeError(
        'LOCATOR_NOT_FOUND',
        'Selecione o texto ou abra um documento reconhecido antes de extrair.',
      );
    }
    return {
      ...result,
      character_count: result.text.length,
    };
  }

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

  async function openProcess(payload, timeoutMs) {
    const processNumber = normalizeCnj(payload.process_number || payload.value);
    await searchProcess({ criterion: 'numero_cnj', value: processNumber, limit: 1 }, timeoutMs);
    const page = await getPage();
    const link = page.getByText(processNumber, { exact: false }).first();
    await link.waitFor({ state: 'visible', timeout: timeoutMs });
    const context = page.context();
    // O PJe às vezes reutiliza a janela nomeada dos autos, portanto pode não
    // emitir um novo evento `page`. A espera curta evita consumir todo o prazo
    // antes de procurar a aba já reutilizada no contexto.
    const popupPromise = context.waitForEvent('page', {
      timeout: Math.min(timeoutMs, 5_000),
    }).catch(() => null);
    const onDialog = (dialog) => dialog.accept().catch(() => {});
    page.once('dialog', onDialog);
    try {
      await link.click();
    } finally {
      page.off('dialog', onDialog);
    }
    let autos = await popupPromise;
    if (!autos) autos = findAutosPage(context);
    if (!autos) {
      throw new PJeBridgeError('PAGE_NOT_FOUND', 'Os autos digitais não foram abertos.', {
        retryable: true,
      });
    }
    await autos.waitForLoadState('domcontentloaded', { timeout: timeoutMs }).catch(() => {});
    await autos.locator('body').waitFor({ state: 'visible', timeout: timeoutMs });
    return {
      status: 'opened',
      process_number: processNumber,
      route: '/pje/Processo/ConsultaProcesso/Detalhe/listAutosDigitais.seam',
      document_controls: await autos.locator(
        'a[id^="divTimeLine:"][id$=":j_id407"], a[id^="divTimeLine:"][id$=":j_id422"]',
      ).count(),
      pdf_visible: await autos.locator(DOCUMENT_FRAME).count() > 0,
    };
  }

  async function listDocuments(payload = {}) {
    const page = await getPage();
    const autos = findAutosPage(page.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const limit = Math.min(Math.max(Number(payload.limit) || 100, 1), 500);
    const items = await autos.locator(
      'a[id^="divTimeLine:"][id$=":j_id407"], a[id^="divTimeLine:"][id$=":j_id422"]',
    ).evaluateAll((elements, maxItems) => elements.slice(0, maxItems).map((element) => {
      const text = String(element.textContent || '').replace(/\s+/g, ' ').trim();
      const match = text.match(/^(\d+)\s*-\s*(.*)$/);
      return {
        document_id: match ? match[1] : null,
        title: match ? match[2].slice(0, 300) : text.slice(0, 300),
        control_id: element.id || null,
      };
    }), limit);
    const unique = [];
    const seen = new Set();
    for (const item of items) {
      const key = item.document_id || item.control_id;
      if (!key || seen.has(key)) continue;
      seen.add(key);
      unique.push(item);
    }
    return { status: 'listed', total: unique.length, documents: unique };
  }

  async function openExpedients() {
    const page = await getPage();
    const autos = findAutosPage(page.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const tab = autos.locator('#navbar\\:linkAbaExpedientes1').first();
    await tab.waitFor({ state: 'visible', timeout: 10_000 });
    await tab.click();
    await autos.waitForFunction(() =>
      document.getElementById('processoExpedienteTab_lbl')?.className.includes('rich-tab-active'),
    null, { timeout: 10_000 });
    const rows = await autos.locator(
      'table[id="processoParteExpedienteMenuGridList"] tr.rich-table-row',
    ).count();
    return {
      status: 'opened',
      route: '/pje/Processo/ConsultaProcesso/Detalhe/listAutosDigitais.seam',
      row_count: rows,
      mutating: false,
    };
  }

  async function listExpedients(payload = {}) {
    await openExpedients();
    const page = await getPage();
    const autos = findAutosPage(page.context());
    const limit = Math.min(Math.max(Number(payload.limit) || 100, 1), 500);
    const table = autos.locator('table[id="processoParteExpedienteMenuGridList"]');
    const result = await table.evaluate((element, maxItems) => {
      const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const headers = Array.from(element.querySelectorAll('thead th, thead td')).map((cell) =>
        clean(Array.from(cell.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE)
          .map((node) => node.textContent).join(' ')));
      const rows = Array.from(element.querySelectorAll('tr.rich-table-row')).slice(0, maxItems)
        .map((row, index) => {
          const cells = Array.from(row.querySelectorAll(':scope > td'));
          const values = cells.map((cell) => clean(cell.textContent));
          const item = { index };
          values.forEach((value, cellIndex) => {
            const label = clean(headers[cellIndex]).toLocaleLowerCase('pt-BR');
            if (/ato de comunica/.test(label)) item.ato_comunicacao = value.slice(0, 1000);
            else if (/data limite/.test(label)) item.data_limite = value.slice(0, 200);
            else if (/document/.test(label)) item.documentos = value.slice(0, 1000);
            else if (/fechado/.test(label)) item.fechado = value.slice(0, 100);
            else if (value) item[`coluna_${cellIndex + 1}`] = value.slice(0, 1000);
          });
          item.acoes = {
            visualizar_ato: Boolean(row.querySelector('a[title="Visualizar ato"]')),
            validar_assinatura: Boolean(row.querySelector('a[title="Validar Assinatura Digital"]')),
            responder: Boolean(row.querySelector('a[title="Resposta"]')),
          };
          return item;
        });
      return {
        headers: headers.filter(Boolean),
        rows,
        total_rendered: element.querySelectorAll('tr.rich-table-row').length,
      };
    }, limit);
    return {
      status: 'listed',
      total: result.total_rendered,
      returned: result.rows.length,
      headers: result.headers,
      expedients: result.rows,
      response_action_executed: false,
      mutating: false,
    };
  }

  async function navigateDocument(payload, timeoutMs) {
    const direction = String(payload.direction || '').trim().toLowerCase();
    const controlId = DOCUMENT_NAVIGATION[direction];
    if (!controlId) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Direção inválida: first, previous, next ou last.');
    }
    const page = await getPage();
    const autos = findAutosPage(page.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const control = autos.locator(`[id="${controlId}"]`).first();
    await control.waitFor({ state: 'visible', timeout: timeoutMs });
    if (String(await control.getAttribute('class') || '').includes('disabled')) {
      return { status: 'boundary', direction, changed: false };
    }
    const previousSource = await autos.locator(DOCUMENT_FRAME).first().getAttribute('src');
    const previousTitle = await autos.locator(DOCUMENT_TITLE).first().textContent().catch(() => '');
    await control.click();
    await autos.waitForFunction(
      ({ previousSource, previousTitle }) => {
        const source = document.querySelector(
          'iframe[src*="/seam/resource/rest/pje-legacy/documento/download/"]',
        )?.getAttribute('src') || '';
        const title = document.querySelector(
          'a[title="Abrir documento em outra página"]',
        )?.textContent || '';
        return source !== previousSource || title !== previousTitle;
      },
      { previousSource, previousTitle },
      { timeout: timeoutMs },
    );
    const title = String(await autos.locator(DOCUMENT_TITLE).first().textContent() || '')
      .replace(/\s+/g, ' ').trim();
    const match = title.match(/^(\d+)\s*-\s*(.*)$/);
    return {
      status: 'navigated',
      direction,
      changed: true,
      document: {
        document_id: match ? match[1] : null,
        title: match ? match[2].slice(0, 300) : title.slice(0, 300),
      },
    };
  }

  function artifactDirectory() {
    const configured = String(process.env.PJE_BROWSER_ARTIFACT_DIR || '').trim();
    const storage = String(process.env.PJE_STORAGE_DIR || '').trim();
    const target = configured || (storage ? path.join(storage, 'browser-bridge') : '');
    if (!target || !path.isAbsolute(target)) {
      throw new PJeBridgeError(
        'STORAGE_NOT_CONFIGURED',
        'Configure PJE_BROWSER_ARTIFACT_DIR ou PJE_STORAGE_DIR para baixar documentos.',
      );
    }
    fs.mkdirSync(target, { recursive: true, mode: 0o700 });
    return target;
  }

  async function downloadCurrentDocument() {
    const page = await getPage();
    const autos = findAutosPage(page.context());
    if (!autos) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Abra os autos digitais primeiro.');
    const source = await autos.locator(DOCUMENT_FRAME).first().getAttribute('src');
    if (!source) throw new PJeBridgeError('DOCUMENT_NOT_FOUND', 'Documento aberto sem PDF reconhecido.');
    const url = new URL(source, autos.url());
    if (url.hostname !== 'pje.tjpa.jus.br' ||
        !url.pathname.startsWith('/pje/seam/resource/rest/pje-legacy/documento/download/')) {
      throw new PJeBridgeError('UNEXPECTED_HOST', 'Endpoint documental fora da allowlist.');
    }
    const response = await autos.context().request.get(url.href, { timeout: 30_000 });
    if (!response.ok()) {
      throw new PJeBridgeError('DOCUMENT_DOWNLOAD_FAILED', `Documento respondeu HTTP ${response.status()}.`, {
        retryable: response.status() >= 500,
      });
    }
    const body = await response.body();
    if (body.length < 5 || body.subarray(0, 5).toString('ascii') !== '%PDF-') {
      throw new PJeBridgeError('INVALID_DOCUMENT', 'Resposta documental não contém um PDF válido.');
    }
    const sha256 = crypto.createHash('sha256').update(body).digest('hex');
    const target = path.join(artifactDirectory(), `${sha256}.pdf`);
    fs.writeFileSync(target, body, { mode: 0o600 });
    fs.chmodSync(target, 0o600);
    return {
      status: 'downloaded',
      path: target,
      bytes: body.length,
      sha256,
      content_type: response.headers()['content-type'] || '',
    };
  }

  async function searchProcess(payload, timeoutMs) {
    const search = normalizeSearchPayload(payload);
    const page = await getPage();
    await page.goto(selectors.consultaUrl, {
      waitUntil: 'domcontentloaded',
      timeout: timeoutMs,
    });
    await assertAuthenticatedSession(page);

    const fillField = async (selector, value, label) => {
      const field = page.locator(selector).first();
      if (!await field.count() || !await field.isVisible()) {
        throw new PJeBridgeError('LOCATOR_NOT_FOUND', `Campo ${label} não encontrado.`);
      }
      await field.clear();
      await field.fill(value);
    };
    const selectByLabelOrValue = async (selector, requested, label) => {
      const field = page.locator(selector).first();
      if (!await field.count() || !await field.isVisible()) {
        throw new PJeBridgeError('LOCATOR_NOT_FOUND', `Campo ${label} não encontrado.`);
      }
      const option = await field.evaluate((select, wanted) => {
        const normalize = (value) => String(value || '').normalize('NFD')
          .replace(/[\u0300-\u036f]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
        const needle = normalize(wanted);
        const options = Array.from(select.options).filter((item) =>
          !/NoSelectionConverter/.test(item.value));
        const exact = options.find((item) => item.value === wanted || normalize(item.textContent) === needle);
        const partial = options.filter((item) => normalize(item.textContent).includes(needle));
        const match = exact || (partial.length === 1 ? partial[0] : null);
        return match ? { value: match.value, label: String(match.textContent || '').trim() } : null;
      }, requested);
      if (!option) {
        throw new PJeBridgeError(
          'INVALID_REQUEST',
          `${label} não possui opção única correspondente a "${requested}".`,
        );
      }
      await field.selectOption(option.value);
      return option.label;
    };
    const ensureCriminalFiltersOpen = async () => {
      const probe = page.locator(selectors.inputNumeroProcedimentoCriminal).first();
      if (await probe.count() && await probe.isVisible()) return;
      const header = page.locator(selectors.cabecalhoFiltrosCriminais).first();
      if (!await header.count() || !await header.isVisible()) {
        throw new PJeBridgeError(
          'LOCATOR_NOT_FOUND',
          'Seção Filtros Criminais não encontrada.',
        );
      }
      await header.click();
      await probe.waitFor({ state: 'visible', timeout: Math.min(timeoutMs, 10000) });
    };

    if ([
      'orgao_origem_criminal',
      'procedimento_criminal',
      'ano_procedimento_criminal',
      'protocolo_policia',
    ].includes(search.criterion)) {
      await ensureCriminalFiltersOpen();
    }

    if (search.criterion === 'numero_cnj') {
      const digits = search.value.replace(/\D/g, '');
      const parts = [
        digits.slice(0, 7),
        digits.slice(7, 9),
        digits.slice(9, 13),
        digits.slice(13, 14),
        digits.slice(14, 16),
        digits.slice(16),
      ];
      const fields = page.locator(selectors.inputNumeroProcesso);
      const count = await fields.count();
      if (count < 5) {
        throw new PJeBridgeError(
          'LOCATOR_NOT_FOUND',
          'Campos segmentados do número CNJ não foram encontrados.',
        );
      }
      const values = count >= 6
        ? parts
        : [parts[0], parts[1], parts[2], parts[4], parts[5]];
      for (let index = 0; index < values.length; index += 1) {
        const field = fields.nth(index);
        if (count < 6 || await field.isEditable()) {
          await field.clear();
          await field.fill(values[index]);
        }
      }
    } else if (search.criterion === 'cpf' || search.criterion === 'cnpj') {
      const radioSelector = search.criterion === 'cpf'
        ? selectors.radioCpf
        : selectors.radioCnpj;
      const radio = page.locator(radioSelector).first();
      if (await radio.count()) await radio.check({ force: true });
      const field = page.locator(selectors.inputDocumentoParte).first();
      await field.clear();
      await field.fill(search.value);
    } else if ([
      'nome_parte',
      'nome_requerente',
      'nome_requerido',
      'nome_advogado',
      'outros_nomes',
      'numero_documento',
      'assunto',
      'classe_judicial',
      'protocolo_policia',
      'ano_procedimento_criminal',
    ].includes(search.criterion)) {
      const selector = search.criterion === 'nome_advogado'
        ? selectors.inputNomeAdvogado
        : search.criterion === 'outros_nomes'
          ? selectors.inputOutrosNomes
          : search.criterion === 'numero_documento'
            ? selectors.inputNumeroDocumento
            : search.criterion === 'assunto'
              ? selectors.inputAssunto
              : search.criterion === 'classe_judicial'
                ? selectors.inputClasseJudicial
                : search.criterion === 'protocolo_policia'
                  ? selectors.inputProtocoloPolicia
                  : search.criterion === 'ano_procedimento_criminal'
                    ? selectors.inputAnoProcedimentoCriminal
            : selectors.inputNomeParte;
      await fillField(selector, search.value, search.criterion);
    } else if (search.criterion === 'oab') {
      const numberField = page.locator(selectors.inputNumeroOab).first();
      const letterField = page.locator(selectors.inputLetraOab).first();
      const stateField = page.locator(selectors.selectUfOab).first();
      await numberField.clear();
      await numberField.fill(search.value);
      if (await letterField.count()) {
        await letterField.clear();
        if (search.letra_oab) await letterField.fill(search.letra_oab);
      }
      await stateField.selectOption(search.uf_oab);
    } else if ([
      'jurisdicao',
      'orgao_julgador',
      'prioridade_processual',
      'orgao_origem_criminal',
    ].includes(search.criterion)) {
      const selector = {
        jurisdicao: selectors.selectJurisdicao,
        orgao_julgador: selectors.selectOrgaoJulgador,
        prioridade_processual: selectors.selectPrioridadeProcessual,
        orgao_origem_criminal: selectors.selectOrgaoOrigemCriminal,
      }[search.criterion];
      await selectByLabelOrValue(selector, search.value, search.criterion);
    } else if (search.criterion === 'data_autuacao') {
      await fillField(selectors.inputDataAutuacaoInicio, search.value, 'data inicial de autuação');
      await fillField(selectors.inputDataAutuacaoFim, search.value_end, 'data final de autuação');
    } else if (search.criterion === 'valor_causa') {
      await fillField(selectors.inputValorCausaInicio, search.value, 'valor inicial da causa');
      await fillField(selectors.inputValorCausaFim, search.value_end, 'valor final da causa');
    } else if (search.criterion === 'procedimento_criminal') {
      await fillField(selectors.inputNumeroProcedimentoCriminal, search.value, 'número do procedimento criminal');
      if (search.value_end) {
        await fillField(selectors.inputAnoProcedimentoCriminal, search.value_end, 'ano do procedimento criminal');
      }
    } else {
      const field = page.locator(selectors.inputMovimentoProcessual).first();
      await field.fill(search.value);
      await field.press('ArrowDown');
      const candidates = page.locator(selectors.sugestoesMovimentoProcessual)
        .filter({ hasNotText: /termo não encontrado/i });
      await candidates.first().waitFor({ state: 'visible', timeout: Math.min(timeoutMs, 8000) })
        .catch(() => {});
      if (!await candidates.count()) {
        throw new PJeBridgeError(
          'INVALID_REQUEST',
          `Movimento processual não encontrado para "${search.value}".`,
        );
      }
      const exact = candidates.filter({ hasText: new RegExp(`^\\s*${search.value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'i') });
      await (await exact.count() ? exact.first() : candidates.first()).click();
    }

    const button = page.getByRole('button', { name: /pesquisar/i })
      .or(page.locator(selectors.botaoPesquisar))
      .first();
    await page.evaluate(() => {
      window.__pjeSearchMutationCount = 0;
      window.__pjeSearchObserver?.disconnect();
      const root = document.body;
      window.__pjeSearchObserver = new MutationObserver(() => {
        window.__pjeSearchMutationCount += 1;
      });
      window.__pjeSearchObserver.observe(root, {
        childList: true,
        subtree: true,
        characterData: true,
      });
    });
    const responseFinished = page.waitForResponse((response) => {
      try {
        const url = new URL(response.url());
        return response.request().method() === 'POST' &&
          url.hostname === 'pje.tjpa.jus.br' &&
          url.pathname.endsWith('/Processo/ConsultaProcesso/listView.seam');
      } catch (_) {
        return false;
      }
    }, { timeout: Math.min(timeoutMs, 15_000) }).catch(() => null);
    await button.click();
    const searchResponse = await responseFinished;
    if (searchResponse) await searchResponse.finished().catch(() => {});
    await page.waitForLoadState('domcontentloaded', {
      timeout: Math.min(timeoutMs, 10000),
    }).catch(() => {});
    await page.waitForFunction(() => {
      const tableHasProcess = Array.from(document.querySelectorAll('table'))
        .some((table) => table.getClientRects().length > 0 &&
          /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/.test(table.innerText || ''));
      const explicitEmpty = /nenhum\s+(registro|processo)|sem\s+resultado/i
        .test(document.body.innerText || '');
      const busy = Array.from(document.querySelectorAll(
        '[aria-busy="true"], .ui-blockui, .rich-mpnl-mask, #modalStatus, [id$="status.start"]',
      ))
        .some((item) => item.getClientRects().length > 0);
      const updated = typeof window.__pjeSearchMutationCount === 'undefined'
        || Number(window.__pjeSearchMutationCount || 0) > 0;
      return !busy && updated && (tableHasProcess || explicitEmpty);
    }, null, { timeout: Math.min(timeoutMs, 15000) }).catch(() => {});
    await page.evaluate(() => {
      window.__pjeSearchObserver?.disconnect();
      delete window.__pjeSearchObserver;
      delete window.__pjeSearchMutationCount;
    });
    await page.locator(`${selectors.tabelaResultados}, ${selectors.avisoSemResultados}`)
      .first().waitFor({ state: 'attached', timeout: Math.min(timeoutMs, 10_000) })
      .catch(() => {});

    const extractSearchPage = () => page.evaluate(
      ({ tableSelector }) => {
        const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
        const roots = Array.from(document.querySelectorAll(tableSelector));
        const tables = Array.from(new Set(roots.flatMap((root) =>
          root.tagName === 'TABLE' ? [root] : Array.from(root.querySelectorAll('table')))));
        const table = tables.find((candidate) => {
          const text = clean(candidate.innerText);
          return candidate.getClientRects().length > 0 &&
            /Processo/i.test(text) && /Órgão julgador|Orgao julgador/i.test(text);
        }) || tables.find((candidate) =>
          candidate.getClientRects().length > 0 &&
          /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/.test(clean(candidate.innerText)));
        const headers = table
          ? Array.from(table.querySelectorAll('thead th, thead td'))
            .map(cell => clean(cell.textContent))
          : [];
        const rows = table
          ? Array.from(table.querySelectorAll('tbody tr')).map(row => {
            const columns = Array.from(row.querySelectorAll('td'))
              .map(cell => clean(cell.textContent));
            const link = row.querySelector('a[href]');
            let hrefPath = '';
            if (link) {
              try {
                const url = new URL(link.href, location.href);
                hrefPath = url.pathname + url.hash;
              } catch (_) {}
            }
            return { headers, columns, href_path: hrefPath };
          })
          : [];
        const body = document.body.innerText || '';
        const total = body.match(/(\d+)\s*resultados?\s*encontrados?/i);
        return {
          rows,
          total: total ? Number(total[1]) : 0,
          total_present: Boolean(total),
          table_found: Boolean(table),
          no_results: /nenhum\s+(registro|processo)|sem\s+resultado/i.test(body),
        };
      },
      { tableSelector: selectors.tabelaResultados },
    );
    let extracted = await extractSearchPage();
    if (!extracted.table_found && !extracted.no_results) {
      await page.waitForFunction(() => {
        const body = document.body.innerText || '';
        const explicitEmpty = /nenhum\s+(registro|processo)|sem\s+resultado/i.test(body);
        const tableReady = Array.from(document.querySelectorAll('table')).some((table) =>
          table.getClientRects().length > 0 &&
          /Processo/i.test(table.innerText || '') &&
          /Órgão julgador|Orgao julgador/i.test(table.innerText || ''));
        return explicitEmpty || tableReady;
      }, null, { timeout: Math.min(timeoutMs, 15000) }).catch(() => {});
      extracted = await extractSearchPage();
      for (let frame = 0;
        frame < 20 && !extracted.table_found && !extracted.no_results;
        frame += 1) {
        await page.evaluate(() => new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve))));
        extracted = await extractSearchPage();
      }
    }
    const unfilteredResults = mapSearchRows(extracted.rows, search.limit);
    const filtered = filterResultsByPartyRole(unfilteredResults, search);
    const results = filtered.results;
    const totalForm = extracted.total || unfilteredResults.length;
    const resultComplete = Boolean(
      extracted.no_results ||
      (extracted.total_present && totalForm <= unfilteredResults.length),
    );
    const total = filtered.roleField ? results.length : totalForm;
    const emptyConfirmed = Boolean(
      extracted.no_results || (extracted.total_present && extracted.total === 0),
    );
    const response = {
      status: results.length ? 'found' : emptyConfirmed ? 'not_found' : 'inconclusive',
      criterio_aplicado: search.criterion,
      valor_busca_mascarado: maskSearchValue(search),
      total_encontrados: total,
      total_formulario: totalForm,
      retornados: results.length,
      grade_detectada: extracted.table_found,
      linhas_grade_brutas: extracted.rows.length,
      limite_aplicado: search.limit,
      sem_resultado_confirmado: Boolean(
        !results.length && emptyConfirmed,
      ),
      resultado_completo: resultComplete,
      inconclusivo: Boolean(!results.length && !emptyConfirmed),
      resultados: results,
      somente_leitura: true,
      rota: '/pje/Processo/ConsultaProcesso/listView.seam',
    };
    if (filtered.roleField) {
      response.filtro_polo_aplicado = filtered.roleField;
      response.criterio_formulario = 'nome_parte';
    }
    if (search.criterion === 'numero_cnj') {
      response.process_number = search.value;
    }
    return response;
  }

  async function consumeSignal() {
    const page = await getPage();
    return page.evaluate(() => {
      const element = document.getElementById('pje-ai-signal-queue');
      if (!element) return null;
      let queue;
      try {
        queue = JSON.parse(element.dataset.queue || '[]');
      } catch (_) {
        element.dataset.queue = '[]';
        return { invalid_payload: true };
      }
      if (!Array.isArray(queue) || queue.length === 0) return null;
      const now = Date.now();
      for (const queued of queue) {
        if (queued.state === 'processing' &&
            now - Date.parse(queued.processing_at || 0) > 120_000) {
          queued.state = 'pending';
          delete queued.processing_at;
        }
      }
      const signal = queue.find((queued) => queued.state === 'pending');
      if (!signal) {
        element.dataset.queue = JSON.stringify(queue);
        return null;
      }
      signal.state = 'processing';
      signal.processing_at = new Date(now).toISOString();
      signal.attempts = (signal.attempts || 0) + 1;
      element.dataset.queue = JSON.stringify(queue);
      return signal;
    });
  }

  async function ackSignal(payload) {
    if (typeof payload.request_id !== 'string') {
      throw new PJeBridgeError('INVALID_REQUEST', 'ACK sem request_id.');
    }
    const page = await getPage();
    return page.evaluate(({ requestId, outcome }) => {
      const element = document.getElementById('pje-ai-signal-queue');
      if (!element) return { acknowledged: false };
      let queue;
      try {
        queue = JSON.parse(element.dataset.queue || '[]');
      } catch (_) {
        element.dataset.queue = '[]';
        return { acknowledged: false };
      }
      const index = queue.findIndex((signal) => signal.request_id === requestId);
      if (index < 0) return { acknowledged: false };
      queue.splice(index, 1);
      element.dataset.queue = JSON.stringify(queue);
      return { acknowledged: true, outcome };
    }, {
      requestId: payload.request_id,
      outcome: payload.outcome || 'completed',
    });
  }

  async function dispatchEvent(payload) {
    if (!ALLOWED_EVENTS.has(payload.event)) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Evento DOM não permitido.');
    }
    const page = await getPage();
    await page.evaluate(
      ({ eventName, detail }) => {
        window.dispatchEvent(new CustomEvent(eventName, { detail }));
      },
      { eventName: payload.event, detail: payload.detail || {} },
    );
    return { dispatched: true, event: payload.event };
  }

  async function executeCommand(command, payload, timeoutMs) {
    switch (command) {
      case 'get_state':
        return getState();
      case 'extract_document':
        return extractDocument(payload);
      case 'search_process':
        return searchProcess(payload, timeoutMs);
      case 'open_process':
        return openProcess(payload, timeoutMs);
      case 'list_documents':
        return listDocuments(payload);
      case 'navigate_document':
        return navigateDocument(payload, timeoutMs);
      case 'download_current_document':
        return downloadCurrentDocument();
      case 'open_expedients':
        return openExpedients();
      case 'list_expedients':
        return listExpedients(payload);
      case 'inspect_document_join':
        return documentJoin.inspect(payload, timeoutMs);
      case 'inspect_communications':
        return communications.inspect(payload, timeoutMs);
      case 'analyze_communication_plan':
        return communications.analyze(payload);
      case 'inspect_document_issue':
        return documentIssue.inspect(payload, timeoutMs);
      case 'analyze_document_issue_plan':
        return documentIssue.analyze(payload);
      case 'inspect_process_metadata':
        return processMetadata.inspect(payload, timeoutMs);
      case 'preview_process_label_change':
        return processMetadata.preview(payload, timeoutMs);
      case 'apply_process_label_change':
        return processMetadata.apply(payload, timeoutMs);
      case 'inspect_retification':
        return retification.inspect(payload, timeoutMs);
      case 'list_retification_candidates':
        return retification.listCandidates(payload, timeoutMs);
      case 'preview_retification':
        return retification.preview(payload, timeoutMs);
      case 'simulate_retification':
        return retification.simulate(payload, timeoutMs);
      case 'apply_retification':
        return retification.apply(payload, timeoutMs);
      case 'consume_signal':
        return consumeSignal();
      case 'ack_signal':
        return ackSignal(payload);
      case 'dispatch_event':
        return dispatchEvent(payload);
      default:
        throw new PJeBridgeError('INVALID_REQUEST', 'Comando não implementado.');
    }
  }

  return function execute(command, payload, timeoutMs) {
    const current = operationQueue.then(
      () => executeCommand(command, payload, timeoutMs),
      () => executeCommand(command, payload, timeoutMs),
    );
    operationQueue = current.catch(() => {});
    return current;
  };
}

module.exports = {
  ALLOWED_EVENTS,
  CNJ_TJPA_RE,
  createPJeOperations,
  filterResultsByPartyRole,
  mapSearchRows,
  maskSearchValue,
  normalizeCnj,
  normalizeSearchPayload,
};
