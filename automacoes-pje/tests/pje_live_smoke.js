'use strict';

const { createPJeOperations } = require('../pje/operations');

function validateLiveSmokeEnvironment({
  env = process.env,
  platform = process.platform,
} = {}) {
  if (env.PJE_LIVE_TEST !== '1') {
    throw new Error('Smoke real bloqueado: defina PJE_LIVE_TEST=1 explicitamente.');
  }

  if (platform !== 'darwin') {
    throw new Error(
      'Smoke real indisponível neste host: execute no macOS que contém o ' +
      'Chrome for Testing v116 e o perfil-oficial.',
    );
  }

  const processNumber = String(env.PJE_TEST_CNJ || '').trim();
  if (!processNumber) {
    throw new Error('Defina PJE_TEST_CNJ com um processo autorizado para teste.');
  }
  if (/[<>]/.test(processNumber) || /CNJ_AUTORIZADO/i.test(processNumber)) {
    throw new Error(
      'PJE_TEST_CNJ ainda contém um placeholder; informe o CNJ real autorizado, sem < ou >.',
    );
  }
  return processNumber;
}

async function main() {
  const processNumber = validateLiveSmokeEnvironment();
  const execute = createPJeOperations();
  const state = await execute('get_state', {}, 10_000);
  if (!state.authenticated) throw new Error('Sessão oficial não confirmada.');
  await execute('search_process', { process_number: processNumber }, 15_000);
  console.log('✅ Smoke PJe somente leitura concluído; sessão e busca confirmadas.');
}

if (require.main === module) {
  main()
    .then(() => process.exit(0))
    .catch((error) => {
      console.error(`❌ Smoke PJe falhou: ${error.message}`);
      process.exit(1);
    });
}

module.exports = { main, validateLiveSmokeEnvironment };
