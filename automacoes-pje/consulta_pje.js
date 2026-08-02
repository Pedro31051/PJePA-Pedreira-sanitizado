const { connect, liteMode } = require('./browser_config');

function normalizarCpf(valor) {
  return String(valor || '').replace(/\D/g, '');
}

function cpfValido(valor) {
  const cpf = normalizarCpf(valor);
  if (cpf.length !== 11 || /^(\d)\1{10}$/.test(cpf)) return false;
  const digito = (tamanho) => {
    let soma = 0;
    for (let i = 0; i < tamanho; i++) soma += Number(cpf[i]) * (tamanho + 1 - i);
    const resto = (soma * 10) % 11;
    return resto === 10 ? 0 : resto;
  };
  return digito(9) === Number(cpf[9]) && digito(10) === Number(cpf[10]);
}

const CPF = process.argv[2];

if (!cpfValido(CPF)) {
  console.error('Uso: node consulta_pje.js <CPF válido>');
  process.exit(2);
}

(async () => {
  try {
    const { context, page } = await connect();
    await liteMode(context);

    // A página já está aberta na consulta PJe
    const currentUrl = page.url();
    console.log('📍 URL atual:', currentUrl);

    if (!currentUrl.includes('ConsultaProcesso')) {
      console.log('🔄 Navegando para a consulta...');
      await page.goto('https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam', { waitUntil: 'domcontentloaded', timeout: 30000 });
    }

    // 1. Limpar pesquisa anterior clicando em LIMPAR
    const limparBtn = page.locator('#fPP\\:clearButtonProcessos');
    if (await limparBtn.count() > 0) {
      console.log('🧹 Limpando pesquisa anterior...');
      await limparBtn.click().catch(() => {});
    }

    // 2. Preencher o campo CPF - id correto: fPP:dpDec:documentoParte
    const cpfSelector = '#fPP\\:dpDec\\:documentoParte';
    const campoCpf = page.locator(cpfSelector);
    await campoCpf.waitFor({ state: 'visible', timeout: 10000 });
    await campoCpf.clear();
    await campoCpf.fill(CPF);
    console.log('✅ Documento de consulta preenchido.');

    // Verificar se o valor foi preenchido
    const valorCampo = await campoCpf.inputValue();
    if (normalizarCpf(valorCampo) !== normalizarCpf(CPF)) {
      throw new Error('O PJe não reteve o documento informado no campo de consulta.');
    }

    // 3. Clicar em PESQUISAR
    const pesquisarBtn = page.locator('#fPP\\:searchProcessos');
    console.log('🔍 Clicando em PESQUISAR...');
    await pesquisarBtn.click();

    // 4. Esperar resultados na tabela ou mensagem de sem resultados
    await page.waitForSelector('.rich-table, .rich-table-row, [id*="processoList"], text=Nenhum registro encontrado', { timeout: 15000 }).catch(() => {});

    // 5. Extrair total de resultados
    const totalResult = await page.evaluate(() => {
      const text = document.body.innerText;
      const match = text.match(/(\d+)\s*resultados?\s*encontrados?/i);
      return match ? match[1] : 'Não encontrado';
    });
    console.log(`\n📊 TOTAL DE PROCESSOS: ${totalResult}`);

    // 6. Extrair detalhes dos processos visíveis na tabela
    const processos = await page.evaluate(() => {
      const resultRows = [];
      // A tabela de resultados do PJe usa rich-table
      const table = document.querySelector('.rich-table') || document.querySelector('[id*="processoList"]');
      
      // Tentar pegar todas as linhas da tabela de resultados
      const rows = document.querySelectorAll('.rich-table-row, tr[class*="rich-table"]');
      
      for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (cells.length >= 5) {
          const numero = (cells[0]?.textContent || '').trim();
          // Filtrar apenas linhas com número de processo
          if (numero.match(/\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}/)) {
            resultRows.push({
              numero,
              orgao: (cells[2]?.textContent || '').trim(),
              data: (cells[3]?.textContent || '').trim(),
              classe: (cells[4]?.textContent || '').trim(),
              poloAtivo: (cells[5]?.textContent || '').trim(),
              poloPassivo: (cells[6]?.textContent || '').trim()
            });
          }
        }
      }
      return resultRows;
    });

    if (processos.length > 0) {
      console.log(`\n📋 PROCESSOS ENCONTRADOS (primeira página):\n`);
      processos.forEach((p, i) => {
        console.log(`--- Processo ${i + 1} ---`);
        console.log(`  Número: ${p.numero}`);
        console.log(`  Órgão: ${p.orgao}`);
        console.log(`  Data: ${p.data}`);
        console.log(`  Classe: ${p.classe}`);
        console.log(`  Polo Ativo: ${p.poloAtivo}`);
        console.log(`  Polo Passivo: ${p.poloPassivo}`);
        console.log('');
      });
    } else {
      console.log('⚠️ Nenhum processo encontrado na extração da tabela');
      
      // Tentar extração alternativa
      const altProcessos = await page.evaluate(() => {
        const links = document.querySelectorAll('a[href*="Processo"], a[id*="processo"]');
        return Array.from(links).map(a => ({
          text: (a.textContent || '').trim(),
          href: a.href
        })).filter(l => l.text.match(/\d{7}/));
      });
      if (altProcessos.length > 0) {
        console.log('📋 Links de processos encontrados:');
        altProcessos.forEach((p, i) => console.log(`  ${i + 1}. ${p.text}`));
      }
    }

    console.log('\n✅ Consulta concluída.');
    process.exit(0);
  } catch (err) {
    console.error('❌ Erro:', err.message);
    process.exit(1);
  }
})();
