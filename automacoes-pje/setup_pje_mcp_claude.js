const { connect } = require('./browser_config.js');
const { artifactPath } = require('./artifact_path');

(async () => {
  try {
    const { browser, context, page } = await connect();
    
    // Go to claude.ai
    await page.goto('https://claude.ai/new', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2000);
    
    // Click profile at bottom left
    const profileLabel = (process.env.CLAUDE_PROFILE_LABEL || '').trim();
    const profileBtn = profileLabel
      ? page.getByText(profileLabel, { exact: true })
      : page.locator('button[aria-label*="perfil" i], button[aria-label*="profile" i]').first();
    if (await profileBtn.isVisible()) {
      await profileBtn.click();
      await page.waitForTimeout(1000);
    } else {
      // Try profile image/avatar or avatar text
      const avatar = page.locator('button[aria-haspopup="menu"]').last();
      if (await avatar.isVisible()) {
        await avatar.click();
        await page.waitForTimeout(1000);
      }
    }
    
    // Look for "Conectores"
    const conectoresBtn = page.locator('button:has-text("Conectores"), div:has-text("Conectores")').filter({ hasText: /^Conectores$/ });
    if (await conectoresBtn.first().isVisible()) {
      await conectoresBtn.first().click();
      await page.waitForTimeout(4000);
    } else {
      // Try text click
      await page.click('text="Conectores"');
      await page.waitForTimeout(4000);
    }
    
    await page.screenshot({ path: artifactPath('claude_connectors_page.png') });
    
    const bodyText = await page.evaluate(() => document.body.innerText);
    console.log('--- BODY TEXT ---');
    console.log(bodyText);

  } catch (err) {
    console.error('Error:', err);
    process.exit(1);
  }
})();
