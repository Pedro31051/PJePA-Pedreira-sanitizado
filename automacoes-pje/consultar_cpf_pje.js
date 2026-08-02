/**
 * consultar_cpf_pje.js — Consulta processos por CPF na Consulta Processual
 * logada do PJe-TJPA, garantindo a sessão NO PONTO CERTO.
 *
 * Por quê: o painel pode aceitar o JSESSIONID antigo enquanto a Consulta
 * Processual força re-auth no SSO (sso.cloud.pje.jus.br). Então a checagem de
 * login é feita na própria página de consulta: se ela redirecionar ao SSO,
 * refazemos ali o fluxo do certificado A3 via PJeOffice (mesma técnica
 * aprovada de pje_tjpa_login.js) e seguimos para a pesquisa.
 *
 * Uso: node consultar_cpf_pje.js "000.000.000-00"
 */
const http = require('http');
const { connect } = require('./browser_config');
const { preencherOtpSeNecessario } = require('./pje_totp');

const CPF = process.argv[2] || '000.000.000-00';
const CONSULTA = 'https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam';
const CAMPO_CPF = '#fPP\\:dpDec\\:documentoParte';
const BTN_PESQUISAR = '#fPP\\:searchProcessos';
const ESPERA_ASSINATURA_MS = 16000; // A3 leva ~10-15s (probing de drivers)

function chamarPjeOffice(url) {
  return new Promise((resolve) => {
    const req = http.get(url, {
      headers: {
        Referer: 'https://pje.tjpa.jus.br/pje/login.seam',
        Origin: 'https://pje.tjpa.jus.br',
        versao: '2.5.16',
      },
    }, (res) => { res.on('data', () => {}); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', (e) => resolve('ERR:' + e.message));
    req.setTimeout(30000, () => { req.destroy(); resolve('TIMEOUT'); });
  });
}

function tokenDoDesafio(url) {
  const m = url.match(/[?&]r=([^&]+)/);
  if (!m) throw new Error('desafio sem parâmetro r');
  const payload = JSON.parse(decodeURIComponent(m[1]));
  const tarefa = typeof payload.tarefa === 'string' ? JSON.parse(payload.tarefa) : payload.tarefa;
  return tarefa.token;
}

const noSso = (page) => page.url().includes('sso.cloud');

// Fluxo A3 executado A PARTIR da tela do SSO já aberta na aba.
async function loginA3NaTelaSso(page) {
  console.log('[a3] tela do SSO detectada — assinando com certificado A3...');
  const descartarDialog = (d) => d.accept().catch(() => {});
  page.on('dialog', descartarDialog);
  let desafio = null;
  const capturar = (r) => {
    if (r.url().includes('localhost:8800/pjeOffice/requisicao') && !desafio) desafio = r.url();
  };
  page.on('request', capturar);
  try {
    await page.waitForSelector('#kc-pje-office', { timeout: 15000 });
    await page.click('#kc-pje-office');
    for (let i = 0; i < 24 && !desafio; i++) await page.waitForTimeout(500);
  } finally {
    page.off('request', capturar);
  }
  if (!desafio) { page.off('dialog', descartarDialog); throw new Error('não capturei o desafio do PJeOffice (assinador rodando?)'); }

  const token = tokenDoDesafio(desafio);
  const status = await chamarPjeOffice(desafio);
  console.log(`[a3] PJeOffice acionado (HTTP ${status}); aguardando assinatura (~15s)...`);
  await page.waitForTimeout(ESPERA_ASSINATURA_MS);

  await page.evaluate((tk) => {
    const el = document.getElementById('pjeoffice-code');
    if (el) el.value = tk;
    if (document.forms.length) document.forms[0].submit();
  }, token);
  await page.waitForURL((u) => !u.toString().includes('sso.cloud'), { timeout: 30000 }).catch(() => {});
  page.off('dialog', descartarDialog);
  if (noSso(page)) throw new Error('login A3 não concluiu — ainda no SSO: ' + page.url());
  console.log('[a3] login concluído ✅ ' + page.url());
}

(async () => {
  try {
    const { page } = await connect();

    // 1. Ir à consulta; se cair no SSO, autenticar ali mesmo e voltar.
    for (let tentativa = 1; tentativa <= 2; tentativa++) {
      await page.goto(CONSULTA, { waitUntil: 'domcontentloaded', timeout: 40000 });
      await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
      if (!noSso(page)) break;
      if (tentativa === 2) throw new Error('consulta continua redirecionando ao SSO após login');
      await loginA3NaTelaSso(page);
    }
    console.log('📍 Na consulta:', page.url());

    // 2. Limpar pesquisa anterior, se o botão existir.
    const limparBtn = page.locator('#fPP\\:clearButtonProcessos');
    if (await limparBtn.count() > 0) {
      await limparBtn.click();
      await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
      await page.waitForTimeout(1000);
    }

    // 3. Preencher CPF e pesquisar.
    await page.waitForSelector(CAMPO_CPF, { state: 'visible', timeout: 20000 });
    await page.locator(CAMPO_CPF).clear();
    await page.locator(CAMPO_CPF).fill(CPF);
    console.log(`✅ CPF "${await page.locator(CAMPO_CPF).inputValue()}" no campo`);
    await page.locator(BTN_PESQUISAR).click();

    // 4. Esperar o painel de resultados atualizar (AJAX rich-faces).
    await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {});
    await page.waitForFunction(() => {
      const t = document.body.innerText;
      return /resultados?\s*encontrados?/i.test(t) || /nenhum\s+registro/i.test(t);
    }, { timeout: 45000 }).catch(() => {});
    await page.waitForTimeout(2000);

    // 5. Total + primeira página de processos.
    const resumo = await page.evaluate(() => {
      const text = document.body.innerText;
      const m = text.match(/(\d+)\s*resultados?\s*encontrados?/i);
      const total = m ? m[1] : null;
      const nenhum = /nenhum\s+registro\s+encontrado/i.test(text);
      const rows = [];
      for (const row of document.querySelectorAll('tr')) {
        const cells = Array.from(row.querySelectorAll('td')).map((c) => (c.textContent || '').trim());
        const idx = cells.findIndex((c) => /\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/.test(c));
        if (idx !== -1) rows.push(cells.filter(Boolean).join(' | ').slice(0, 400));
      }
      return { total, nenhum, rows: rows.slice(0, 25) };
    });

    console.log('\n===== RESULTADO =====');
    if (resumo.nenhum && !resumo.total) console.log('TOTAL: 0 (nenhum registro encontrado)');
    else console.log(`TOTAL: ${resumo.total ?? 'não identificado no texto da página'}`);
    if (resumo.rows.length) {
      console.log(`\nProcessos visíveis na 1ª página (${resumo.rows.length}):`);
      resumo.rows.forEach((r, i) => console.log(`${i + 1}. ${r}`));
    }
    process.exit(0);
  } catch (err) {
    console.error('❌ Erro:', err.message);
    process.exit(1);
  }
})();
