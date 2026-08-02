'use strict';

const { PJeBridgeError } = require('./contracts');
const selectors = require('./selectors');

const ALLOWED_HOST = 'pje.tjpa.jus.br';

function isAllowedPJeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === ALLOWED_HOST;
  } catch (_) {
    return false;
  }
}

async function findOfficialPJePage(context) {
  const pages = context.pages();
  for (const page of [...pages].reverse()) {
    if (isAllowedPJeUrl(page.url())) return page;
  }
  throw new PJeBridgeError(
    'PAGE_NOT_FOUND',
    'Nenhuma aba HTTPS oficial do PJe-TJPA foi encontrada.',
  );
}

async function findAuthenticatedPJePage(context) {
  let lastError = null;
  for (const page of [...context.pages()].reverse()) {
    if (!isAllowedPJeUrl(page.url())) continue;
    try {
      await assertAuthenticatedSession(page);
      return page;
    } catch (error) {
      lastError = error;
    }
  }
  if (lastError) throw lastError;
  throw new PJeBridgeError(
    'PAGE_NOT_FOUND',
    'Nenhuma aba HTTPS oficial e autenticada do PJe-TJPA foi encontrada.',
  );
}

async function assertAuthenticatedSession(page) {
  if (!isAllowedPJeUrl(page.url())) {
    throw new PJeBridgeError('UNEXPECTED_HOST', 'A aba ativa não pertence ao host oficial do TJPA.');
  }
  const url = new URL(page.url());
  if (url.pathname.toLowerCase().includes('login.seam')) {
    throw new PJeBridgeError('SESSION_EXPIRED', 'Sessão expirada; faça login manual no perfil oficial.');
  }
  const loginVisible = await page.locator(selectors.marcadoresLogin)
    .first().isVisible().catch(() => false);
  if (loginVisible) {
    throw new PJeBridgeError('SESSION_EXPIRED', 'O PJe está exibindo uma etapa de autenticação manual.');
  }
  const sessionVisible = await page.locator(selectors.marcadoresSessao)
    .first().isVisible().catch(() => false);
  if (!sessionVisible) {
    throw new PJeBridgeError(
      'SESSION_EXPIRED',
      'Não foi possível confirmar a sessão por um marcador autenticado do PJe.',
    );
  }
}

module.exports = {
  ALLOWED_HOST,
  assertAuthenticatedSession,
  findAuthenticatedPJePage,
  findOfficialPJePage,
  isAllowedPJeUrl,
};
