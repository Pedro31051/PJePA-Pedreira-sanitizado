const { connect } = require('./browser_config.js');
const { artifactPath } = require('./artifact_path');

(async () => {
  try {
    const { browser, context, page } = await connect();
    
    const input = page.locator('input[placeholder*="api.exemplo.com"]');
    if (await input.isVisible()) {
      const connectorUrl = (process.env.PJE_MCP_CONNECTOR_URL || '').trim();
      if (!connectorUrl) throw new Error('Defina PJE_MCP_CONNECTOR_URL');
      await input.fill(connectorUrl);
      await page.waitForTimeout(500);
      
      const submitBtn = page.locator('button[type="submit"]:has-text("Adicionar")');
      await submitBtn.click();
      await page.waitForTimeout(4000);
    } else {
      console.log('Input not found directly, checking page state...');
    }
    
    await page.screenshot({ path: artifactPath('claude_pje_submit_result.png') });
    
    const bodyText = await page.evaluate(() => document.body.innerText);
    console.log('--- BODY TEXT AFTER SUBMIT ---');
    console.log(bodyText);

    const inputs = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('input, textarea, select, button')).map(el => ({
        tag: el.tagName,
        type: el.type || '',
        placeholder: el.placeholder || '',
        name: el.name || '',
        text: el.innerText || el.value || '',
        ariaLabel: el.getAttribute('aria-label') || ''
      }));
    });
    console.log('--- INPUTS & BUTTONS AFTER SUBMIT ---');
    console.log(JSON.stringify(inputs, null, 2));

  } catch (err) {
    console.error('Error:', err);
    process.exit(1);
  }
})();
