const { chromium } = require('playwright');
const assert = require('node:assert/strict');

const base = process.env.TGBOT_URL || 'http://tgbot:8080';

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base);
  await page.fill('#admin-key', 'wrong-key-wrong-key');
  await page.click('#login-form button');
  await page.waitForSelector('#message[data-error=true]');
  assert.ok(await page.isVisible('#login'));
  await page.fill('#admin-key', process.env.TGBOT_ADMIN_KEY);
  await page.click('#login-form button');
  await page.waitForSelector('#app:not([hidden])');
  await page.waitForFunction(() => document.querySelector('#bot-status').textContent !== '—');
  assert.match(await page.inputValue('#llm_backends'), /^qwen=http:\/\/qwen:8080/);
  // Saving unchanged settings must round-trip without validation errors.
  await page.click('#settings-form button[type=submit]');
  await page.waitForSelector('#message:not([hidden])');
  assert.equal(await page.getAttribute('#message', 'data-error'), 'false');
  assert.equal(await page.textContent('#message'), 'Настройки сохранены');
  for (const [name, width, height] of [['desktop', 1400, 1000], ['mobile', 390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(300);
    await page.screenshot({ path: `/screenshots/${name}.png`, fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${name} overflows`);
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('PASS: settings page rejects a wrong key, loads, saves, fits desktop and mobile');
})().catch(error => { console.error(error); process.exit(1); });
