const { connect } = require('./browser_config.js');
const { artifactPath } = require('./artifact_path');

(async () => {
  try {
    const { browser, context, page } = await connect();
    
    // Click "Adicionar conector"
    const addBtn = page.locator('button:has-text("Adicionar conector")').first();
    if (await addBtn.isVisible()) {
      await addBtn.click();
      await page.waitForTimeout(2000);
    } else {
      await page.click('text="Adicionar conector"');
      await page.waitForTimeout(2000);
    }
    
    await page.screenshot({ path: artifactPath('claude_add_connector_form.png') });
    
    // Log inputs and buttons on page
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
    console.log('--- INPUTS & BUTTONS ---');
    console.log(JSON.stringify(inputs, null, 2));

  } catch (err) {
    console.error('Error:', err);
    process.exit(1);
  }
})();
