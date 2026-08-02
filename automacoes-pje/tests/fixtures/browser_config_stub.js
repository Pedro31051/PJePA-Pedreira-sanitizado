'use strict';

async function unavailable() {
  throw new Error('Navegador indisponível em teste unitário offline.');
}

module.exports = {
  connect: unavailable,
  liteMode: unavailable,
};
