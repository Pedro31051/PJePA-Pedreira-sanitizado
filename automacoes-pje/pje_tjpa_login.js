/**
 * pje_tjpa_login.js — autenticação do PJe-TJPA pelo mesmo fluxo do MCP PJe.
 *
 * Fluxo principal:
 *   1. abre /login.seam pelo Chrome oficial conectado por browser_config.js;
 *   2. reaproveita uma sessão somente quando o DOM confirma o menu do usuário;
 *   3. se necessário, lê CPF, senha e seed TOTP do cofre do sistema;
 *   4. preenche o SSO PDPJ e conclui o segundo fator em memória;
 *   5. considera sucesso apenas depois de sair do SSO e confirmar um marcador
 *      autenticado do PJe.
 *
 * Nenhuma credencial, cookie, código TOTP ou URL com query é impressa ou
 * gravada pelo script. No Linux, as credenciais vêm do mesmo
 * systemd-credentials usado por mcp-pje-tjpa. No macOS, o fallback consulta os
 * services mcp-pje-* no Keychain. Se nenhum cofre estiver disponível, o fluxo
 * por certificado A3/PJeOffice continua disponível como alternativa.
 */
const fs = require('fs');
const http = require('http');
const path = require('path');
const { execFileSync } = require('child_process');
const { gerarCodigo } = require('./pje_totp');

const BASE_URL = 'https://pje.tjpa.jus.br/pje';
const PAINEL = BASE_URL;
const LOGIN = `${BASE_URL}/login.seam`;
const SSO_HOST = 'sso.cloud.pje.jus.br';
const PJE_HOST = 'pje.tjpa.jus.br';
const DEFAULT_CREDENTIALS_DIRECTORY = '/run/credentials/pjepa-mcp.service';
const KEYCHAIN_SERVICES = ['mcp-pje-tjpa', 'mcp-pje-tjma', 'mcp-pje-tjpi'];
const CREDENTIAL_NAMES = ['cpf', 'senha', 'totp_seed'];
const PJEOFFICE_TIMEOUT_MS = 30_000;
const ESPERA_ASSINATURA_MS = 16_000;

const SESSION_MARKERS = [
  'li.menu-usuario',
  'a[href*="logout"]',
  'a.dropdown-toggle',
  '[id*="usuarioLogado"]',
  '[class*="usuario-logado"]',
  '[data-testid="user-menu"]',
].join(', ');

const LOGIN_MARKERS = [
  '#kc-form-login',
  'input#username',
  'input#password',
  'input[autocomplete="one-time-code"]',
  '#kc-pje-office',
].join(', ');

const OTP_SELECTOR = [
  'input#otp',
  'input[name="otp"]',
  'input[autocomplete="one-time-code"]',
  "input[type='text']:not(#username)",
].join(', ');

function urlSemSegredos(value) {
  try {
    const url = new URL(value);
    return `${url.protocol}//${url.host}${url.pathname}`;
  } catch (_) {
    return String(value || '').replace(/[?#].*$/, '');
  }
}

function lerCredencialMac(service, account) {
  try {
    return execFileSync(
      'security',
      ['find-generic-password', '-s', service, '-a', account, '-w'],
      {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      },
    ).trim();
  } catch (_) {
    return '';
  }
}

/**
 * Lê credenciais do mesmo cofre usado pelo MCP, sem registrá-las em log.
 * Dependências injetáveis existem apenas para testes herméticos.
 */
function carregarCredenciaisPdpj({
  env = process.env,
  platform = process.platform,
  readFile = (file) => fs.readFileSync(file, 'utf8'),
} = {}) {
  const directories = [
    env.PJE_CREDENTIALS_DIRECTORY,
    env.CREDENTIALS_DIRECTORY,
    DEFAULT_CREDENTIALS_DIRECTORY,
  ].filter((value, index, values) => value && values.indexOf(value) === index);

  for (const directory of directories) {
    try {
      const values = CREDENTIAL_NAMES.map(
        (name) => String(readFile(path.join(directory, name))).trim(),
      );
      if (values.every(Boolean)) {
        return {
          cpf: values[0],
          senha: values[1],
          totpSeed: values[2],
          source: 'systemd-credentials',
        };
      }
    } catch (_) {
      // Tenta a próxima fonte sem revelar caminho, permissão ou conteúdo.
    }
  }

  if (platform === 'darwin') {
    for (const service of KEYCHAIN_SERVICES) {
      const cpf = lerCredencialMac(service, 'cpf');
      const senha = lerCredencialMac(service, 'senha');
      const totpSeed = lerCredencialMac(service, 'totp_seed');
      if (cpf && senha && totpSeed) {
        return { cpf, senha, totpSeed, source: 'keychain' };
      }
    }
  }

  return null;
}

function validarCredenciais(credentials) {
  return Boolean(
    credentials
      && /^\d{11}$/.test(credentials.cpf)
      && credentials.senha
      && /^[A-Z2-7\s=-]+$/i.test(credentials.totpSeed),
  );
}

async function localizarVisivel(page, selector) {
  for (const frame of page.frames()) {
    const locator = frame.locator(selector).first();
    if (await locator.isVisible().catch(() => false)) return locator;
  }
  return null;
}

async function esperarFormularioCredenciais(page, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const username = await localizarVisivel(page, 'input#username');
    const password = await localizarVisivel(page, 'input#password');
    if (username && password) return { username, password };
    await page.waitForTimeout(100);
  }
  return null;
}

async function estaLogado(page) {
  let current;
  try {
    current = new URL(page.url());
  } catch (_) {
    return false;
  }
  if (current.protocol !== 'https:' || current.hostname !== PJE_HOST) return false;
  if (current.pathname.toLowerCase().includes('login.seam')) return false;
  if (await localizarVisivel(page, LOGIN_MARKERS)) return false;
  return Boolean(await localizarVisivel(page, SESSION_MARKERS));
}

async function esperarSessaoAutenticada(page, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await estaLogado(page)) return true;
    await page.waitForTimeout(250);
  }
  return false;
}

async function submeter(page, field, timeoutMs = 15_000) {
  const button = await localizarVisivel(
    page,
    '#kc-login, input[name="login"], input[type="submit"], button[type="submit"]',
  );
  const navigation = page.waitForNavigation({
    waitUntil: 'domcontentloaded',
    timeout: timeoutMs,
  }).catch(() => null);
  if (button) await button.click();
  else await field.press('Enter');
  await navigation;
}

async function codigoTotpEstavel(page, seed) {
  const epochSeconds = Math.floor(Date.now() / 1000);
  const remaining = 30 - (epochSeconds % 30);
  if (remaining < 3) await page.waitForTimeout((remaining + 1) * 1000);
  return gerarCodigo(seed);
}

async function erroCurtoSso(page) {
  try {
    const lines = await page.locator('body').evaluate((body) => (
      (body.textContent || '')
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line && line.length < 160)
    ));
    const keys = [
      'expirad',
      'inválid',
      'invalid',
      'incorret',
      'bloquead',
      'não foi possível',
      'tente novamente',
      'nova senha',
    ];
    return lines.find((line) => keys.some((key) => line.toLowerCase().includes(key))) || null;
  } catch (_) {
    return null;
  }
}

async function loginComCredenciais(page, credentials, { verbose = true } = {}) {
  const log = (...args) => verbose && console.log('[pje-login]', ...args);
  if (!validarCredenciais(credentials)) {
    throw new Error('cofre PDPJ incompleto ou com formato inválido');
  }

  const form = await esperarFormularioCredenciais(page);
  if (!form) {
    throw new Error('formulário CPF/senha do SSO não foi encontrado');
  }
  const { username, password } = form;

  await username.clear();
  await username.fill(credentials.cpf);
  await password.clear();
  await password.fill(credentials.senha);
  await submeter(page, password);

  if (await esperarSessaoAutenticada(page, 5_000)) {
    log('LOGADO ✅ (SSO não exigiu segundo fator)');
    return true;
  }

  const passwordAgain = await localizarVisivel(page, 'input#password');
  if (passwordAgain) {
    const detail = await erroCurtoSso(page);
    throw new Error(detail || 'o SSO rejeitou CPF/senha');
  }

  const otp = await localizarVisivel(page, OTP_SELECTOR);
  if (!otp) {
    throw new Error(
      `segundo fator não apareceu e a sessão não foi confirmada em ${urlSemSegredos(page.url())}`,
    );
  }

  const code = await codigoTotpEstavel(page, credentials.totpSeed);
  await otp.clear();
  await otp.fill(code);
  await submeter(page, otp, 30_000);

  const leftSso = await page.waitForURL((value) => {
    try {
      return new URL(value).hostname !== SSO_HOST;
    } catch (_) {
      return false;
    }
  }, { timeout: 15_000 }).then(() => true).catch(() => false);

  if (!leftSso || !(await esperarSessaoAutenticada(page, 15_000))) {
    const detail = await erroCurtoSso(page);
    throw new Error(detail || 'sessão não confirmada após o segundo fator');
  }

  log('LOGADO ✅ (CPF + senha + TOTP, sessão confirmada no DOM)');
  return true;
}

function chamarPjeOffice(url) {
  return new Promise((resolve) => {
    const request = http.get(url, {
      headers: {
        Referer: LOGIN,
        Origin: `https://${PJE_HOST}`,
        versao: '2.5.16',
      },
    }, (response) => {
      response.on('data', () => {});
      response.on('end', () => resolve(response.statusCode));
    });
    request.on('error', (error) => resolve(`ERR:${error.code || error.name}`));
    request.setTimeout(PJEOFFICE_TIMEOUT_MS, () => {
      request.destroy();
      resolve('TIMEOUT');
    });
  });
}

function tokenDoDesafio(url) {
  const match = url.match(/[?&]r=([^&]+)/);
  if (!match) throw new Error('desafio do PJeOffice sem parâmetro r');
  const payload = JSON.parse(decodeURIComponent(match[1]));
  const task = typeof payload.tarefa === 'string'
    ? JSON.parse(payload.tarefa)
    : payload.tarefa;
  return task.token;
}

async function loginComPjeOffice(page, { verbose = true } = {}) {
  const log = (...args) => verbose && console.log('[pje-login]', ...args);
  const certificateButton = await localizarVisivel(page, '#kc-pje-office');
  if (!certificateButton) {
    throw new Error('botão de certificado digital não encontrado');
  }

  let challenge = null;
  const capture = (request) => {
    if (
      request.url().includes('localhost:8800/pjeOffice/requisicao')
      && !challenge
    ) {
      challenge = request.url();
    }
  };
  const acceptDialog = (dialog) => dialog.accept().catch(() => {});
  page.on('request', capture);
  page.on('dialog', acceptDialog);
  try {
    await certificateButton.click();
    const deadline = Date.now() + 12_000;
    while (!challenge && Date.now() < deadline) {
      await page.waitForTimeout(250);
    }
  } finally {
    page.off('request', capture);
  }

  if (!challenge) {
    page.off('dialog', acceptDialog);
    throw new Error('desafio do PJeOffice não foi emitido');
  }

  const token = tokenDoDesafio(challenge);
  const status = await chamarPjeOffice(challenge);
  if (typeof status !== 'number' || status < 200 || status >= 400) {
    page.off('dialog', acceptDialog);
    throw new Error(`PJeOffice indisponível (${status})`);
  }
  log(`PJeOffice acionado (HTTP ${status}); aguardando assinatura A3...`);
  await page.waitForTimeout(ESPERA_ASSINATURA_MS);

  await page.evaluate((value) => {
    const element = document.getElementById('pjeoffice-code');
    if (element) element.value = value;
    if (document.forms.length) document.forms[0].submit();
  }, token);

  await page.waitForURL((value) => {
    try {
      return new URL(value).hostname !== SSO_HOST;
    } catch (_) {
      return false;
    }
  }, { timeout: 30_000 }).catch(() => {});
  page.off('dialog', acceptDialog);

  if (!(await esperarSessaoAutenticada(page, 15_000))) {
    throw new Error('assinatura A3 concluída sem marcador autenticado do PJe');
  }
  log('LOGADO ✅ (certificado A3, sessão confirmada no DOM)');
  return true;
}

/**
 * Garante uma sessão autenticada no PJe-TJPA.
 *
 * Ordem:
 *   1. sessão já existente no perfil oficial;
 *   2. CPF + senha + TOTP do mesmo cofre do MCP;
 *   3. certificado A3/PJeOffice, quando não houver credenciais no cofre.
 */
async function garantirLogin(page, { verbose = true } = {}) {
  const log = (...args) => verbose && console.log('[pje-login]', ...args);

  await page.goto(LOGIN, {
    waitUntil: 'domcontentloaded',
    timeout: 40_000,
  });

  if (await esperarSessaoAutenticada(page, 10_000)) {
    log('sessão reaproveitada e confirmada no DOM.');
    return true;
  }

  const credentials = carregarCredenciaisPdpj();
  if (credentials) {
    log(`sessão ausente — autenticando pelo cofre ${credentials.source}...`);
    return loginComCredenciais(page, credentials, { verbose });
  }

  log('cofre PDPJ indisponível — tentando certificado A3/PJeOffice...');
  return loginComPjeOffice(page, { verbose });
}

/**
 * Helper de autenticação single-flight reutilizável.
 */
async function loginSingleFlight({ page, cpf, senha, totpSeed, verbose = true } = {}) {
  const credentials = (cpf && senha && totpSeed)
    ? { cpf, senha, totpSeed, source: 'single-flight-param' }
    : carregarCredenciaisPdpj();

  if (!credentials || !validarCredenciais(credentials)) {
    return { success: false, error: 'credenciais_invalidas', code: 'INVALID_CREDENTIALS' };
  }

  try {
    if (await estaLogado(page)) {
      return { success: true, status: 'already_logged_in' };
    }
    await page.goto(LOGIN, { waitUntil: 'domcontentloaded', timeout: 30_000 }).catch(() => {});
    if (await estaLogado(page)) {
      return { success: true, status: 'already_logged_in' };
    }
    const ok = await loginComCredenciais(page, credentials, { verbose });
    return { success: ok, status: ok ? 'authenticated' : 'failed' };
  } catch (error) {
    return { success: false, error: error.message, code: error.code || 'AUTH_FAILED' };
  }
}

module.exports = {
  BASE_URL,
  LOGIN,
  PAINEL,
  carregarCredenciaisPdpj,
  estaLogado,
  garantirLogin,
  loginComCredenciais,
  loginSingleFlight,
  urlSemSegredos,
  validarCredenciais,
};

if (require.main === module) {
  const { connect } = require('./browser_config');
  (async () => {
    try {
      const { page } = await connect();
      const ok = await garantirLogin(page);
      process.exit(ok ? 0 : 1);
    } catch (error) {
      console.error('❌', error.message);
      process.exit(1);
    }
  })();
}
