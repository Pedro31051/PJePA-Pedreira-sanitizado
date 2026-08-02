'use strict';

const crypto = require('crypto');
const { PJeBridgeError } = require('./contracts');

const RETIFICATION_URL =
  'https://pje.tjpa.jus.br/pje/Processo/RetificacaoAutuacao/listView.seam';
const RETIFICATION_PATH = '/pje/Processo/RetificacaoAutuacao/listView.seam';
const UPDATE_PATH = '/pje/Processo/RetificacaoAutuacao/updateRetificacaoAutuacao.seam';
const PREVIEW_TTL_MS = 5 * 60 * 1000;

const INSTITUTIONS = Object.freeze({
  mppa: Object.freeze({
    name: 'MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ',
    cnpj: '05.054.960/0001-58',
    cnpj_digits: '05054960000158',
    source: 'https://www.mppa.mp.br/data/files/FE/76/0D/C2/5C80191002E18109180808FF/PE%20019-2024%20-%20Edital%20RP%20SERV%20FABRICANTE%20MICROSOFT%20-%20p%20assinatura%20-%20assinado.pdf',
    verified_on: '2026-08-01',
  }),
});

const TABS = Object.freeze({
  initial: 'form_lbl',
  subjects: 'assunto_lbl',
  jurisdiction: 'tabRetificarCompetencia_lbl',
  parties: 'tabPartes_lbl',
  characteristics: 'caracteristicaProcesso_lbl',
});

const PARTY_GRIDS = Object.freeze({
  ativo: 'gridPartesPoloAtivoList',
  passivo: 'gridPartesPoloPassivoList',
  outros: 'gridPartesOutrosParticipantesList',
});

const PARTY_FIELDS = Object.freeze({
  tipo_parte: 'formInserirParteProcesso:tpParteAlteracao',
  nome_social: 'formInserirParteProcesso:nomeSocial',
  nome_civil: 'formInserirParteProcesso:nomeDecoration:nome',
  nome_genitora: 'formInserirParteProcesso:nomeGenitoraDecoration:nomeGenitora',
  nome_parte_processo: 'formInserirParteProcesso:nomeParteDecoration:nomeParte',
  nome_genitor: 'formInserirParteProcesso:nomeGenitorDecoration:nomeGenitor',
  sexo: 'formInserirParteProcesso:sexoDecoration:sexo',
  data_nascimento: 'formInserirParteProcesso:dataNascimentoDecoration:dataNascimentoInputDate',
  data_obito: 'formInserirParteProcesso:dataObitoDecoration:dataObitoInputDate',
  etnia: 'formInserirParteProcesso:etniaDecoration:etnia',
  estado_civil: 'formInserirParteProcesso:estadoCivilDecoration:estadoCivil',
  escolaridade: 'formInserirParteProcesso:escolaridadeDecoration:escolaridade',
  pais_nascimento: 'formInserirParteProcesso:selectPaisNacionalidadeDecoration:selectPaisNacionalidade',
  parte_sigilosa: 'formInserirParteProcesso:j_id8718:comboParteSigilosa',
  situacao_rua: 'formInserirParteProcesso:j_id8736:comboMoradorRua',
  procuradoria_defensoria:
    'formInserirParteProcesso:comboRepresentanteDecoration:comboRepresentante',
});

const ADDRESS_FIELDS = Object.freeze({
  logradouro:
    'formInserirParteProcesso:cadastroPartePessoaEndereconomeLogradouroDecoration:cadastroPartePessoaEndereconomeLogradouro',
  numero:
    'formInserirParteProcesso:cadastroPartePessoaEndereconumeroEnderecoDecoration:cadastroPartePessoaEndereconumeroEndereco',
  complemento:
    'formInserirParteProcesso:cadastroPartePessoaEnderecocomplementoDecoration:cadastroPartePessoaEnderecocomplemento',
  correspondencia:
    'formInserirParteProcesso:cadastroPartePessoaEnderecoinCorrespondenciaDecoration:cadastroPartePessoaEnderecoinCorrespondencia',
});

const CONTACT_TYPES = new Set(['Email', 'Telefone Celular', 'Telefone Fixo']);

function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) =>
      `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function hash(value) {
  return crypto.createHash('sha256').update(stableJson(value)).digest('hex');
}

function normalizeCnj(value) {
  const digits = String(value || '').replace(/\D/g, '');
  if (digits.length !== 20 || digits[13] !== '8' || digits.slice(14, 16) !== '14') {
    throw new PJeBridgeError('INVALID_REQUEST', 'Número CNJ inválido ou fora do TJPA.');
  }
  return `${digits.slice(0, 7)}-${digits.slice(7, 9)}.${digits.slice(9, 13)}.` +
    `${digits[13]}.${digits.slice(14, 16)}.${digits.slice(16)}`;
}

function cleanObject(value, allowed, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new PJeBridgeError('INVALID_REQUEST', `${label} deve ser um objeto.`);
  }
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) {
    throw new PJeBridgeError('INVALID_REQUEST', `${label} contém campos não permitidos.`);
  }
  return value;
}

function normalizeChanges(raw) {
  const changes = cleanObject(
    raw,
    new Set(['dados_iniciais', 'assuntos', 'partes', 'ministerio_publico', 'caracteristicas']),
    'alteracoes',
  );
  const normalized = {};
  if (changes.dados_iniciais !== undefined) {
    const initial = cleanObject(
      changes.dados_iniciais,
      new Set(['classe_judicial']),
      'dados_iniciais',
    );
    const value = String(initial.classe_judicial || '').trim();
    if (!value || value.length > 240) {
      throw new PJeBridgeError('INVALID_REQUEST', 'classe_judicial inválida.');
    }
    normalized.dados_iniciais = { classe_judicial: value };
  }
  if (changes.assuntos !== undefined) {
    const subjects = cleanObject(changes.assuntos, new Set(['adicionar']), 'assuntos');
    if (!Array.isArray(subjects.adicionar) || subjects.adicionar.length !== 1) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Inclua um assunto por confirmação.');
    }
    normalized.assuntos = {
      adicionar: subjects.adicionar.map((item) => {
        const subject = cleanObject(item, new Set(['codigo', 'descricao']), 'assunto');
        const codigo = String(subject.codigo || '').replace(/\D/g, '');
        const descricao = String(subject.descricao || '').replace(/\s+/g, ' ').trim();
        if (!codigo && descricao.length < 3) {
          throw new PJeBridgeError('INVALID_REQUEST', 'Assunto exige código ou descrição.');
        }
        if (codigo.length > 12 || descricao.length > 240) {
          throw new PJeBridgeError('INVALID_REQUEST', 'Assunto excede o limite permitido.');
        }
        return { codigo, descricao };
      }),
    };
  }
  if (changes.partes !== undefined) {
    if (!Array.isArray(changes.partes) || changes.partes.length !== 1) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Edite uma parte por confirmação.');
    }
    normalized.partes = changes.partes.map((item) => {
      const edit = cleanObject(
        item,
        new Set(['polo', 'indice', 'dados', 'endereco', 'contato']),
        'edição de parte',
      );
      const polo = String(edit.polo || '').trim().toLowerCase();
      const indice = Number(edit.indice);
      if (!PARTY_GRIDS[polo] || !Number.isInteger(indice) || indice < 0 || indice > 500) {
        throw new PJeBridgeError('INVALID_REQUEST', 'Polo ou índice da parte inválido.');
      }
      const operationCount = ['dados', 'endereco', 'contato']
        .filter((key) => edit[key] !== undefined).length;
      if (operationCount !== 1) {
        throw new PJeBridgeError(
          'INVALID_REQUEST',
          'Informe exatamente dados, endereco ou contato por edição de parte.',
        );
      }
      const normalizedEdit = { polo, indice };
      if (edit.dados !== undefined) {
        const data = cleanObject(edit.dados, new Set(Object.keys(PARTY_FIELDS)), 'dados da parte');
        if (!Object.keys(data).length) {
          throw new PJeBridgeError('INVALID_REQUEST', 'Edição de parte sem dados.');
        }
        const normalizedData = {};
        for (const [key, rawValue] of Object.entries(data)) {
          const value = String(rawValue ?? '').replace(/\s+/g, ' ').trim();
          if (value.length > 300) {
            throw new PJeBridgeError('INVALID_REQUEST', `Campo ${key} excede 300 caracteres.`);
          }
          normalizedData[key] = value;
        }
        normalizedEdit.dados = normalizedData;
      }
      if (edit.endereco !== undefined) {
        const address = cleanObject(
          edit.endereco,
          new Set(['acao', 'indice', 'dados']),
          'endereco',
        );
        const acao = String(address.acao || '').trim().toLowerCase();
        const addressIndex = Number(address.indice);
        if (acao !== 'editar' || !Number.isInteger(addressIndex) || addressIndex < 0 || addressIndex > 50) {
          throw new PJeBridgeError(
            'INVALID_REQUEST',
            'endereco aceita, por ora, acao editar e um indice válido.',
          );
        }
        const data = cleanObject(address.dados, new Set(Object.keys(ADDRESS_FIELDS)), 'dados do endereco');
        if (!Object.keys(data).length) {
          throw new PJeBridgeError('INVALID_REQUEST', 'Edição de endereço sem dados.');
        }
        const normalizedData = {};
        for (const [key, rawValue] of Object.entries(data)) {
          if (key === 'correspondencia') {
            if (typeof rawValue !== 'boolean') {
              throw new PJeBridgeError('INVALID_REQUEST', 'correspondencia deve ser booleana.');
            }
            normalizedData[key] = rawValue;
          } else {
            const value = String(rawValue ?? '').replace(/\s+/g, ' ').trim();
            if (value.length > 300) {
              throw new PJeBridgeError('INVALID_REQUEST', `Campo ${key} excede 300 caracteres.`);
            }
            normalizedData[key] = value;
          }
        }
        normalizedEdit.endereco = { acao, indice: addressIndex, dados: normalizedData };
      }
      if (edit.contato !== undefined) {
        const contact = cleanObject(
          edit.contato,
          new Set(['acao', 'indice', 'tipo', 'valor']),
          'contato',
        );
        const acao = String(contact.acao || '').trim().toLowerCase();
        if (!['adicionar', 'remover'].includes(acao)) {
          throw new PJeBridgeError('INVALID_REQUEST', 'Ação de contato inválida.');
        }
        if (acao === 'remover') {
          const contactIndex = Number(contact.indice);
          if (!Number.isInteger(contactIndex) || contactIndex < 0 || contactIndex > 50) {
            throw new PJeBridgeError('INVALID_REQUEST', 'Índice de contato inválido.');
          }
          normalizedEdit.contato = { acao, indice: contactIndex };
        } else {
          const tipo = String(contact.tipo || '').replace(/\s+/g, ' ').trim();
          const valor = String(contact.valor || '').trim();
          if (!CONTACT_TYPES.has(tipo) || !valor || valor.length > 254) {
            throw new PJeBridgeError('INVALID_REQUEST', 'Tipo ou valor de contato inválido.');
          }
          normalizedEdit.contato = { acao, tipo, valor };
        }
      }
      return normalizedEdit;
    });
  }
  if (changes.ministerio_publico !== undefined) {
    const mp = cleanObject(
      changes.ministerio_publico,
      new Set(['acao', 'fundamento']),
      'ministerio_publico',
    );
    const acao = String(mp.acao || '').trim().toLowerCase();
    const fundamento = String(mp.fundamento || '').replace(/\s+/g, ' ').trim();
    if (!['adicionar', 'remover'].includes(acao) || fundamento.length < 5 || fundamento.length > 500) {
      throw new PJeBridgeError('INVALID_REQUEST', 'Ação ou fundamento do Ministério Público inválido.');
    }
    normalized.ministerio_publico = {
      acao,
      fundamento,
      instituicao: { nome: INSTITUTIONS.mppa.name, cnpj: INSTITUTIONS.mppa.cnpj },
    };
  }
  if (changes.caracteristicas !== undefined) {
    const characteristics = cleanObject(
      changes.caracteristicas,
      new Set(['tutela_liminar', 'valor_causa', 'justica_gratuita', 'prioridade']),
      'caracteristicas',
    );
    const item = {};
    for (const key of ['tutela_liminar', 'justica_gratuita']) {
      if (characteristics[key] !== undefined) {
        if (typeof characteristics[key] !== 'boolean') {
          throw new PJeBridgeError('INVALID_REQUEST', `${key} deve ser booleano.`);
        }
        item[key] = characteristics[key];
      }
    }
    if (characteristics.valor_causa !== undefined) {
      const value = String(characteristics.valor_causa).trim();
      if (!/^\d{1,15}(?:[.,]\d{1,2})?$/.test(value)) {
        throw new PJeBridgeError('INVALID_REQUEST', 'valor_causa inválido.');
      }
      item.valor_causa = value;
    }
    if (characteristics.prioridade !== undefined) {
      const value = String(characteristics.prioridade).replace(/\s+/g, ' ').trim();
      if (!value || value.length > 240) {
        throw new PJeBridgeError('INVALID_REQUEST', 'prioridade inválida.');
      }
      item.prioridade = value;
    }
    if (!Object.keys(item).length) {
      throw new PJeBridgeError('INVALID_REQUEST', 'caracteristicas sem alterações.');
    }
    normalized.caracteristicas = item;
  }
  if (!Object.keys(normalized).length) {
    throw new PJeBridgeError('INVALID_REQUEST', 'Nenhuma alteração foi informada.');
  }
  if (Object.keys(normalized).length !== 1) {
    throw new PJeBridgeError(
      'INVALID_REQUEST',
      'Altere uma seção por confirmação para evitar gravação parcial.',
    );
  }
  return normalized;
}

function createRetificationOperations({ getPage }) {
  const previews = new Map();

  async function waitForPost(page, action, timeoutMs) {
    const response = page.waitForResponse((candidate) => {
      try {
        return candidate.request().method() === 'POST' &&
          new URL(candidate.url()).hostname === 'pje.tjpa.jus.br';
      } catch (_) {
        return false;
      }
    }, { timeout: Math.min(timeoutMs, 15_000) }).catch(() => null);
    await action();
    await response;
  }

  async function activateTab(page, id, timeoutMs) {
    const tab = page.locator(`[id="${id}"]`).first();
    await tab.waitFor({ state: 'visible', timeout: timeoutMs });
    if (!String(await tab.getAttribute('class') || '').includes('rich-tab-active')) {
      await waitForPost(page, () => tab.click(), timeoutMs);
      await page.waitForFunction((tabId) =>
        document.getElementById(tabId)?.className.includes('rich-tab-active'), id,
      { timeout: timeoutMs });
    }
  }

  async function openForm(processNumber, timeoutMs) {
    const normalized = normalizeCnj(processNumber);
    const current = await getPage();
    const context = current.context();
    let listPage = [...context.pages()].reverse().find((candidate) => {
      try { return new URL(candidate.url()).pathname === RETIFICATION_PATH; }
      catch (_) { return false; }
    });
    if (!listPage) listPage = await context.newPage();
    await listPage.goto(RETIFICATION_URL, {
      waitUntil: 'domcontentloaded', timeout: timeoutMs,
    });
    const search = listPage.locator('input[id$=":search"]').first();
    if (!await search.isVisible().catch(() => false)) {
      throw new PJeBridgeError(
        'RETIFICATION_FORBIDDEN',
        'O perfil funcional atual não possui acesso à Retificação da Autuação.',
      );
    }
    const digits = normalized.replace(/\D/g, '');
    const fieldValues = {
      idNumeroSequencial: digits.slice(0, 7),
      idNumeroDigitoVerificador: digits.slice(7, 9),
      idAno: digits.slice(9, 13),
      idNumeroOrigemProcesso: digits.slice(16),
    };
    for (const [suffix, value] of Object.entries(fieldValues)) {
      const field = listPage.locator(`input[id$="${suffix}"]`).first();
      await field.clear();
      await field.fill(value);
    }
    await waitForPost(listPage, () => search.click(), timeoutMs);
    const row = listPage.locator(
      'table[id="consultaProcessoRetificacaoAutuacaoGridList"] tr.rich-table-row',
    ).filter({ hasText: normalized }).first();
    if (!await row.isVisible().catch(() => false)) {
      throw new PJeBridgeError(
        'PROCESS_NOT_EDITABLE_IN_PROFILE',
        'O processo não está disponível para retificação no perfil funcional atual.',
      );
    }
    const details = row.locator('a[title="Ver Detalhes"]').first();
    const existing = new Set(context.pages());
    const popup = context.waitForEvent('page', {
      timeout: Math.min(timeoutMs, 10_000),
    }).catch(() => null);
    await details.click();
    let formPage = await popup;
    if (!formPage) {
      formPage = [...context.pages()].reverse().find((candidate) =>
        !existing.has(candidate)) || [...context.pages()].reverse().find((candidate) => {
        try { return new URL(candidate.url()).pathname === UPDATE_PATH; }
        catch (_) { return false; }
      }) || null;
    }
    if (!formPage) {
      throw new PJeBridgeError('PAGE_NOT_FOUND', 'O formulário de retificação não foi aberto.');
    }
    await formPage.waitForLoadState('domcontentloaded', { timeout: timeoutMs }).catch(() => {});
    await formPage.waitForURL((value) => {
      try { return value.hostname === 'pje.tjpa.jus.br' && value.pathname === UPDATE_PATH; }
      catch (_) { return false; }
    }, { timeout: Math.min(timeoutMs, 10_000) }).catch(() => {});
    let formUrl = new URL(formPage.url());
    if (formUrl.hostname !== 'pje.tjpa.jus.br' || formUrl.pathname !== UPDATE_PATH) {
      const confirmed = [...context.pages()].reverse().find((candidate) => {
        try {
          const url = new URL(candidate.url());
          return url.hostname === 'pje.tjpa.jus.br' && url.pathname === UPDATE_PATH;
        } catch (_) {
          return false;
        }
      });
      if (confirmed) {
        formPage = confirmed;
        formUrl = new URL(formPage.url());
      }
    }
    if (formUrl.hostname !== 'pje.tjpa.jus.br' || formUrl.pathname !== UPDATE_PATH) {
      throw new PJeBridgeError('UNEXPECTED_HOST', 'A retificação abriu uma rota inesperada.');
    }
    const visibleContext = `${await formPage.title()}\n${await formPage.locator('body').textContent() || ''}`;
    if (!visibleContext.includes(normalized)) {
      throw new PJeBridgeError(
        'PROCESS_CONTEXT_MISMATCH',
        'O formulário aberto não confirmou o mesmo número de processo solicitado.',
      );
    }
    return { processNumber: normalized, listPage, formPage };
  }

  async function listCandidates(payload, timeoutMs) {
    const current = await getPage();
    const context = current.context();
    let page = [...context.pages()].reverse().find((candidate) => {
      try { return new URL(candidate.url()).pathname === RETIFICATION_PATH; }
      catch (_) { return false; }
    });
    if (!page) page = await context.newPage();
    await page.goto(RETIFICATION_URL, {
      waitUntil: 'domcontentloaded', timeout: timeoutMs,
    });
    const search = page.locator('input[id$=":search"]').first();
    if (!await search.isVisible().catch(() => false)) {
      throw new PJeBridgeError(
        'RETIFICATION_FORBIDDEN',
        'O perfil funcional atual não possui acesso à Retificação da Autuação.',
      );
    }
    for (const suffix of [
      'idNumeroSequencial',
      'idNumeroDigitoVerificador',
      'idAno',
      'idNumeroOrigemProcesso',
    ]) {
      const field = page.locator(`input[id$="${suffix}"]`).first();
      if (await field.isVisible().catch(() => false)) await field.clear();
    }
    await waitForPost(page, () => search.click(), timeoutMs);
    const limit = Math.min(Math.max(Number(payload.limit) || 20, 1), 100);
    const extracted = await page.evaluate((maxItems) => {
      const body = String(document.body?.textContent || '');
      const total = Number(body.match(/(\d+)\s+resultados?\s+encontrados?/i)?.[1] || 0);
      const rows = Array.from(document.querySelectorAll(
        'table[id="consultaProcessoRetificacaoAutuacaoGridList"] tr.rich-table-row',
      ));
      const seen = new Set();
      const processNumbers = [];
      for (const row of rows) {
        const match = String(row.textContent || '').match(
          /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/,
        );
        if (!match || seen.has(match[0])) continue;
        seen.add(match[0]);
        processNumbers.push(match[0]);
        if (processNumbers.length >= maxItems) break;
      }
      return { total, process_numbers: processNumbers };
    }, limit);
    return {
      status: 'listed',
      total: extracted.total,
      returned: extracted.process_numbers.length,
      process_numbers: extracted.process_numbers,
      profile_scoped: true,
      mutating: false,
    };
  }

  async function extractState(page, timeoutMs) {
    await activateTab(page, TABS.initial, timeoutMs);
    const initial = await page.evaluate(() => ({
      classe_judicial: document.querySelector(
        'select[id$="classeJudicialComboDecoration:classeJudicialCombo"]',
      )?.value || '',
    }));
    await activateTab(page, TABS.subjects, timeoutMs);
    const subjectRows = await page.locator(
      'table[id="l_processoAssuntoListList"] tr.rich-table-row',
    ).allTextContents();
    const subjects = await page.locator('a[title="Remover"][id^="l_processoAssuntoListList:"]')
      .evaluateAll((elements) => elements.map((element) => element.id));
    await activateTab(page, TABS.parties, timeoutMs);
    const partyRows = await page.locator(
      'table[id^="gridPartesPolo"] tr.rich-table-row, ' +
      'table[id="gridPartesOutrosParticipantesList"] tr.rich-table-row',
    ).allTextContents();
    const parties = await page.locator('a[title="Editar"][id*="gridPartes"]')
      .evaluateAll((elements) => elements.map((element) => element.id));
    await activateTab(page, TABS.characteristics, timeoutMs);
    const characteristics = await page.evaluate(() => {
      const checked = (prefix) => document.querySelector(
        `input[id^="${prefix}"]:checked`,
      )?.value || '';
      return {
        tutela_liminar: checked('caracteristica:tutelaLiminar:'),
        valor_causa: document.querySelector(
          'input[id="caracteristica:valorCausa:valorCausaDecoration:valorCausa"]',
        )?.value || '',
        justica_gratuita: checked('caracteristica:documentoCusta:'),
        prioridades: Array.from(document.querySelectorAll(
          '[id^="processoPrioridade"] option:checked',
        )).map((option) => option.value).filter(Boolean),
      };
    });
    return {
      initial,
      subject_control_ids: subjects,
      subject_row_sha256: subjectRows.map((text) => hash(String(text).replace(/\s+/g, ' ').trim())),
      party_control_ids: parties,
      party_row_sha256: partyRows.map((text) => hash(String(text).replace(/\s+/g, ' ').trim())),
      characteristics,
    };
  }

  async function inspect(payload, timeoutMs) {
    const opened = await openForm(payload.process_number, timeoutMs);
    try {
      const state = await extractState(opened.formPage, timeoutMs);
      const schema = {
        dados_iniciais: ['classe_judicial'],
        assuntos: { adicionar: ['codigo', 'descricao'] },
        partes: {
          polos: Object.keys(PARTY_GRIDS),
          campos_editaveis: Object.keys(PARTY_FIELDS),
          endereco_existente: {
            acoes: ['editar'],
            campos: Object.keys(ADDRESS_FIELDS),
          },
          contato: {
            acoes: ['adicionar', 'remover'],
            tipos: [...CONTACT_TYPES],
          },
          adicionar_nova_parte: false,
          remover_parte: true,
        },
        ministerio_publico: {
          instituicao: INSTITUTIONS.mppa.name,
          cnpj: INSTITUTIONS.mppa.cnpj,
          remover_se_identidade_exata: true,
          adicionar_nova_parte: false,
        },
        competencia: { disponivel: false, motivo: 'PJe não renderizou controles editáveis.' },
        caracteristicas: [
          'tutela_liminar', 'valor_causa', 'justica_gratuita', 'prioridade',
        ],
      };
      return {
        status: 'inspected',
        process_number: opened.processNumber,
        schema,
        current_counts: {
          assuntos: state.subject_control_ids.length,
          partes: state.party_control_ids.length,
        },
        state_sha256: hash(state),
        mutating: false,
      };
    } finally {
      await opened.formPage.close().catch(() => {});
    }
  }

  async function preview(payload, timeoutMs) {
    const processNumber = normalizeCnj(payload.process_number);
    const changes = normalizeChanges(payload.changes);
    const opened = await openForm(processNumber, timeoutMs);
    try {
      const state = await extractState(opened.formPage, timeoutMs);
      const token = crypto.randomBytes(32).toString('hex');
      const expiresAt = Date.now() + PREVIEW_TTL_MS;
      previews.set(token, {
        processNumber,
        changesHash: hash(changes),
        stateHash: hash(state),
        expiresAt,
      });
      for (const [key, item] of previews) {
        if (item.expiresAt < Date.now()) previews.delete(key);
      }
      return {
        status: 'preview_ready',
        process_number: processNumber,
        changes,
        confirmation_token: token,
        expires_at: new Date(expiresAt).toISOString(),
        state_sha256: hash(state),
        warning: 'A confirmação aplica somente uma seção e expira em cinco minutos.',
        mutating: false,
      };
    } finally {
      await opened.formPage.close().catch(() => {});
    }
  }

  async function chooseSelect(select, value) {
    const options = await select.locator('option').evaluateAll((elements) =>
      elements.map((option) => ({ value: option.value, label: option.textContent.trim() })));
    const normalized = String(value).trim().toLocaleLowerCase('pt-BR');
    const exact = options.find((option) => option.value === value ||
      option.label.toLocaleLowerCase('pt-BR') === normalized);
    if (!exact) {
      throw new PJeBridgeError('OPTION_NOT_FOUND', 'Opção solicitada não existe no formulário atual.');
    }
    await select.selectOption(exact.value);
    return exact.value;
  }

  async function applyInitial(page, changes, timeoutMs) {
    await activateTab(page, TABS.initial, timeoutMs);
    const select = page.locator(
      'select[id$="classeJudicialComboDecoration:classeJudicialCombo"]',
    ).first();
    await chooseSelect(select, changes.classe_judicial);
    await waitForPost(page, () => page.locator('#processoTrfForm\\:salvaProcessoButton').click(), timeoutMs);
    return { section: 'dados_iniciais', status: 'saved' };
  }

  async function applySubjects(page, changes, timeoutMs) {
    await activateTab(page, TABS.subjects, timeoutMs);
    let added = 0;
    for (const subject of changes.adicionar) {
      const description = page.locator(
        'input[id$="assuntoCompleto"]',
      ).first();
      const code = page.locator('input[id$="codAssuntoTrf"]').first();
      await description.clear();
      await code.clear();
      if (subject.descricao) await description.fill(subject.descricao);
      if (subject.codigo) await code.fill(subject.codigo);
      await waitForPost(page, () => page.locator(
        '#r_processoAssuntoListSearchForm\\:search',
      ).click(), timeoutMs);
      const query = subject.codigo || subject.descricao;
      const resultRow = page.locator(
        'table[id="r_processoAssuntoListList"] tr.rich-table-row',
      ).filter({ hasText: query }).first();
      const add = resultRow.locator('a[title="Adicionar"]').first();
      if (!await add.isVisible().catch(() => false)) {
        throw new PJeBridgeError('SUBJECT_NOT_FOUND', 'Assunto não foi encontrado para inclusão.');
      }
      await waitForPost(page, () => add.click(), timeoutMs);
      added += 1;
    }
    return { section: 'assuntos', status: 'saved', added };
  }

  async function applyPartyEdit(page, edit, timeoutMs, commit = true) {
    await activateTab(page, TABS.parties, timeoutMs);
    const grid = PARTY_GRIDS[edit.polo];
    const control = page.locator(
      `a[title="Editar"][id^="${grid}:${edit.indice}:"]`,
    ).first();
    if (!await control.isVisible().catch(() => false)) {
      throw new PJeBridgeError('PARTY_NOT_FOUND', 'Parte não encontrada no polo e índice informados.');
    }
    const context = page.context();
    const existing = new Set(context.pages());
    const popup = context.waitForEvent('page', {
      timeout: Math.min(timeoutMs, 10_000),
    }).then((candidate) => candidate).catch(() => null);
    await waitForPost(page, () => control.click(), timeoutMs);
    let partyPage = [...context.pages()].reverse().find((candidate) => !existing.has(candidate));
    if (!partyPage) {
      const inlineEditor = page.locator(
        '#formInserirParteProcesso\\:btnAtualizarParte:visible, ' +
        '#formInserirParteProcesso\\:btnComplementarDadosParte:visible',
      ).first();
      await inlineEditor.waitFor({
        state: 'visible', timeout: Math.min(timeoutMs, 10_000),
      }).catch(() => {});
      if (await inlineEditor.isVisible().catch(() => false)) partyPage = page;
    }
    if (!partyPage) partyPage = await popup;
    if (!partyPage) throw new PJeBridgeError('PAGE_NOT_FOUND', 'Formulário da parte não foi aberto.');
    try {
      for (const [key, value] of Object.entries(edit.dados || {})) {
        if (key === 'nome_social') {
          const checkbox = partyPage.locator('#formInserirParteProcesso\\:informarNomeSocial');
          if (value) await checkbox.check();
        }
        const locator = partyPage.locator(`[id="${PARTY_FIELDS[key]}"]`).first();
        await locator.waitFor({ state: 'visible', timeout: timeoutMs });
        if (await locator.isDisabled()) {
          throw new PJeBridgeError('FIELD_DISABLED', `Campo ${key} está desabilitado no PJe.`);
        }
        const tag = await locator.evaluate((element) => element.tagName);
        if (tag === 'SELECT') await chooseSelect(locator, value);
        else {
          await locator.clear();
          await locator.fill(value);
        }
      }
      if (edit.endereco) {
        await activateTab(partyPage, 'formInserirParteProcesso:enderecoUsuario_lbl', timeoutMs);
        const addressEdit = partyPage.locator(
          `a[title="Editar"][id*="enderecoGridTabList:${edit.endereco.indice}:"]`,
        ).first();
        if (!await addressEdit.isVisible().catch(() => false)) {
          throw new PJeBridgeError('ADDRESS_NOT_FOUND', 'Endereço não encontrado no índice informado.');
        }
        await waitForPost(partyPage, () => addressEdit.click(), timeoutMs);
        for (const [key, value] of Object.entries(edit.endereco.dados)) {
          const locator = partyPage.locator(`[id="${ADDRESS_FIELDS[key]}"]`).first();
          await locator.waitFor({ state: 'visible', timeout: timeoutMs });
          if (key === 'correspondencia') {
            await locator.setChecked(value, { force: true });
          } else {
            if (await locator.isDisabled()) {
              throw new PJeBridgeError('FIELD_DISABLED', `Campo de endereço ${key} está desabilitado.`);
            }
            await locator.clear();
            await locator.fill(value);
          }
        }
        if (commit) {
          const saveAddress = partyPage.locator(
            '#formInserirParteProcesso\\:cadastroPartePessoaEnderecobtnAtualizarEndereco',
          );
          await waitForPost(partyPage, () => saveAddress.click(), timeoutMs);
        }
      }
      if (edit.contato) {
        await activateTab(partyPage, 'formInserirParteProcesso:meioContatoRichTab_lbl', timeoutMs);
        if (edit.contato.acao === 'adicionar') {
          await chooseSelect(partyPage.locator(
            'select[id$="tipoContato"]:visible',
          ), edit.contato.tipo);
          const value = partyPage.locator(
            'input[id$="identificacaoMeioContatoText"]:visible',
          );
          await value.clear();
          await value.fill(edit.contato.valor);
          if (commit) {
            await waitForPost(partyPage, () => partyPage.locator(
              'input[type="button"][value="Incluir"]:visible',
            ).last().click(), timeoutMs);
          }
        } else {
          const remove = partyPage.locator(
            `a[title="Excluir"][id*="gridProcessoParteVinculoPessoaMeioContatoList:${edit.contato.indice}:"]`,
          ).first();
          if (!await remove.isVisible().catch(() => false)) {
            throw new PJeBridgeError('CONTACT_NOT_FOUND', 'Contato não encontrado no índice informado.');
          }
          if (commit) await waitForPost(partyPage, () => remove.click(), timeoutMs);
        }
      }
      if (!commit) {
        const cancel = partyPage.locator(
          'input[type="button"][value="Cancelar"]:visible, ' +
          'button:has-text("Cancelar"):visible',
        ).last();
        if (!await cancel.isVisible().catch(() => false)) {
          throw new PJeBridgeError('CANCEL_NOT_AVAILABLE', 'O PJe não exibiu o cancelamento da parte.');
        }
        await cancel.click({ force: true });
        return {
          section: 'partes', polo: edit.polo, indice: edit.indice,
          operation: edit.endereco ? 'endereco' : edit.contato ? 'contato' : 'dados',
          status: 'simulated_and_cancelled',
        };
      }
      const save = partyPage.locator(
        '#formInserirParteProcesso\\:btnAtualizarParte:visible, ' +
        '#formInserirParteProcesso\\:btnComplementarDadosParte:visible',
      ).first();
      await waitForPost(partyPage, () => save.click(), timeoutMs);
      return {
        section: 'partes', polo: edit.polo, indice: edit.indice,
        operation: edit.endereco ? 'endereco' : edit.contato ? 'contato' : 'dados',
        status: 'saved',
      };
    } finally {
      if (partyPage !== page) await partyPage.close().catch(() => {});
    }
  }

  async function applyPublicProsecutor(page, changes, timeoutMs) {
    if (changes.acao === 'adicionar') {
      throw new PJeBridgeError(
        'PARTY_ADDITION_NOT_AVAILABLE',
        'Esta tela de retificação não expõe inclusão de nova parte; o MPPA não foi adicionado.',
      );
    }
    await activateTab(page, TABS.parties, timeoutMs);
    const row = page.locator(
      'table[id^="gridPartesPolo"] tr.rich-table-row, ' +
      'table[id="gridPartesOutrosParticipantesList"] tr.rich-table-row',
    ).filter({ hasText: INSTITUTIONS.mppa.cnpj }).first();
    if (!await row.isVisible().catch(() => false)) {
      throw new PJeBridgeError(
        'MPPA_NOT_FOUND',
        'O MPPA com o CNPJ institucional confirmado não está cadastrado neste processo.',
      );
    }
    const remove = row.locator('a[title="Remover Parte"]').first();
    if (!await remove.isVisible().catch(() => false)) {
      throw new PJeBridgeError('PARTY_REMOVAL_NOT_AVAILABLE', 'O PJe não liberou a remoção do MPPA.');
    }
    await waitForPost(page, () => remove.click(), timeoutMs);
    return {
      section: 'ministerio_publico', operation: 'remover',
      cnpj: INSTITUTIONS.mppa.cnpj, status: 'saved',
    };
  }

  async function simulatePublicProsecutor(page, changes, timeoutMs) {
    await activateTab(page, TABS.parties, timeoutMs);
    const row = page.locator(
      'table[id^="gridPartesPolo"] tr.rich-table-row, ' +
      'table[id="gridPartesOutrosParticipantesList"] tr.rich-table-row',
    ).filter({ hasText: INSTITUTIONS.mppa.cnpj }).first();
    const present = await row.isVisible().catch(() => false);
    if (changes.acao === 'remover' && !present) {
      throw new PJeBridgeError('MPPA_NOT_FOUND', 'O MPPA institucional não está neste processo.');
    }
    if (changes.acao === 'adicionar') {
      throw new PJeBridgeError(
        'PARTY_ADDITION_NOT_AVAILABLE',
        'A retificação atual não oferece controle de inclusão de nova parte; nada foi alterado.',
      );
    }
    return {
      section: 'ministerio_publico', operation: changes.acao,
      cnpj: INSTITUTIONS.mppa.cnpj, status: 'simulated_without_click',
    };
  }

  async function simulate(payload, timeoutMs) {
    const processNumber = normalizeCnj(payload.process_number);
    const changes = normalizeChanges(payload.changes);
    const opened = await openForm(processNumber, timeoutMs);
    let before;
    const completed = [];
    try {
      before = await extractState(opened.formPage, timeoutMs);
      for (const edit of changes.partes || []) {
        completed.push(await applyPartyEdit(opened.formPage, edit, timeoutMs, false));
      }
      if (changes.ministerio_publico) {
        completed.push(await simulatePublicProsecutor(
          opened.formPage, changes.ministerio_publico, timeoutMs,
        ));
      }
      if (!changes.partes && !changes.ministerio_publico) {
        throw new PJeBridgeError(
          'SIMULATION_NOT_SUPPORTED',
          'A simulação controlada está disponível para partes, endereço, contato e MPPA.',
        );
      }
    } finally {
      await opened.formPage.close().catch(() => {});
    }
    const verification = await openForm(processNumber, timeoutMs);
    try {
      const after = await extractState(verification.formPage, timeoutMs);
      if (hash(before) !== hash(after)) {
        throw new PJeBridgeError(
          'SIMULATION_CHANGED_STATE',
          'O estado do formulário divergiu após a simulação; nenhuma aplicação adicional foi feita.',
        );
      }
      return {
        status: 'simulated_and_verified',
        process_number: processNumber,
        completed_steps: completed,
        state_sha256: hash(after),
        mutating: false,
      };
    } finally {
      await verification.formPage.close().catch(() => {});
    }
  }

  async function checkBooleanRadio(page, prefix, value) {
    const radio = page.locator(`input[id^="${prefix}"]`).nth(value ? 0 : 1);
    await radio.check({ force: true });
  }

  async function applyCharacteristics(page, changes, timeoutMs) {
    await activateTab(page, TABS.characteristics, timeoutMs);
    if (changes.tutela_liminar !== undefined) {
      await checkBooleanRadio(page, 'caracteristica:tutelaLiminar:', changes.tutela_liminar);
    }
    if (changes.justica_gratuita !== undefined) {
      await checkBooleanRadio(page, 'caracteristica:documentoCusta:', changes.justica_gratuita);
    }
    if (changes.valor_causa !== undefined) {
      const field = page.locator(
        '#caracteristica\\:valorCausa\\:valorCausaDecoration\\:valorCausa',
      );
      await field.clear();
      await field.fill(changes.valor_causa);
    }
    const result = { section: 'caracteristicas', status: 'saved' };
    if (changes.prioridade !== undefined) {
      const select = page.locator(
        '#processoPrioridade\\:prioridadeProcessoDecoration\\:prioridadeProcesso',
      );
      await chooseSelect(select, changes.prioridade);
      await waitForPost(page, () => page.locator('#processoPrioridade\\:persistButton').click(), timeoutMs);
      result.priority_added = true;
    }
    const save = page.locator('#caracteristica\\:salvaCaracteristicaProcessoButton');
    await waitForPost(page, () => save.click(), timeoutMs);
    return result;
  }

  async function apply(payload, timeoutMs) {
    const processNumber = normalizeCnj(payload.process_number);
    const changes = normalizeChanges(payload.changes);
    const token = String(payload.confirmation_token || '').trim();
    const previewData = previews.get(token);
    previews.delete(token);
    if (!previewData || previewData.expiresAt < Date.now() ||
        previewData.processNumber !== processNumber ||
        previewData.changesHash !== hash(changes)) {
      throw new PJeBridgeError(
        'CONFIRMATION_REQUIRED',
        'Prévia ausente, expirada ou diferente das alterações solicitadas.',
      );
    }
    const opened = await openForm(processNumber, timeoutMs);
    const completed = [];
    try {
      const currentState = await extractState(opened.formPage, timeoutMs);
      if (hash(currentState) !== previewData.stateHash) {
        throw new PJeBridgeError(
          'FORM_CHANGED_SINCE_PREVIEW',
          'A autuação mudou depois da prévia; gere uma nova confirmação.',
        );
      }
      if (changes.dados_iniciais) {
        completed.push(await applyInitial(opened.formPage, changes.dados_iniciais, timeoutMs));
      }
      if (changes.assuntos) {
        completed.push(await applySubjects(opened.formPage, changes.assuntos, timeoutMs));
      }
      for (const edit of changes.partes || []) {
        completed.push(await applyPartyEdit(opened.formPage, edit, timeoutMs));
      }
      if (changes.ministerio_publico) {
        completed.push(await applyPublicProsecutor(
          opened.formPage, changes.ministerio_publico, timeoutMs,
        ));
      }
      if (changes.caracteristicas) {
        completed.push(await applyCharacteristics(opened.formPage, changes.caracteristicas, timeoutMs));
      }
      return {
        status: 'applied',
        process_number: processNumber,
        completed_steps: completed,
        mutating: true,
      };
    } catch (error) {
      if (completed.length) {
        error.completed_steps = completed;
        error.message = `${error.message} Alterações anteriores podem já ter sido gravadas.`;
      }
      throw error;
    } finally {
      await opened.formPage.close().catch(() => {});
    }
  }

  return { apply, inspect, listCandidates, preview, simulate };
}

module.exports = {
  ADDRESS_FIELDS,
  INSTITUTIONS,
  PARTY_FIELDS,
  PARTY_GRIDS,
  TABS,
  createRetificationOperations,
  normalizeChanges,
};
